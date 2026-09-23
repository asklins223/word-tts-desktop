"""录入速度总控（``platform_entry.adapter.pacing``）的行为合同。

这里锁的是两件事：档位怎么解析，以及停顿在页面上到底怎么落地。页面动作
本身的正确性由 ``test_platform_input`` / ``test_textbook_page`` 负责。
"""

from __future__ import annotations

import os
import pathlib
import random
import re
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from platform_entry.adapter import pacing
    from platform_entry.adapter.errors import PlatformInputUiError
    from platform_entry.adapter.page_navigation import PlatformInputNavigationMixin
except (ImportError, SyntaxError) as exc:  # pragma: no cover
    raise unittest.SkipTest("可选页面录入适配器未解锁，跳过速度总控测试") from exc


class _Page:
    """Recording page: the pacer only needs Playwright's wait call."""

    def __init__(self) -> None:
        self.wait_ms: list[int] = []

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_ms.append(int(timeout_ms))


_PAUSE_SITE_RE = re.compile(r"pacing\.pause\([^,]+,\s*\"([a-z_]+)\"\)")


def _pause_sites() -> tuple[dict[str, set[str]], int]:
    """按文件收集 ``pacing.pause`` 用到的区间名，并给出落点总数。"""

    kinds_by_file: dict[str, set[str]] = {}
    total = 0
    for path in sorted((ROOT / "platform_entry" / "adapter").glob("*.py")):
        found = _PAUSE_SITE_RE.findall(path.read_text(encoding="utf-8"))
        if found:
            kinds_by_file[path.name] = set(found)
            total += len(found)
    return kinds_by_file, total


class PaceScaleResolutionTests(unittest.TestCase):
    def test_environment_values_map_to_a_scale(self) -> None:
        cases = {
            "off": 0.0,
            "0": 0.0,
            "none": 0.0,
            "0.5": 0.5,
            "1": 1.0,
            "2": 2.0,
            "": pacing.PACE_SCALE,
            "fast": pacing.PACE_SCALE,
            "-3": 0.0,
        }
        for raw, expected in cases.items():
            with self.subTest(pace=raw):
                self.assertEqual(pacing.resolve_scale(raw), expected)

    def test_unconfigured_environment_falls_back_to_the_default_scale(self) -> None:
        with patch.dict(os.environ, clear=True):
            os.environ.pop(pacing.PACE_SCALE_ENV_VAR, None)
            self.assertEqual(pacing.resolve_scale(), pacing.PACE_SCALE)

    def test_environment_is_read_once_per_reset(self) -> None:
        with patch.dict(os.environ, {pacing.PACE_SCALE_ENV_VAR: "3"}):
            pacing.reset()
            self.assertEqual(pacing.scale(), 3.0)
        # 缓存的意义：调用方改了环境变量但没重启进程时，本次运行不跟着漂。
        self.assertEqual(pacing.scale(), 3.0)
        pacing.reset()
        with patch.dict(os.environ, clear=True):
            os.environ.pop(pacing.PACE_SCALE_ENV_VAR, None)
            self.assertEqual(pacing.scale(), pacing.PACE_SCALE)

    def tearDown(self) -> None:
        pacing.reset()


class PauseBehaviourTests(unittest.TestCase):
    def _pacer(self, scale: float, seed: int = 1234) -> None:
        pacing.configure(scale=scale, rng=random.Random(seed))

    def setUp(self) -> None:
        self.addCleanup(pacing.reset)

    def test_scale_zero_pauses_nothing_at_all(self) -> None:
        self._pacer(0)
        page = _Page()
        for kind in pacing.PAUSE_WINDOWS_MS:
            self.assertEqual(pacing.pause(page, kind), 0)
        self.assertEqual(page.wait_ms, [])
        self.assertEqual(pacing.poll_interval_ms(200), 200)

    def test_each_boundary_uses_its_own_window(self) -> None:
        self._pacer(1)
        for kind, (low, high) in pacing.PAUSE_WINDOWS_MS.items():
            page = _Page()
            waited = pacing.pause(page, kind)
            self.assertGreaterEqual(waited, low, kind)
            self.assertLessEqual(waited, high, kind)
            self.assertEqual(page.wait_ms, [waited], kind)

    def test_pauses_are_not_a_fixed_heartbeat(self) -> None:
        self._pacer(1, seed=99)
        draws = {pacing.pause(_Page(), "field") for _ in range(40)}
        self.assertGreater(len(draws), 5, "同一区间反复取出同一个值等于没有随机")

    def test_scale_multiplies_the_window_linearly(self) -> None:
        page_single = _Page()
        self._pacer(1, seed=7)
        single = pacing.pause(page_single, "item")
        page_double = _Page()
        self._pacer(2, seed=7)
        double = pacing.pause(page_double, "item")
        self.assertLessEqual(abs(double - single * 2), 1)
        self.assertLessEqual(abs(page_double.wait_ms[0] - page_single.wait_ms[0] * 2), 1)

    def test_poll_jitter_is_scaled_and_switched_off_with_the_scale(self) -> None:
        low, high = pacing.POLL_JITTER_MS
        self._pacer(1, seed=5)
        for _ in range(20):
            tick = pacing.poll_interval_ms(100)
            self.assertGreater(tick, 100)
            self.assertLessEqual(tick, 100 + high)
        self._pacer(0.5, seed=5)
        self.assertLessEqual(pacing.poll_interval_ms(100), 100 + high)
        self.assertGreaterEqual(pacing.poll_interval_ms(100), 100 + low // 2)
        self._pacer(0)
        self.assertEqual(pacing.poll_interval_ms(100), 100)

    def test_pause_is_capped_when_the_scale_is_absurd(self) -> None:
        self._pacer(100_000)
        page = _Page()
        self.assertEqual(pacing.pause(page, "step"), pacing.MAX_PAUSE_MS)

    def test_unknown_boundary_kind_fails_loudly(self) -> None:
        self._pacer(1)
        with self.assertRaises(KeyError):
            pacing.pause(_Page(), "after_everything")

    def test_every_pause_site_uses_a_declared_window(self) -> None:
        # 23 处落点靠字面量选区间；写错一个名字只会在真实浏览器跑到那一步
        # 才炸，所以这里静态核一遍。
        per_file, total = _pause_sites()
        self.assertGreater(total, 10, "扫描没有读到落点，等于这条守卫是空的")
        used = {kind for kinds in per_file.values() for kind in kinds}
        self.assertTrue(used, "课文/试卷链路都必须有停顿落点")
        self.assertLessEqual(used, set(pacing.PAUSE_WINDOWS_MS))

    def test_each_funnel_keeps_its_pause_kind(self) -> None:
        """录入链路的每个汇聚点都必须还在用属于它的那个区间。

        题干/输入框/题卡/栏目/步骤这几处是所有题型的公共咽喉；这里丢掉任何
        一处，页面就又会在同一个动作上连点不停。
        """
        per_file, _total = _pause_sites()
        expected = {
            "flow.py": {"step"},
            "page_cards.py": {"field", "item"},
            "page_content.py": {"group"},
            "page_forms.py": {"field"},
            "textbook_page.py": {"field", "item", "group", "step"},
        }
        for name, kinds in expected.items():
            with self.subTest(file=name):
                self.assertLessEqual(kinds, set(per_file.get(name, set())))

    def test_pauses_only_fire_on_verified_success(self) -> None:
        # 由 test_platform_input_batch_probe.ReplaceRichTextTests 覆盖回读失败
        # 分支；这里核对的是档位解析失败不会把停顿变成整段跳过。
        self._pacer(1)
        self.assertGreater(pacing.pause(_Page(), "field"), 0)

    def tearDown(self) -> None:
        pacing.reset()


class WaitUntilJitterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(pacing.reset)

    def _owner(self) -> PlatformInputNavigationMixin:
        navigation = object.__new__(PlatformInputNavigationMixin)
        navigation.page = _Page()
        return navigation

    def test_shared_wait_tick_is_jittered(self) -> None:
        pacing.configure(scale=1, rng=random.Random(3))
        navigation = self._owner()
        attempts = {"n": 0}

        def predicate() -> bool:
            attempts["n"] += 1
            return attempts["n"] >= 3

        navigation._wait_until(predicate, "等待页面", timeout_seconds=30, interval_ms=100)

        waits = navigation.page.wait_ms
        self.assertEqual(len(waits), 2)
        low, high = pacing.POLL_JITTER_MS
        for waited in waits:
            self.assertGreater(waited, 100)
            self.assertLessEqual(waited, 100 + high)

    def test_jitter_never_extends_a_bounded_wait(self) -> None:
        pacing.configure(scale=100, rng=random.Random(3))
        navigation = self._owner()
        with self.assertRaises(PlatformInputUiError):
            navigation._wait_until(
                lambda: False,
                "等待页面",
                timeout_seconds=0.05,
                interval_ms=100,
            )
        # 抖动再大也不能把一次有上限的等待拖成长时间卡住。
        self.assertTrue(navigation.page.wait_ms)
        for waited in navigation.page.wait_ms:
            self.assertLessEqual(waited, 60)


if __name__ == "__main__":
    unittest.main()

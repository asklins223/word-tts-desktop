"""tools/audio_match_eval.py 的指标数学、fixture 冒烟与 CLI 测试。

测试重点：

- 用假 matcher 精确验证 recall / missing / 重复区间 / 跨度误差的数学；
- 真实真题 fixture 只做结构断言：匹配器仍在持续优化，不能把当前指标
  写死成回归门槛（真正守住数值的是 tests/test_audio_asr.py）；
- 导入工具模块不得触发 ASR 依赖加载。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "audio_match_eval.py"
FIXTURES_DIR = ROOT / "tests" / "fixtures"
REAL_FIXTURES = (
    ("audio_asr_exam.json", 16),
    ("audio_asr_exam_shenzhen_2024_set2.json", 19),
)


def _load_eval_module():
    spec = importlib.util.spec_from_file_location("audio_match_eval", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["audio_match_eval"] = module
    spec.loader.exec_module(module)
    return module


audio_match_eval = _load_eval_module()


def _items(*item_ids: str) -> list[dict[str, Any]]:
    return [
        {
            "item_id": item_id,
            "sequence": index,
            "normalized_content": f"synthetic content for {item_id}",
        }
        for index, item_id in enumerate(item_ids)
    ]


def _fixture(
    items: Sequence[Mapping[str, Any]],
    expected_spans: Sequence[Sequence[Sequence[float]]],
    speech: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if speech is None:
        speech = [
            {"start": float(index), "end": float(index) + 0.5, "text": f"row {index}"}
            for index in range(len(items))
        ]
    return {
        "description": "synthetic fixture",
        "items": [dict(item) for item in items],
        "speech": [dict(row) for row in speech],
        "expected_spans": [[list(span) for span in per_item] for per_item in expected_spans],
    }


def _fake_matcher(spans_by_item: Mapping[str, Sequence[tuple[float, float]]]):
    """返回只按给定区间“识别”的 matcher，并记录被调用时的入参规模。"""
    calls: list[tuple[int, int, list[str]]] = []

    def matcher(recognized, spans, items):  # noqa: ANN001 - 模拟匹配器签名
        calls.append(
            (len(recognized), len(spans), [str(item.get("item_id")) for item in items])
        )
        occurrences: list[dict[str, Any]] = []
        for item_id, item_spans in spans_by_item.items():
            for start, end in item_spans:
                occurrences.append(
                    {
                        "top_item_id": item_id,
                        "start": float(start),
                        "end": float(end),
                        "score": 0.9,
                    }
                )
        return occurrences

    matcher.calls = calls
    return matcher


class EvaluateMetricsTests(unittest.TestCase):
    def test_perfect_match_reports_clean_metrics(self) -> None:
        fixture = _fixture(
            _items("a", "b"),
            [[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0]]],
        )
        matcher = _fake_matcher({"a": [(1.0, 2.0), (3.0, 4.0)], "b": [(5.0, 6.0)]})

        result = audio_match_eval.evaluate(fixture, matcher)

        self.assertEqual(result["required_count"], 2)
        self.assertEqual(result["matched_count"], 2)
        self.assertEqual(result["missing_ids"], [])
        self.assertEqual(result["count_mismatch_ids"], [])
        self.assertEqual(result["over_tolerance_ids"], [])
        self.assertEqual(result["duplicate_binding_count"], 0)
        self.assertEqual(result["span_error_max"], 0.0)
        self.assertEqual(result["span_error_mean"], 0.0)
        self.assertTrue(result["ok"])
        # expected_spans 按 fixture 顺序对齐到 items，而不是按 matcher 返回顺序。
        self.assertEqual([row["item_id"] for row in result["items"]], ["a", "b"])
        self.assertEqual(result["items"][0]["expected_spans"], [[1.0, 2.0], [3.0, 4.0]])
        self.assertEqual(result["items"][0]["actual_spans"], [[1.0, 2.0], [3.0, 4.0]])
        # matcher 收到的就是 fixture 的识别文本与时间轴（含注入副本）。
        self.assertEqual(matcher.calls[0][:2], (2, 2))
        self.assertEqual(matcher.calls[0][2], ["a", "b"])

    def test_missing_item_is_reported_and_reduces_recall(self) -> None:
        fixture = _fixture(_items("a", "b"), [[[1.0, 2.0]], [[5.0, 6.0]]])

        result = audio_match_eval.evaluate(
            fixture, _fake_matcher({"a": [(1.0, 2.0)]})
        )

        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["missing_count"], 1)
        self.assertEqual(result["missing_ids"], ["b"])
        self.assertAlmostEqual(result["recall"], 0.5)
        self.assertFalse(result["ok"])
        self.assertTrue(result["items"][0]["matched"])
        self.assertFalse(result["items"][1]["matched"])
        self.assertEqual(result["items"][1]["actual_spans"], [])

    def test_span_offsets_produce_mean_and_max_error(self) -> None:
        fixture = _fixture(_items("a", "b"), [[[10.0, 20.0]], [[30.0, 40.0]]])
        matcher = _fake_matcher({"a": [(10.2, 20.1)], "b": [(33.0, 43.2)]})

        result = audio_match_eval.evaluate(fixture, matcher, tolerance=0.5)

        # a: max(0.2, 0.1) = 0.2；b: max(3.0, 3.2) = 3.2。
        self.assertAlmostEqual(result["span_error_mean"], 1.7)
        self.assertAlmostEqual(result["span_error_max"], 3.2)
        self.assertAlmostEqual(result["start_error_max"], 3.0)
        self.assertAlmostEqual(result["end_error_max"], 3.2)
        self.assertAlmostEqual(result["start_error_mean"], 1.6)
        self.assertAlmostEqual(result["end_error_mean"], 1.65)
        self.assertEqual(result["paired_span_count"], 2)
        self.assertEqual(result["over_tolerance_ids"], ["b"])
        self.assertEqual(result["over_tolerance_count"], 1)
        self.assertAlmostEqual(result["items"][0]["max_error"], 0.2)
        self.assertAlmostEqual(result["items"][1]["max_error"], 3.2)

    def test_tolerance_is_configurable(self) -> None:
        fixture = _fixture(_items("a"), [[[0.0, 10.0]]])
        matcher = _fake_matcher({"a": [(0.3, 10.2)]})  # 误差 0.3s

        lenient = audio_match_eval.evaluate(fixture, matcher, tolerance=0.5)
        strict = audio_match_eval.evaluate(fixture, matcher, tolerance=0.1)

        self.assertEqual(lenient["over_tolerance_ids"], [])
        self.assertTrue(lenient["ok"])
        self.assertEqual(strict["over_tolerance_ids"], ["a"])
        self.assertFalse(strict["ok"])
        with self.assertRaises(ValueError):
            audio_match_eval.evaluate(fixture, matcher, tolerance=-1.0)

    def test_span_count_mismatch_is_flagged_but_compared_on_available_pairs(self) -> None:
        fixture = _fixture(_items("a"), [[[1.0, 2.0]]])
        matcher = _fake_matcher({"a": [(1.05, 2.05), (3.0, 4.0)]})

        result = audio_match_eval.evaluate(fixture, matcher)

        self.assertEqual(result["count_mismatch_ids"], ["a"])
        self.assertEqual(result["count_mismatch_count"], 1)
        self.assertTrue(result["items"][0]["count_mismatch"])
        self.assertEqual(len(result["items"][0]["actual_spans"]), 2)
        # 只对能配对的区间求误差，多出来的区间不参与误差统计。
        self.assertAlmostEqual(result["paired_span_count"], 1)
        self.assertAlmostEqual(result["items"][0]["max_error"], 0.05)
        self.assertFalse(result["ok"])

    def test_cross_item_overlap_counts_duplicate_bindings(self) -> None:
        fixture = _fixture(
            _items("a", "b"),
            [[[1.0, 2.0], [3.0, 4.0]], [[10.0, 11.0]]],
        )
        # a 播放两遍是正常重复（同一 item 多区间），b 占用了 a 第二遍的前半段。
        overlapping = _fake_matcher({"a": [(1.0, 2.0), (3.0, 4.0)], "b": [(3.5, 10.5)]})
        result = audio_match_eval.evaluate(fixture, overlapping)

        self.assertEqual(result["duplicate_binding_count"], 1)
        binding = result["duplicate_bindings"][0]
        self.assertEqual(sorted(binding["item_ids"]), ["a", "b"])
        self.assertAlmostEqual(binding["overlap_seconds"], 0.5)

        # 同一 item 的重复播放不是“重复绑定”，b 回到自己的区间后应为 0。
        clean = _fake_matcher({"a": [(1.0, 2.0), (3.0, 4.0)], "b": [(10.0, 11.0)]})
        clean_result = audio_match_eval.evaluate(fixture, clean)
        self.assertEqual(clean_result["duplicate_binding_count"], 0)
        self.assertEqual(clean_result["duplicate_bindings"], [])
        self.assertTrue(clean_result["ok"])

    def test_unknown_item_occurrences_are_counted_but_ignored(self) -> None:
        fixture = _fixture(_items("a"), [[[1.0, 2.0]]])
        matcher = _fake_matcher({"ghost": [(1.0, 2.0)]})

        result = audio_match_eval.evaluate(fixture, matcher)

        self.assertEqual(result["missing_ids"], ["a"])
        self.assertEqual(result["unknown_occurrence_count"], 1)
        self.assertEqual(result["occurrence_count"], 1)
        self.assertEqual(result["items"][0]["actual_spans"], [])

    def test_evaluate_is_pure_and_does_not_mutate_fixture(self) -> None:
        fixture = _fixture(_items("a"), [[[1.0, 2.0]]])
        snapshot = deepcopy(fixture)

        audio_match_eval.evaluate(fixture, _fake_matcher({"a": [(1.0, 2.0)]}))

        self.assertEqual(fixture, snapshot)

    def test_load_fixture_normalises_a_real_fixture(self) -> None:
        loaded = audio_match_eval.load_fixture(FIXTURES_DIR / "audio_asr_exam.json")
        self.assertEqual(loaded.name, "audio_asr_exam.json")
        self.assertEqual(len(loaded.items), 16)
        self.assertEqual(len(loaded.spans), len(loaded.speech))
        self.assertEqual(len(loaded.recognized), len(loaded.speech))
        self.assertNotIn("start", loaded.recognized[0])

    def test_load_fixture_rejects_malformed_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / "broken.json"
            broken.write_text(json.dumps({"items": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                audio_match_eval.load_fixture(broken)

            misaligned = Path(directory) / "misaligned.json"
            misaligned.write_text(
                json.dumps(
                    {
                        "items": _items("a", "b"),
                        "speech": [],
                        "expected_spans": [[[0.0, 1.0]]],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                audio_match_eval.load_fixture(misaligned)

        with self.assertRaises(OSError):
            audio_match_eval.load_fixture(Path("/nonexistent/audio_asr_exam.json"))


class RealFixtureSmokeTests(unittest.TestCase):
    def test_real_exam_fixtures_smoke_with_default_matcher(self) -> None:
        for fixture_name, expected_items in REAL_FIXTURES:
            with self.subTest(fixture=fixture_name):
                fixture = audio_match_eval.load_fixture(FIXTURES_DIR / fixture_name)
                result = audio_match_eval.evaluate(fixture)

                # required_count 来自 fixture 本身，与匹配器实现无关。
                self.assertEqual(result["required_count"], expected_items)
                self.assertEqual(len(result["items"]), expected_items)
                self.assertEqual(
                    [row["item_id"] for row in result["items"]],
                    [str(item["item_id"]) for item in fixture.items],
                )
                self.assertEqual(result["tolerance"], audio_match_eval.DEFAULT_TOLERANCE_SECONDS)
                # 匹配器持续优化中：只断言结构自洽，不锁定具体数值。
                self.assertGreater(result["matched_count"], 0)
                self.assertLessEqual(result["matched_count"], expected_items)
                self.assertEqual(
                    result["matched_count"] + len(result["missing_ids"]),
                    expected_items,
                )
                self.assertEqual(result["over_tolerance_count"], len(result["over_tolerance_ids"]))
                self.assertEqual(result["count_mismatch_count"], len(result["count_mismatch_ids"]))
                self.assertGreaterEqual(result["occurrence_count"], 0)
                self.assertGreaterEqual(result["duplicate_binding_count"], 0)
                self.assertIsInstance(result["ok"], bool)
                if result["paired_span_count"]:
                    self.assertGreaterEqual(result["span_error_mean"], 0.0)
                    self.assertGreaterEqual(result["span_error_max"], result["span_error_mean"])
                for row in result["items"]:
                    self.assertEqual(
                        set(row),
                        {
                            "item_id",
                            "sequence",
                            "matched",
                            "expected_spans",
                            "actual_spans",
                            "count_mismatch",
                            "mean_error",
                            "max_error",
                            "over_tolerance",
                        },
                    )

    def test_real_fixture_result_is_json_serialisable(self) -> None:
        fixture = audio_match_eval.load_fixture(FIXTURES_DIR / REAL_FIXTURES[0][0])
        result = audio_match_eval.evaluate(fixture)
        payload = json.dumps({"fixtures": [result]}, ensure_ascii=False)
        decoded = json.loads(payload)
        self.assertEqual(decoded["fixtures"][0]["required_count"], REAL_FIXTURES[0][1])


class TextReportTests(unittest.TestCase):
    def test_report_flags_problems_and_clean_fixtures(self) -> None:
        fixture = _fixture(_items("a", "b"), [[[1.0, 2.0]], [[5.0, 6.0]]])
        problem = audio_match_eval.evaluate(
            fixture, _fake_matcher({"a": [(1.1, 2.1)]})
        )
        clean = audio_match_eval.evaluate(
            fixture, _fake_matcher({"a": [(1.0, 2.0)], "b": [(5.0, 6.0)]})
        )

        problem_text = audio_match_eval.format_report(problem)
        clean_text = audio_match_eval.format_report(clean)

        self.assertIn("required items", problem_text)
        self.assertIn("REVIEW", problem_text)
        self.assertIn("problem items", problem_text)
        self.assertIn("missing", problem_text)
        self.assertIn("b", problem_text)
        self.assertIn("all items matched within tolerance", clean_text)
        self.assertIn("OK", clean_text)


class LazyImportTests(unittest.TestCase):
    def test_importing_the_tool_does_not_load_asr_dependencies(self) -> None:
        script = """
import importlib.util
import sys

module_path = sys.argv[1]
spec = importlib.util.spec_from_file_location("audio_match_eval", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules["audio_match_eval"] = module
spec.loader.exec_module(module)
loaded = [name for name in ("workflow", "workflow.audio_asr", "numpy") if name in sys.modules]
if loaded:
    raise SystemExit("importing the tool must not load " + ", ".join(loaded))
if not callable(module.evaluate) or not callable(module.default_matcher):
    raise SystemExit("missing required callables")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(MODULE_PATH)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class CliTests(unittest.TestCase):
    def _run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MODULE_PATH), *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_json_output_for_a_synthetic_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = Path(directory) / "synthetic.json"
            fixture_path.write_text(
                json.dumps(
                    {
                        "description": "synthetic",
                        "items": [
                            {
                                "item_id": "solo",
                                "sequence": 0,
                                "normalized_content": "The quick brown fox jumps over the lazy dog.",
                            }
                        ],
                        "speech": [{"start": 1.0, "end": 2.0, "text": "unrelated chatter"}],
                        "expected_spans": [[[1.0, 2.0]]],
                    }
                ),
                encoding="utf-8",
            )

            completed = self._run_cli("--json", "--fixture", str(fixture_path))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["tolerance"], audio_match_eval.DEFAULT_TOLERANCE_SECONDS)
        self.assertEqual(len(payload["fixtures"]), 1)
        result = payload["fixtures"][0]
        self.assertEqual(result["required_count"], 1)
        self.assertEqual(result["matched_count"], 0)
        self.assertEqual(result["missing_ids"], ["solo"])
        self.assertFalse(result["ok"])
        self.assertIn("name", result)

    def test_missing_fixture_exits_non_zero(self) -> None:
        completed = self._run_cli("--fixture", "/nonexistent/audio_asr_exam.json")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("无法评测", completed.stderr)


if __name__ == "__main__":
    unittest.main()

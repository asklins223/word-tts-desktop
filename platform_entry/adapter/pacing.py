"""录入速度总控：页面自动化所有“人为停顿”的唯一起点。

调快调慢只改这一个文件里的 ``PACE_SCALE``，或者不改代码用环境变量
``WORDTTS_PLATFORM_INPUT_PACE`` 覆盖：

- ``0`` / ``off``：完全不加停顿，等于本模块引入之前的机器速度；
- ``0.5``：下面的所有区间整体减半；
- ``1``（默认）：按本模块写定的人手节奏；
- ``2``：整体加倍，用于页面反应慢或被限流时。

区间是 scale=1.0 时的毫秒范围，每次停顿都在区间内随机取值，所以同一批
录入不会两次走出同一条时间线。缩放是线性的，档位本身不再拆细项：需要
单独调某个边界时，直接改 ``PAUSE_WINDOWS_MS`` 里那一行。

音频生成链路（``xunfei/``）不使用本模块。
"""

from __future__ import annotations

import os
import random
from typing import Any

from .performance import page_perf

PACE_SCALE_ENV_VAR = "WORDTTS_PLATFORM_INPUT_PACE"

# 总档位。0.0 表示关闭全部停顿。
PACE_SCALE = 1.0

# 每个边界动作完成后的随机停顿区间（毫秒，scale=1.0 时）。
PAUSE_WINDOWS_MS = {
    # 一个输入框或富文本字段填好并回读一致之后。
    "field": (120, 420),
    # 一张题卡（或课文里一条句子卡片）整体填完之后。
    "item": (350, 900),
    # 切换一个题型栏目 / 进入下一段段落之后。
    "group": (700, 1600),
    # 走完一个页面步骤（新增试卷、下一步、保存、一条完整课文）之后。
    "step": (900, 2000),
}

# 轮询等待的每一跳额外随机附加毫秒数。固定 100/200ms 跳点在页面上是
# 一眼可辨的机器心跳，这里只加抖动，不改变任何 timeout_seconds 上限。
POLL_JITTER_MS = (30, 160)

# 单次停顿上限：档位写错时也不会把一次停顿变成几分钟的假死。
MAX_PAUSE_MS = 30_000

_OFF_VALUES = {"0", "-0", "off", "none", "machine"}

_scale: float | None = None
_rng = random.Random()


def resolve_scale(value: Any = None) -> float:
    """把档位输入解析成一个非负倍数。

    ``None`` 表示读环境变量；环境变量缺失或为空时用 ``PACE_SCALE``；
    无法解析的值同样回落到 ``PACE_SCALE``，而不是让一次录入启动失败。
    """

    if value is None:
        raw = os.environ.get(PACE_SCALE_ENV_VAR)
        if raw is None or not raw.strip():
            return PACE_SCALE
    else:
        raw = str(value)
    text = raw.strip()
    if text.casefold() in _OFF_VALUES:
        return 0.0
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        return PACE_SCALE


def scale() -> float:
    """当前生效档位；每个进程只解析一次环境变量。"""

    global _scale
    if _scale is None:
        _scale = resolve_scale()
    return _scale


def configure(*, scale: float | None = None, rng: random.Random | None = None) -> None:
    """设置本进程档位；``scale=None`` 表示重新按环境变量解析。"""

    global _scale, _rng
    _scale = resolve_scale(scale)
    if rng is not None:
        _rng = rng


def reset() -> None:
    """清掉缓存，让下一次调用重新按环境变量解析。"""

    global _scale
    _scale = None


def pause(page: Any, kind: str) -> int:
    """在 ``kind`` 对应的边界动作后停一下，返回实际停的毫秒数。

    走 ``page.wait_for_timeout`` 而不是 ``time.sleep``：录入跑在持有页面
    的线程上，普通 sleep 会阻塞 Playwright 的事件分发。
    """

    current_scale = scale()
    if current_scale <= 0:
        return 0
    low, high = PAUSE_WINDOWS_MS[kind]
    duration_ms = int(
        min(MAX_PAUSE_MS, max(1, _rng.uniform(low, high) * current_scale))
    )
    tracer = page_perf(page, operation="pacing")
    with tracer.span(
        f"pace:{kind}",
        scale=round(current_scale, 3),
        pace_ms=duration_ms,
    ):
        page.wait_for_timeout(duration_ms)
    return duration_ms


def poll_interval_ms(base_ms: int) -> int:
    """给轮询跳点加上随机抖动；关闭档位时原样返回。"""

    base = max(1, int(base_ms))
    current_scale = scale()
    if current_scale <= 0:
        return base
    low, high = POLL_JITTER_MS
    return base + int(_rng.uniform(low, high) * current_scale)


__all__ = [
    "PACE_SCALE",
    "PACE_SCALE_ENV_VAR",
    "PAUSE_WINDOWS_MS",
    "POLL_JITTER_MS",
    "configure",
    "pause",
    "poll_interval_ms",
    "reset",
    "resolve_scale",
    "scale",
]

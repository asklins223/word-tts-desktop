"""多人配音合并音频的安全切割算法。"""


import math

from wordtts.config import (
    COMPOSITE_BOUNDARY_MS,
    COMPOSITE_EDGE_KEEP_MS,
    COMPOSITE_EDGE_TRIM_MIN_MS,
    COMPOSITE_MARKER_MIN_CORE_MS,
    COMPOSITE_MARKER_STRONG_MIN_CORE_MS,
    COMPOSITE_MARKER_TARGET_TOLERANCE_MS,
    COMPOSITE_MIN_CORE_SILENCE_MS,
    COMPOSITE_MIN_OUTPUT_MS,
    COMPOSITE_MIN_SAFE_SILENCE_MS,
    COMPOSITE_OUTER_EDGE_KEEP_MS,
    COMPOSITE_OUTER_EDGE_TRIM_MIN_MS,
    COMPOSITE_SILENCE_CORE_DBFS,
    COMPOSITE_SILENCE_EDGE_DBFS,
    COMPOSITE_SILENCE_FRAME_MS,
)


class CompositeCutError(RuntimeError):
    """合并音频缺少可验证的安全切割边界。"""


def _audio_dbfs(audio):
    """返回可比较的 dBFS；完全静音和空音频统一视为很低能量。"""
    try:
        value = float(audio.dBFS)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return -100.0
    return value if math.isfinite(value) else -100.0


def _find_composite_silence_runs(
    audio,
    *,
    frame_ms=COMPOSITE_SILENCE_FRAME_MS,
    core_dbfs=COMPOSITE_SILENCE_CORE_DBFS,
    edge_dbfs=COMPOSITE_SILENCE_EDGE_DBFS,
):
    """找出合并音频中的低能量候选区。

    先用更低的 core 阈值确认“确实静音”，再用较宽的 edge 阈值扩展候选区。
    这样切点不会落在静音刚开始或刚结束的边缘，能给首音、尾音留保护空间。
    """
    duration = len(audio)
    if duration <= 0:
        return []
    frame_ms = max(10, int(frame_ms))
    frames = []
    for start in range(0, duration, frame_ms):
        end = min(duration, start + frame_ms)
        dbfs = _audio_dbfs(audio[start:end])
        frames.append({
            "start": start,
            "end": end,
            "dbfs": dbfs,
            "core": dbfs <= core_dbfs,
            "edge": dbfs <= edge_dbfs,
        })

    runs = []
    index = 0
    while index < len(frames):
        if not frames[index]["core"]:
            index += 1
            continue
        core_start = index
        while index + 1 < len(frames) and frames[index + 1]["core"]:
            index += 1
        core_end = index

        left = core_start
        while left > 0 and frames[left - 1]["edge"]:
            left -= 1
        right = core_end
        while right + 1 < len(frames) and frames[right + 1]["edge"]:
            right += 1

        core_start_ms = frames[core_start]["start"]
        core_end_ms = frames[core_end]["end"]
        start_ms = frames[left]["start"]
        end_ms = frames[right]["end"]
        core_length = max(0, core_end_ms - core_start_ms)
        safe_length = max(0, end_ms - start_ms)
        if (
            core_length >= COMPOSITE_MIN_CORE_SILENCE_MS
            and safe_length >= COMPOSITE_MIN_SAFE_SILENCE_MS
        ):
            runs.append({
                "start": start_ms,
                "end": end_ms,
                # 扩展后的 safe 区可能因为首尾弱音而左右不对称；真正的
                # 切点固定落在 core 静音中心，避免把尾音/首音带进切点。
                "center": (core_start_ms + core_end_ms) // 2,
                "cut_position": (core_start_ms + core_end_ms) // 2,
                "core_start": core_start_ms,
                "core_end": core_end_ms,
                "core_length": core_length,
                "length": safe_length,
            })
        index += 1
    return runs


def _select_composite_silence_runs(
    audio,
    runs,
    boundary_count,
    item_lengths=None,
    diagnostics=None,
):
    """全局选择每个条目之间的安全停顿，不按比例猜测切点。

    旧实现按边界逐个贪心选择：前面某个自然停顿一旦被选中，后面的
    expected position 就会整体错位，表现为前两段正常、第三段开始音频
    对不上。现在使用全局动态规划同时选择整条有序候选链，并在候选足够
    时优先保留页面插入的长停顿标记。
    """
    if isinstance(diagnostics, dict):
        context = {
            key: diagnostics[key]
            for key in ("item_count", "detected_run_count")
            if key in diagnostics
        }
        diagnostics.clear()
        diagnostics.update(context)
        diagnostics.update({
            "boundary_count": max(0, int(boundary_count or 0)),
            "total_duration_ms": max(0, len(audio) if audio is not None else 0),
        })

    if boundary_count <= 0:
        return []
    if len(runs) < boundary_count:
        if isinstance(diagnostics, dict):
            diagnostics.update({
                "candidate_count": len(runs),
                "strategy": "insufficient_candidates",
            })
        raise CompositeCutError(
            f"合并音频只找到 {len(runs)} 个安全停顿，需要 {boundary_count} 个"
        )

    total_duration = max(1, len(audio))
    expected_positions = []
    lengths = [max(1, int(value or 0)) for value in (item_lengths or [])]
    if len(lengths) == boundary_count + 1 and sum(lengths) > 0:
        accumulated = 0
        total_length = sum(lengths)
        for value in lengths[:-1]:
            accumulated += value
            expected_positions.append(round(total_duration * accumulated / total_length))
    else:
        expected_positions = [
            round(total_duration * index / (boundary_count + 1))
            for index in range(1, boundary_count + 1)
        ]

    ordered_runs = sorted(
        (
            run for run in runs
            if run["start"] > 0 and run["end"] < total_duration
        ),
        key=lambda run: run["center"],
    )
    if len(ordered_runs) < boundary_count:
        if isinstance(diagnostics, dict):
            diagnostics.update({
                "candidate_count": len(ordered_runs),
                "strategy": "insufficient_candidates",
            })
        raise CompositeCutError(
            f"合并音频只找到 {len(ordered_runs)} 个内部安全停顿，需要 {boundary_count} 个"
        )

    long_marker_runs = [
        run for run in ordered_runs
        if float(run.get("core_length", run.get("length", 0)) or 0)
        >= COMPOSITE_MARKER_MIN_CORE_MS
    ]
    strong_marker_runs = [
        run for run in long_marker_runs
        if (
            float(run.get("core_length", run.get("length", 0)) or 0)
            >= COMPOSITE_MARKER_STRONG_MIN_CORE_MS
            and abs(
                float(run.get("core_length", run.get("length", 0)) or 0)
                - COMPOSITE_BOUNDARY_MS
            )
            <= COMPOSITE_MARKER_TARGET_TOLERANCE_MS
        )
    ]
    if isinstance(diagnostics, dict):
        diagnostics.update({
            "candidate_count": len(ordered_runs),
            "long_marker_count": len(long_marker_runs),
            "strong_marker_count": len(strong_marker_runs),
        })

    if len(strong_marker_runs) == boundary_count:
        ordered_runs = strong_marker_runs
        selection_strategy = "strong_markers"
    elif len(strong_marker_runs) > boundary_count:
        if isinstance(diagnostics, dict):
            diagnostics["strategy"] = "ambiguous_or_extra_markers"
        raise CompositeCutError(
            "人工停顿标记数量存在歧义："
            f"需要 {boundary_count} 个，检测到 {len(strong_marker_runs)} 个"
            "接近 2 秒的强标记；拒绝猜测边界"
        )
    elif len(long_marker_runs) == boundary_count:
        # 兼容音频编码后 2 秒停顿的 core 被压短的情况；候选数必须刚好
        # 等于边界数，避免把额外的自然长停顿当成定位标记。
        ordered_runs = long_marker_runs
        selection_strategy = "exact_long_markers"
    else:
        if isinstance(diagnostics, dict):
            diagnostics["strategy"] = "ambiguous_or_missing_markers"
        raise CompositeCutError(
            "人工停顿标记不足或存在歧义："
            f"需要 {boundary_count} 个，"
            f"强标记 {len(strong_marker_runs)} 个，"
            f"长停顿候选 {len(long_marker_runs)} 个，"
            f"全部安全候选 {len(ordered_runs)} 个；拒绝按自然停顿猜测"
        )

    def score(run, expected):
        core_length = float(run.get("core_length", run.get("length", 0)) or 0)
        safe_length = float(run.get("length", core_length) or core_length)
        # 页面标记不只要“长”，还应接近实际插入的 2 秒；这样正文中
        # 偶然出现的 1.5 秒长停顿不会轻易压过真正的定位标记。
        length_score = min(core_length, COMPOSITE_BOUNDARY_MS * 1.5) * 2.0
        edge_score = min(safe_length, COMPOSITE_BOUNDARY_MS * 1.5) * 0.5
        target_penalty = min(
            abs(core_length - COMPOSITE_BOUNDARY_MS),
            COMPOSITE_BOUNDARY_MS * 2,
        ) * 1.5
        distance_penalty = abs(run["center"] - expected) / total_duration * 300.0
        return length_score + edge_score - target_penalty - distance_penalty

    # states[run_index] = (累计分数, 已选择的候选索引路径)，表示当前边界
    # 选择该候选时的最优前缀。候选数量通常很小，完整保留状态能避免贪心
    # 选早了一个自然停顿后把后续边界全部推偏。
    states = []
    for boundary_index, expected in enumerate(expected_positions):
        next_states = [None] * len(ordered_runs)
        for run_index, run in enumerate(ordered_runs):
            best = None
            current_score = score(run, expected)
            if boundary_index == 0:
                best = (current_score, [run_index])
            else:
                for previous_index, previous_state in enumerate(states):
                    if previous_state is None or previous_index >= run_index:
                        continue
                    previous_run = ordered_runs[previous_index]
                    if run["center"] - previous_run["center"] < COMPOSITE_MIN_OUTPUT_MS:
                        continue
                    candidate = (
                        previous_state[0] + current_score,
                        [*previous_state[1], run_index],
                    )
                    if best is None or candidate[0] > best[0]:
                        best = candidate
            next_states[run_index] = best
        states = next_states

    candidates = [state for state in states if state is not None]
    if not candidates:
        raise CompositeCutError("安全停顿顺序不连续，拒绝按比例强行切割")
    selected_path = max(candidates, key=lambda state: state[0])[1]
    selected = [ordered_runs[index] for index in selected_path]

    if isinstance(diagnostics, dict):
        diagnostics.update({
            "strategy": selection_strategy,
            "selected_count": len(selected),
            "selected_centers": [int(run["center"]) for run in selected],
            "selected_core_lengths": [
                int(run.get("core_length", run.get("length", 0)) or 0)
                for run in selected
            ],
        })

    if any(
        right["center"] - left["center"] < COMPOSITE_MIN_OUTPUT_MS
        for left, right in zip(selected, selected[1:])
    ):
        raise CompositeCutError("候选停顿之间的音频过短，无法安全恢复题目")
    return selected


def _edge_silence_length(
    audio,
    *,
    leading,
    dbfs_threshold=COMPOSITE_SILENCE_CORE_DBFS,
):
    """返回音频首部或尾部连续的低能量长度。

    首尾整理使用更严格的 core 阈值，不把低音量尾音或弱首辅音误判成
    可删除的保护空档。
    """
    duration = len(audio)
    if duration <= 0:
        return 0
    frame_ms = max(10, COMPOSITE_SILENCE_FRAME_MS)
    starts = range(0, duration, frame_ms)
    frames = [
        _audio_dbfs(audio[start:min(duration, start + frame_ms)])
        <= dbfs_threshold
        for start in starts
    ]
    if leading:
        count = 0
        for is_silent in frames:
            if not is_silent:
                break
            count += 1
        return min(duration, count * frame_ms)
    count = 0
    for is_silent in reversed(frames):
        if not is_silent:
            break
        count += 1
    return min(duration, count * frame_ms)


def _trim_composite_edge_silence(
    audio,
    *,
    trim_leading=True,
    trim_trailing=True,
    leading_is_outer=False,
    trailing_is_outer=False,
):
    """去掉边界人工停顿残留，同时保护真实首音和尾音。

    内部切点两侧已知来自人工 break，因此可以在较短阈值下整理；合并
    作品最外层没有这个确定性，只处理明显过长的静音，并保留更长保护。
    """
    leading = _edge_silence_length(audio, leading=True)
    trailing = _edge_silence_length(audio, leading=False)
    start = 0
    end = len(audio)
    leading_min = (
        COMPOSITE_OUTER_EDGE_TRIM_MIN_MS
        if leading_is_outer
        else COMPOSITE_EDGE_TRIM_MIN_MS
    )
    leading_keep = (
        COMPOSITE_OUTER_EDGE_KEEP_MS
        if leading_is_outer
        else COMPOSITE_EDGE_KEEP_MS
    )
    trailing_min = (
        COMPOSITE_OUTER_EDGE_TRIM_MIN_MS
        if trailing_is_outer
        else COMPOSITE_EDGE_TRIM_MIN_MS
    )
    trailing_keep = (
        COMPOSITE_OUTER_EDGE_KEEP_MS
        if trailing_is_outer
        else COMPOSITE_EDGE_KEEP_MS
    )
    if trim_leading and leading >= leading_min:
        start = min(max(0, leading - leading_keep), end)
    if trim_trailing and trailing >= trailing_min:
        end = max(start, end - max(0, trailing - trailing_keep))
    trimmed = audio[start:end]
    if len(trimmed) < COMPOSITE_MIN_OUTPUT_MS:
        raise CompositeCutError("切割后得到的音频段过短，可能存在首尾边界异常")
    return trimmed


def cut_composite_audio(audio, item_count, item_lengths=None, diagnostics=None):
    """按多人配音作品中的人工停顿恢复每道题的音频。

    切点只允许落在通过双阈值静音检测的停顿中；找不到足够安全的停顿时
    抛出 CompositeCutError，由上层保留合并音频并提示用户，不按时长比例
    猜测边界。
    """
    count = int(item_count or 0)
    if audio is None or count <= 0:
        raise CompositeCutError("没有可切割的合并音频或题目数量")
    if isinstance(diagnostics, dict):
        diagnostics.clear()
        diagnostics.update({
            "item_count": count,
            "total_duration_ms": len(audio),
        })
    if count == 1:
        # 单题作品没有内部 break 可供定位，但讯飞仍可能在作品最外层
        # 留下较长首尾空档；沿用外层保护规则，避免默认合并模式下单题
        # 音频与单条生成相比出现明显的首尾停顿。
        pieces = [_trim_composite_edge_silence(
            audio,
            leading_is_outer=True,
            trailing_is_outer=True,
        )]
        if isinstance(diagnostics, dict):
            diagnostics.update({
                "strategy": "outer_edge_trim",
                "selected_count": 0,
                "piece_lengths": [len(pieces[0])],
            })
        return pieces

    runs = _find_composite_silence_runs(audio)
    if isinstance(diagnostics, dict):
        diagnostics["detected_run_count"] = len(runs)
    selected = _select_composite_silence_runs(
        audio,
        runs,
        count - 1,
        item_lengths=item_lengths,
        diagnostics=diagnostics,
    )
    cut_positions = [
        int(run.get("cut_position", run["center"]))
        for run in selected
    ]
    pieces = []
    start = 0
    for piece_index, cut_position in enumerate([*cut_positions, len(audio)]):
        piece = audio[start:cut_position]
        pieces.append(_trim_composite_edge_silence(
            piece,
            # 切点内部的这一侧是人工 break 的残留；真正作品首尾使用
            # 更高阈值，避免误伤弱首音或自然尾音。
            trim_leading=True,
            trim_trailing=True,
            leading_is_outer=piece_index == 0,
            trailing_is_outer=piece_index == count - 1,
        ))
        start = cut_position
    if len(pieces) != count:
        raise CompositeCutError(
            f"安全切割段数异常：期望 {count}，实际 {len(pieces)}"
        )
    if isinstance(diagnostics, dict):
        diagnostics["piece_lengths"] = [len(piece) for piece in pieces]
    return pieces


def format_composite_cut_diagnostics(diagnostics):
    """把合并切割诊断压缩成可读日志，不改变结构化诊断原数据。"""
    if (
        not isinstance(diagnostics, dict)
        or not diagnostics
        or not any(key in diagnostics for key in ("item_count", "strategy"))
    ):
        return ""

    def as_int(key):
        try:
            return int(diagnostics.get(key) or 0)
        except (TypeError, ValueError, OverflowError):
            return 0

    strategy = str(diagnostics.get("strategy") or "unknown")
    boundary_count = as_int("boundary_count")
    candidate_count = as_int("candidate_count")
    long_count = as_int("long_marker_count")
    strong_count = as_int("strong_marker_count")
    selected_count = as_int("selected_count")
    centers = diagnostics.get("selected_centers")
    if isinstance(centers, (list, tuple)):
        preview = [str(value) for value in centers[:4]]
        if len(centers) > 8:
            preview.append("…")
            preview.extend(str(value) for value in centers[-4:])
        elif len(centers) > 4:
            preview.extend(str(value) for value in centers[4:])
        center_text = ",".join(preview) or "无"
    else:
        center_text = "无"
    return (
        f"切割诊断：策略={strategy}，需要边界={boundary_count}，"
        f"候选={candidate_count}，长停顿={long_count}，强标记={strong_count}，"
        f"已选={selected_count}，中心位置(ms)=[{center_text}]"
    )

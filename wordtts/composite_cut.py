"""多人配音合并音频的安全切割算法。"""


import math

from wordtts.config import (
    COMPOSITE_BOUNDARY_MS,
    COMPOSITE_EDGE_KEEP_MS,
    COMPOSITE_EDGE_TRIM_MIN_MS,
    COMPOSITE_MAX_EXTRA_LONG_MARKERS,
    COMPOSITE_MARKER_ALIGNMENT_MAX_ERROR_RATIO,
    COMPOSITE_MARKER_ALIGNMENT_MIN_MARGIN,
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


def _composite_core_length(run):
    """Return one candidate's strict silent core length as a finite float."""
    try:
        value = float(run.get("core_length", run.get("length", 0)) or 0)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _expected_composite_boundary_positions(
    total_duration,
    boundary_count,
    item_lengths=None,
):
    """Estimate marker positions using text time plus the known pause budget.

    A simple ``text_ratio * total_duration`` estimate is biased because the
    total duration already includes one inserted pause for every boundary. It
    puts the first marker too early and becomes increasingly wrong later in a
    long work.  Removing the expected pause budget first keeps the estimate
    useful for choosing between one real marker and one natural long gap; it
    is never used as a cut point by itself.
    """
    count = max(0, int(boundary_count or 0))
    duration = max(1, int(total_duration or 0))
    if count <= 0:
        return []

    lengths = []
    for value in item_lengths or []:
        try:
            length = int(value or 0)
        except (TypeError, ValueError, OverflowError):
            length = 0
        lengths.append(max(1, length))

    # When the audio is shorter than the nominal pause budget it is already
    # malformed; use a monotonic uniform estimate only for diagnostics and let
    # the candidate checks reject it later.
    pause_budget = count * COMPOSITE_BOUNDARY_MS
    if duration > pause_budget and pause_budget > 0:
        speech_duration = duration - pause_budget
        pause_ms = COMPOSITE_BOUNDARY_MS
    else:
        speech_duration = duration
        pause_ms = 0

    if len(lengths) == count + 1 and sum(lengths) > 0:
        total_length = sum(lengths)
        accumulated = 0
        positions = []
        for index, length in enumerate(lengths[:-1]):
            accumulated += length
            positions.append(
                round(speech_duration * accumulated / total_length
                      + (index + 1) * pause_ms)
            )
        return positions

    return [
        round(speech_duration * index / (count + 1) + index * pause_ms)
        for index in range(1, count + 1)
    ]


def _composite_boundary_alignment_tolerances(
    total_duration,
    boundary_count,
    item_lengths=None,
):
    """Return conservative per-boundary tolerances for candidate alignment."""
    count = max(0, int(boundary_count or 0))
    duration = max(1, int(total_duration or 0))
    if count <= 0:
        return []
    pause_budget = count * COMPOSITE_BOUNDARY_MS
    speech_duration = max(1, duration - pause_budget) if duration > pause_budget else duration
    lengths = []
    for value in item_lengths or []:
        try:
            length = int(value or 0)
        except (TypeError, ValueError, OverflowError):
            length = 0
        lengths.append(max(1, length))

    if len(lengths) == count + 1 and sum(lengths) > 0:
        total_length = sum(lengths)
        item_durations = [speech_duration * length / total_length for length in lengths[:-1]]
    else:
        item_durations = [speech_duration / (count + 1)] * count

    # Character counts are only a rough timing model. A generous lower bound
    # handles short prompts, while the cap prevents a long essay from making
    # an unrelated natural gap look acceptable.
    return [
        max(2200.0, min(15000.0, item_duration * 0.65 + 1200.0))
        for item_duration in item_durations
    ]


def _rank_composite_marker_paths(
    runs,
    expected_positions,
    tolerances,
    boundary_count,
):
    """Return the best two ordered long-marker paths.

    Keeping two paths per state is enough to detect an ambiguous extra marker
    while avoiding the combinatorial cost of enumerating every subsequence.
    Each path contains only candidates that passed the strict long-silence
    gate; position estimates merely disambiguate those already-safe regions.
    """
    count = max(0, int(boundary_count or 0))
    if count <= 0 or len(runs) < count:
        return []

    def score(run, expected, tolerance):
        position_error = abs(float(run["center"]) - float(expected))
        position_penalty = position_error / max(float(tolerance), 1.0)
        duration_penalty = min(
            abs(_composite_core_length(run) - COMPOSITE_BOUNDARY_MS)
            / max(float(COMPOSITE_BOUNDARY_MS), 1.0),
            1.5,
        )
        # Position is the primary discriminator; duration only breaks ties
        # between already long candidates and cannot manufacture a boundary.
        return -(position_penalty + duration_penalty * 0.18)

    def keep_top_two(values):
        unique = {}
        for value in values:
            path = tuple(value[1])
            previous = unique.get(path)
            if previous is None or value[0] > previous[0]:
                unique[path] = value
        return sorted(unique.values(), key=lambda value: value[0], reverse=True)[:2]

    states = [[] for _ in runs]
    for run_index, run in enumerate(runs):
        states[run_index] = [
            (score(run, expected_positions[0], tolerances[0]), [run_index])
        ]

    for boundary_index in range(1, count):
        next_states = [[] for _ in runs]
        for run_index, run in enumerate(runs):
            options = []
            current_score = score(
                run,
                expected_positions[boundary_index],
                tolerances[boundary_index],
            )
            for previous_index in range(run_index):
                previous_run = runs[previous_index]
                if run["center"] - previous_run["center"] < COMPOSITE_MIN_OUTPUT_MS:
                    continue
                for previous_score, previous_path in states[previous_index]:
                    options.append((
                        previous_score + current_score,
                        [*previous_path, run_index],
                    ))
            next_states[run_index] = keep_top_two(options)
        states = next_states

    final_paths = []
    for state in states:
        final_paths.extend(state)
    return keep_top_two(final_paths)


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
    expected_positions = _expected_composite_boundary_positions(
        total_duration,
        boundary_count,
        item_lengths,
    )
    alignment_tolerances = _composite_boundary_alignment_tolerances(
        total_duration,
        boundary_count,
        item_lengths,
    )

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
        if _composite_core_length(run) >= COMPOSITE_MARKER_MIN_CORE_MS
    ]
    strong_marker_runs = [
        run for run in long_marker_runs
        if (
            _composite_core_length(run) >= COMPOSITE_MARKER_STRONG_MIN_CORE_MS
            and abs(
                _composite_core_length(run)
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
        marker_runs = strong_marker_runs
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
        # 兼容音频编码后 2 秒停顿的 core 被压短的情况；候选数刚好等于
        # 边界数时不需要在自然停顿中作选择。
        marker_runs = long_marker_runs
        selection_strategy = "exact_long_markers"
    elif len(long_marker_runs) > boundary_count:
        extra_count = len(long_marker_runs) - boundary_count
        if extra_count > COMPOSITE_MAX_EXTRA_LONG_MARKERS:
            if isinstance(diagnostics, dict):
                diagnostics["strategy"] = "ambiguous_or_extra_markers"
            raise CompositeCutError(
                "人工停顿标记数量存在歧义："
                f"需要 {boundary_count} 个，长停顿候选 {len(long_marker_runs)} 个；"
                "额外候选过多，拒绝猜测边界"
            )
        # 有些 2 秒标记会被讯飞 MP3 编码压短，正文又可能出现一个自然
        # 长停顿。把这类候选交给“按已知题目序列对齐”的全局选择器，只有
        # 最佳路径明显优于次佳路径且每个位置误差可接受时才继续。
        marker_runs = long_marker_runs
        selection_strategy = "aligned_long_markers"
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

    ranked_paths = _rank_composite_marker_paths(
        marker_runs,
        expected_positions,
        alignment_tolerances,
        boundary_count,
    )
    if not ranked_paths:
        if isinstance(diagnostics, dict):
            diagnostics["strategy"] = "non_contiguous_markers"
        raise CompositeCutError("安全停顿顺序不连续，拒绝按比例强行切割")

    best_score, selected_path = ranked_paths[0]
    selected = [marker_runs[index] for index in selected_path]

    if selection_strategy == "aligned_long_markers":
        position_errors = [
            abs(float(run["center"]) - float(expected))
            for run, expected in zip(selected, expected_positions)
        ]
        max_position_error = max(position_errors or [0.0])
        max_error_ratio = max(
            (
                error / max(float(tolerance), 1.0)
                for error, tolerance in zip(position_errors, alignment_tolerances)
            ),
            default=0.0,
        )
        second_score = ranked_paths[1][0] if len(ranked_paths) > 1 else None
        margin = (
            best_score - second_score
            if second_score is not None
            else None
        )
        if isinstance(diagnostics, dict):
            diagnostics.update({
                "selection_margin": margin,
                "max_position_error_ms": int(round(max_position_error)),
                "max_position_error_ratio": round(max_error_ratio, 4),
                "expected_centers": [int(value) for value in expected_positions],
                "skipped_long_centers": [
                    int(run["center"])
                    for index, run in enumerate(marker_runs)
                    if index not in selected_path
                ],
            })
        if (
            (margin is not None and margin < COMPOSITE_MARKER_ALIGNMENT_MIN_MARGIN)
            or max_error_ratio > COMPOSITE_MARKER_ALIGNMENT_MAX_ERROR_RATIO
        ):
            if isinstance(diagnostics, dict):
                diagnostics["strategy"] = "ambiguous_or_missing_markers"
            raise CompositeCutError(
                "人工停顿标记对齐存在歧义："
                f"需要 {boundary_count} 个，长停顿候选 {len(long_marker_runs)} 个，"
                f"最佳路径边距 {margin if margin is not None else '无'}，"
                f"最大位置误差 {int(round(max_position_error))}ms；拒绝猜测边界"
            )

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

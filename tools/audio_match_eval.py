#!/usr/bin/env python3
"""用真实真题 fixture 量化评测“连续音频切题”匹配算法。

音频导入算法的质量靠真题复现，而不是靠用户发现漏题。本工具把两份真实
SenseVoice 识别 fixture（``tests/fixtures/audio_asr_exam*.json``）喂给当前
匹配器，输出可对比的指标：

- 题目召回：required_count / matched_count / missing_ids；
- 重复绑定：不同题目占用同一段语音（跨 item 区间重叠）的数量；
- 跨度误差：每题实际切割区间与 ``expected_spans`` 的数量与逐区间误差
  （起止偏差的 mean/max，以及超过 ``--tolerance`` 的题目）。

匹配器默认惰性导入 ``workflow.audio_asr._continuous_audio_matches``；导入
本模块不会加载 ASR 模型、不访问网络，也不会读取 fixture。测试可以给
``evaluate(fixture, matcher)`` 注入任意 matcher 来验证指标数学。

用法::

    .venv/bin/python tools/audio_match_eval.py
    .venv/bin/python tools/audio_match_eval.py --json
    .venv/bin/python tools/audio_match_eval.py --tolerance 0.25 --verbose
    .venv/bin/python tools/audio_match_eval.py --fixture tests/fixtures/audio_asr_exam.json

退出码：正常评测（即使有漏题/误差）为 0；fixture 读取、解析或匹配器导入
失败时为 1。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES_DIR = ROOT / "tests" / "fixtures"
DEFAULT_FIXTURES = (
    FIXTURES_DIR / "audio_asr_exam.json",
    FIXTURES_DIR / "audio_asr_exam_shenzhen_2024_set2.json",
)
DEFAULT_TOLERANCE_SECONDS = 0.5
# 只有明确的时间重叠才算“同一片段被两题占用”，避免相邻区间因四舍五入
# 产生 0.000x 秒的伪重叠。
OVERLAP_EPSILON_SECONDS = 0.01

# (recognized, spans, items) -> occurrences
Matcher = Callable[
    [Sequence[Mapping[str, Any]], Sequence[tuple[float, float]], Sequence[Mapping[str, Any]]],
    Sequence[Mapping[str, Any]],
]


@dataclass(frozen=True)
class Fixture:
    """一份真题 fixture 的规范化视图（items 与 expected_spans 顺序对齐）。"""

    items: tuple[Mapping[str, Any], ...]
    speech: tuple[Mapping[str, Any], ...]
    expected_spans: tuple[tuple[tuple[float, float], ...], ...]
    name: str = ""
    path: str = ""
    description: str = ""

    @property
    def recognized(self) -> list[dict[str, str]]:
        """匹配器要求的 ``recognized`` 视图：只暴露识别文本。"""
        return [{"text": str(row.get("text") or "")} for row in self.speech]

    @property
    def spans(self) -> list[tuple[float, float]]:
        return [
            (float(row["start"]), float(row["end"]))
            for row in self.speech
        ]


def _as_float(value: Any, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} 不是有效数字：{value!r}") from exc


def _coerce_fixture(value: Any, *, name: str = "", path: str = "") -> Fixture:
    """把原始 JSON dict（或已规范的 Fixture）转成 Fixture，并做结构校验。"""
    if isinstance(value, Fixture):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("fixture 必须是 JSON 对象或 Fixture 实例")

    missing = [key for key in ("items", "speech", "expected_spans") if key not in value]
    if missing:
        raise ValueError(f"fixture 缺少字段：{', '.join(missing)}")

    items = tuple(dict(item) for item in value["items"])
    speech = tuple(dict(row) for row in value["speech"])
    for index, item in enumerate(items):
        if not str(item.get("item_id") or ""):
            raise ValueError(f"fixture.items[{index}] 缺少 item_id")
    for index, row in enumerate(speech):
        if "start" not in row or "end" not in row:
            raise ValueError(f"fixture.speech[{index}] 缺少 start/end")
        _as_float(row["start"], f"fixture.speech[{index}].start")
        _as_float(row["end"], f"fixture.speech[{index}].end")

    raw_expected = value["expected_spans"]
    if len(raw_expected) != len(items):
        raise ValueError(
            f"expected_spans 数量 {len(raw_expected)} 与 items 数量 {len(items)} 不一致"
        )
    expected_spans: list[tuple[tuple[float, float], ...]] = []
    for index, per_item in enumerate(raw_expected):
        pairs: list[tuple[float, float]] = []
        for pair in per_item:
            if len(pair) != 2:
                raise ValueError(f"expected_spans[{index}] 的区间必须是 [start, end]")
            pairs.append(
                (
                    _as_float(pair[0], f"expected_spans[{index}].start"),
                    _as_float(pair[1], f"expected_spans[{index}].end"),
                )
            )
        expected_spans.append(tuple(pairs))

    return Fixture(
        items=items,
        speech=speech,
        expected_spans=tuple(expected_spans),
        name=name or str(value.get("name") or ""),
        path=path,
        description=str(value.get("description") or ""),
    )


def load_fixture(path: str | Path) -> Fixture:
    """读取 JSON fixture 并规范化；不做任何匹配。"""
    fixture_path = Path(path)
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    return _coerce_fixture(
        raw,
        name=fixture_path.name,
        path=str(fixture_path),
    )


def default_matcher(
    recognized: Sequence[Mapping[str, Any]],
    spans: Sequence[tuple[float, float]],
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """惰性导入仓库里的连续音频匹配器。

    模块导入时不触发该 import，避免加载 ASR 依赖或模型。
    """
    from workflow.audio_asr import _continuous_audio_matches

    return _continuous_audio_matches(recognized, spans, items)


def _group_occurrences(
    occurrences: Sequence[Mapping[str, Any]],
    known_ids: set[str],
) -> tuple[dict[str, list[tuple[float, float]]], int, int]:
    """按 top_item_id 分组并排序；返回 (分组区间, occurrence 数, 未知 item 数)。"""
    grouped: dict[str, list[tuple[float, float]]] = {item_id: [] for item_id in known_ids}
    unknown = 0
    for occurrence in occurrences:
        if not isinstance(occurrence, Mapping):
            raise TypeError(f"matcher 返回的 occurrence 必须是映射：{occurrence!r}")
        item_id = str(occurrence.get("top_item_id") or "")
        try:
            start = float(occurrence["start"])
            end = float(occurrence["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"matcher 返回的 occurrence 缺少有效 start/end：{occurrence!r}"
            ) from exc
        if item_id not in grouped:
            unknown += 1
            continue
        grouped[item_id].append((start, end))
    for intervals in grouped.values():
        intervals.sort()
    return grouped, len(occurrences), unknown


def _cross_item_overlaps(
    grouped: Mapping[str, Sequence[tuple[float, float]]],
) -> list[dict[str, Any]]:
    """统计“同一段语音被两个不同题目占用”的区间对（跨 item 重叠）。"""
    flat = [
        (item_id, start, end)
        for item_id, intervals in grouped.items()
        for start, end in intervals
    ]
    overlaps: list[dict[str, Any]] = []
    for index, (left_id, left_start, left_end) in enumerate(flat):
        for right_id, right_start, right_end in flat[index + 1:]:
            if left_id == right_id:
                continue
            overlap = min(left_end, right_end) - max(left_start, right_start)
            if overlap > OVERLAP_EPSILON_SECONDS:
                overlaps.append(
                    {
                        "item_ids": [left_id, right_id],
                        "overlap_seconds": round(overlap, 6),
                    }
                )
    return overlaps


def evaluate(
    fixture: Fixture | Mapping[str, Any],
    matcher: Matcher | None = None,
    *,
    tolerance: float = DEFAULT_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    """对一份 fixture 求指标；纯函数，不改入参、不打印、不读写文件。

    ``matcher`` 省略时惰性导入仓库默认匹配器；测试可注入假 matcher，
    其签名与 ``_continuous_audio_matches(recognized, spans, items)`` 一致。
    """
    if tolerance < 0:
        raise ValueError("tolerance 不能为负数")
    resolved = _coerce_fixture(fixture)
    match = default_matcher if matcher is None else matcher
    if not callable(match):
        raise TypeError("matcher 必须可调用")

    # 传入副本，保证 fake / 真实 matcher 的原地修改不会污染调用方 fixture。
    occurrences = list(
        match(
            resolved.recognized,
            resolved.spans,
            [dict(item) for item in resolved.items],
        )
    )
    grouped, occurrence_count, unknown_occurrence_count = _group_occurrences(
        occurrences, {str(item.get("item_id") or "") for item in resolved.items}
    )

    items: list[dict[str, Any]] = []
    missing_ids: list[str] = []
    count_mismatch_ids: list[str] = []
    over_tolerance_ids: list[str] = []
    pair_errors: list[float] = []
    start_errors: list[float] = []
    end_errors: list[float] = []

    for index, item in enumerate(resolved.items):
        item_id = str(item.get("item_id") or "")
        expected = resolved.expected_spans[index]
        actual = list(grouped.get(item_id, ()))
        expected_list = [tuple(span) for span in expected]
        matched = bool(actual)
        if not matched:
            missing_ids.append(item_id)

        count_mismatch = len(actual) != len(expected_list)
        if count_mismatch:
            count_mismatch_ids.append(item_id)

        item_start_errors: list[float] = []
        item_end_errors: list[float] = []
        for (actual_start, actual_end), (expected_start, expected_end) in zip(
            actual, expected_list
        ):
            start_error = abs(actual_start - expected_start)
            end_error = abs(actual_end - expected_end)
            item_start_errors.append(start_error)
            item_end_errors.append(end_error)
            start_errors.append(start_error)
            end_errors.append(end_error)
            pair_errors.append(max(start_error, end_error))

        item_errors = [
            max(start_error, end_error)
            for start_error, end_error in zip(item_start_errors, item_end_errors)
        ]
        over_tolerance = any(error > tolerance for error in item_errors)
        if over_tolerance:
            over_tolerance_ids.append(item_id)

        items.append(
            {
                "item_id": item_id,
                "sequence": int(item.get("sequence") or 0),
                "matched": matched,
                "expected_spans": [list(span) for span in expected_list],
                "actual_spans": [list(span) for span in actual],
                "count_mismatch": count_mismatch,
                "mean_error": mean(item_errors) if item_errors else None,
                "max_error": max(item_errors) if item_errors else None,
                "over_tolerance": over_tolerance,
            }
        )

    overlaps = _cross_item_overlaps(grouped)
    required_count = len(resolved.items)
    matched_count = sum(1 for item in items if item["matched"])

    return {
        "name": resolved.name,
        "path": resolved.path,
        "description": resolved.description,
        "tolerance": float(tolerance),
        "required_count": required_count,
        "matched_count": matched_count,
        "missing_count": len(missing_ids),
        "missing_ids": missing_ids,
        "recall": matched_count / required_count if required_count else 1.0,
        "count_mismatch_count": len(count_mismatch_ids),
        "count_mismatch_ids": count_mismatch_ids,
        "over_tolerance_count": len(over_tolerance_ids),
        "over_tolerance_ids": over_tolerance_ids,
        "span_error_mean": mean(pair_errors) if pair_errors else None,
        "span_error_max": max(pair_errors) if pair_errors else None,
        "start_error_mean": mean(start_errors) if start_errors else None,
        "start_error_max": max(start_errors) if start_errors else None,
        "end_error_mean": mean(end_errors) if end_errors else None,
        "end_error_max": max(end_errors) if end_errors else None,
        "paired_span_count": len(pair_errors),
        "occurrence_count": occurrence_count,
        "unknown_occurrence_count": unknown_occurrence_count,
        "duplicate_binding_count": len(overlaps),
        "duplicate_bindings": overlaps,
        "ok": (
            matched_count == required_count
            and not count_mismatch_ids
            and not over_tolerance_ids
            and not overlaps
        ),
        "items": items,
    }


def _format_seconds(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.3f}s"


def _format_spans(spans: Sequence[Sequence[float]]) -> str:
    if not spans:
        return "-"
    return ", ".join(f"{float(start):.3f}-{float(end):.3f}" for start, end in spans)


def _format_ids(ids: Sequence[str]) -> str:
    return ", ".join(ids) if ids else "-"


def _render_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    cells = [[str(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in cells:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(row: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()

    lines = [render(headers), render(["-" * width for width in widths])]
    lines.extend(render(row) for row in cells)
    return lines


def format_report(result: Mapping[str, Any], *, verbose: bool = False) -> str:
    """把一次 evaluate 的结果格式化成人类可读的表格。"""
    label = str(result.get("path") or result.get("name") or "<fixture>")
    lines = [f"== {label} =="]
    if result.get("description"):
        lines.append(f"   {result['description']}")

    tolerance = float(result["tolerance"])
    summary_rows = [
        ("required items", result["required_count"]),
        (
            "matched items",
            f"{result['matched_count']} ({float(result['recall']) * 100:.1f}%)",
        ),
        ("missing items", _format_ids(result["missing_ids"])),
        ("span-count mismatches", _format_ids(result["count_mismatch_ids"])),
        (
            f"over-tolerance items (>{tolerance:.2f}s)",
            _format_ids(result["over_tolerance_ids"]),
        ),
        ("cross-item overlaps", result["duplicate_binding_count"]),
        ("span error mean", _format_seconds(result["span_error_mean"])),
        ("span error max", _format_seconds(result["span_error_max"])),
        (
            "start/end error max",
            f"{_format_seconds(result['start_error_max'])} / "
            f"{_format_seconds(result['end_error_max'])}",
        ),
        ("occurrences", result["occurrence_count"]),
    ]
    if result.get("unknown_occurrence_count"):
        summary_rows.append(
            ("unknown item occurrences", result["unknown_occurrence_count"])
        )
    summary_rows.append(("status", "OK" if result["ok"] else "REVIEW"))
    lines.extend(_render_table(("metric", "value"), summary_rows))

    problem_rows = [
        (
            item["item_id"],
            item["sequence"],
            _item_status(item),
            _format_spans(item["expected_spans"]),
            _format_spans(item["actual_spans"]),
            _format_seconds(item["max_error"]),
        )
        for item in result["items"]
        if verbose or _item_has_problem(item)
    ]
    if problem_rows:
        lines.append("")
        lines.append("item detail" if verbose else "problem items")
        lines.extend(
            _render_table(
                ("item", "seq", "status", "expected", "actual", "max error"),
                problem_rows,
            )
        )
    elif not verbose:
        lines.append("")
        lines.append("all items matched within tolerance")
    return "\n".join(lines)


def _item_status(item: Mapping[str, Any]) -> str:
    statuses: list[str] = []
    if not item["matched"]:
        statuses.append("missing")
    if item["count_mismatch"]:
        statuses.append("span-count")
    if item["over_tolerance"]:
        statuses.append("over-tolerance")
    return "+".join(statuses) or "ok"


def _item_has_problem(item: Mapping[str, Any]) -> bool:
    return not item["matched"] or bool(item["count_mismatch"]) or bool(item["over_tolerance"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用真实真题 fixture 评测音频连续切题匹配算法。",
    )
    parser.add_argument(
        "--fixture",
        action="append",
        metavar="PATH",
        help="只评测指定 fixture（可重复）；默认评测两份真题 fixture。",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE_SECONDS,
        metavar="SECONDS",
        help=f"起止时间容差（秒），默认 {DEFAULT_TOLERANCE_SECONDS}。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出机器可读 JSON，而不是文本表格。",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="文本模式下同时列出每道题的明细。",
    )
    return parser


def _display_path(path: str | Path) -> str:
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.tolerance < 0:
        parser.error("--tolerance 不能为负数")

    paths = [Path(path) for path in (args.fixture or DEFAULT_FIXTURES)]
    results: list[dict[str, Any]] = []
    for path in paths:
        try:
            fixture = load_fixture(path)
            result = evaluate(fixture, tolerance=args.tolerance)
        except (OSError, ValueError, TypeError, ImportError) as exc:
            print(f"无法评测 {path}: {exc}", file=sys.stderr)
            return 1
        # 展示与 JSON 都用可读路径，避免绝对路径污染对照输出。
        display = _display_path(path)
        result["name"] = display
        result["path"] = display
        results.append(result)

    if args.json:
        payload = {"tolerance": args.tolerance, "fixtures": results}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    for result in results:
        print(format_report(result, verbose=args.verbose))
        print()
    clean = sum(1 for result in results if result["ok"])
    print(f"summary: {clean}/{len(results)} fixture(s) within tolerance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

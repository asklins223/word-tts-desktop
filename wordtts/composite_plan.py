"""多人配音作品批次计划与断点续传边界保护。"""


import hashlib

from wordtts.config import (
    COMPOSITE_BOUNDARY_MS,
    COMPOSITE_MAX_ITEMS_PER_WORK,
    COMPOSITE_MAX_TEXT_LENGTH,
    TTS_PARAM_DEFAULT,
)
from wordtts.synthesis import build_synthesis_segments


class CompositePlanError(RuntimeError):
    """多人配音合并批次无法安全构造。"""


def _composite_item_from_spec(spec):
    """把一道题展开为可写入进度文件的多人配音单元。"""
    raw_item_id = spec.get("item_id")
    if not isinstance(raw_item_id, (str, int)) or isinstance(raw_item_id, bool):
        raise CompositePlanError("多人配音批次的 item_id 格式异常")
    item_id = str(raw_item_id).strip()
    if not item_id:
        raise CompositePlanError("多人配音批次缺少 item_id")
    segments = build_synthesis_segments(
        spec.get("text", ""),
        spec.get("rate", TTS_PARAM_DEFAULT),
        spec.get("volume", TTS_PARAM_DEFAULT),
        spec.get("pitch", TTS_PARAM_DEFAULT),
        default_voice=spec.get("default_voice"),
        female_voice=spec.get("female_voice"),
        male_voice=spec.get("male_voice"),
        voice_configs=spec.get("voice_configs"),
        role_voices=spec.get("role_voices"),
        role_configs=spec.get("role_configs"),
        default_role=spec.get("default_role"),
    )
    char_count = sum(len(str(segment.get("text") or "")) for segment in segments)
    if char_count <= 0:
        raise CompositePlanError(f"{item_id} 文本为空，无法加入多人配音作品")
    if char_count > COMPOSITE_MAX_TEXT_LENGTH:
        raise CompositePlanError(
            f"{item_id} 单题文本超过讯飞多人配音安全上限 "
            f"{COMPOSITE_MAX_TEXT_LENGTH} 字，不能从题目内部强行拆分"
        )
    return {
        "item_id": item_id,
        "segments": [dict(segment) for segment in segments],
        "char_count": char_count,
    }


def _stable_composite_work_id(item_ids):
    raw = "|".join(str(item_id) for item_id in item_ids)
    return f"composite:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def build_composite_work_plan(
    item_specs,
    max_chars=COMPOSITE_MAX_TEXT_LENGTH,
    existing_plan=None,
    *,
    max_items=COMPOSITE_MAX_ITEMS_PER_WORK,
):
    """按完整题目构造多人配音作品批次。

    ``existing_plan`` 用于断点续传：已有合并作品的题目组合必须保持不变，
    避免程序中断后重新分批导致 worksId 无法复用。
    """
    try:
        max_chars = max(1, int(max_chars))
    except (TypeError, ValueError):
        max_chars = COMPOSITE_MAX_TEXT_LENGTH
    try:
        max_items = max(1, int(max_items))
    except (TypeError, ValueError):
        max_items = COMPOSITE_MAX_ITEMS_PER_WORK
    raw_specs = list(item_specs or [])
    if any(not isinstance(spec, dict) for spec in raw_specs):
        raise CompositePlanError("多人配音批次包含格式异常的题目")
    specs = raw_specs
    units = [_composite_item_from_spec(spec) for spec in specs]
    unit_ids = [unit["item_id"] for unit in units]
    if len(set(unit_ids)) != len(unit_ids):
        raise CompositePlanError("多人配音批次存在重复 item_id，拒绝覆盖切割结果")
    by_id = {unit["item_id"]: unit for unit in units}
    works = []
    used_ids = set()
    stored_work_ids = set()

    def append_work(item_units, work_id=None, works_name=None):
        if not item_units:
            return
        if len(item_units) > max_items:
            raise CompositePlanError(
                f"多人配音作品条目数超过安全上限 {max_items}"
            )
        item_ids = [unit["item_id"] for unit in item_units]
        resolved_work_id = str(work_id or _stable_composite_work_id(item_ids))
        if any(work["work_id"] == resolved_work_id for work in works):
            raise CompositePlanError("多人配音断点中存在重复作品 ID")
        # 作品名在提交前写入讯飞页面，并作为漏捕获 worksId 时的对账键。
        # 从稳定 work_id 派生，进程重启/断点续传时仍保持不变。
        resolved_works_name = str(
            works_name
            or f"wordtts_{hashlib.sha1(resolved_work_id.encode('utf-8')).hexdigest()[:16]}"
        )[:25]
        works.append({
            "work_id": resolved_work_id,
            "works_name": resolved_works_name,
            "item_ids": item_ids,
            "items": item_units,
            "item_count": len(item_units),
            "char_count": sum(unit["char_count"] for unit in item_units),
            "boundary_ms": COMPOSITE_BOUNDARY_MS,
        })

    for stored_work in existing_plan or []:
        if not isinstance(stored_work, dict):
            raise CompositePlanError("断点中的多人配音作品格式异常")
        stored_work_id = str(stored_work.get("work_id") or "").strip()
        if not stored_work_id:
            raise CompositePlanError("断点中的多人配音作品缺少作品 ID")
        if stored_work_id in stored_work_ids:
            raise CompositePlanError("多人配音断点中存在重复作品 ID")
        stored_work_ids.add(stored_work_id)
        raw_stored_ids = stored_work.get("item_ids") or []
        if not isinstance(raw_stored_ids, (list, tuple)):
            raise CompositePlanError("断点中的多人配音作品 item_ids 格式异常")
        stored_ids = []
        for raw_item_id in raw_stored_ids:
            if not isinstance(raw_item_id, (str, int)) or isinstance(raw_item_id, bool):
                raise CompositePlanError("断点中的多人配音作品包含异常题目 ID")
            item_id = str(raw_item_id).strip()
            if not item_id:
                raise CompositePlanError("断点中的多人配音作品包含空题目 ID")
            stored_ids.append(item_id)
        if not stored_ids:
            raise CompositePlanError("断点中的多人配音作品缺少题目")
        if len(set(stored_ids)) != len(stored_ids):
            raise CompositePlanError("断点中的多人配音作品存在重复题目")
        present_ids = [item_id for item_id in stored_ids if item_id in by_id]
        if not present_ids:
            continue
        if len(present_ids) != len(stored_ids):
            raise CompositePlanError(
                "断点中的多人配音作品缺少原始题目，拒绝改变作品边界"
            )
        if any(item_id in used_ids for item_id in present_ids):
            raise CompositePlanError("断点中的多人配音作品存在重复题目")
        item_units = [by_id[item_id] for item_id in present_ids]
        if len(item_units) > max_items:
            raise CompositePlanError(
                f"断点中的多人配音作品超过安全条目上限 {max_items}"
            )
        if sum(unit["char_count"] for unit in item_units) > max_chars:
            raise CompositePlanError(
                "断点中的多人配音作品超过当前字数上限，拒绝改变作品边界"
            )
        append_work(item_units, stored_work_id, stored_work.get("works_name"))
        used_ids.update(present_ids)

    current = []
    current_chars = 0
    for unit in units:
        if unit["item_id"] in used_ids:
            continue
        unit_chars = unit["char_count"]
        if current and (
            current_chars + unit_chars > max_chars
            or len(current) >= max_items
        ):
            append_work(current)
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += unit_chars
    append_work(current)

    return works

"""实体→章节回溯链测试（方案 9.1：定位可精确回溯到原文结构）。"""
import json
import os

from question_model import extract_candidate
from question_types.segmenter import load_document_once, parse_document_once
from question_types.section_slice import slice_sections
from question_types.entity_trace import trace_entities_to_sections

BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "baselines", "parse", "20260829-pre-atomic-model", "docs")
DOC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "documents")


def test_info_acquisition_entities_trace_to_sections():
    doc = json.loads(open(os.path.join(
        BASE, "7上-U2-信息获取.json"), encoding="utf-8").read())
    result = doc["parse_results"][0]
    candidate = extract_candidate(result["doc_type"], result, "7上-U2-信息获取")
    paras, _ = load_document_once(os.path.join(DOC, "7上-U2-信息获取.docx"))
    ranges = slice_sections(paras)
    traced = trace_entities_to_sections(candidate, ranges)
    assert len(traced) == len(candidate.entities)
    # 10 个小题 + 4 段材料全部归属到某个范围
    assert all(t["section_locator"] for t in traced)
    assert all(t["section_range"] for t in traced)


def test_text_reading_trace_covers_all_units():
    doc = json.loads(open(os.path.join(
        BASE, "课文跟读-G7-1.json"), encoding="utf-8").read())
    result = doc["parse_results"][0]
    candidate = extract_candidate(result["doc_type"], result, "课文跟读-G7-1")
    paras, _ = load_document_once(os.path.join(DOC, "课文跟读-G7-1.docx"))
    traced = trace_entities_to_sections(candidate, slice_sections(paras))
    assert len(traced) == 82
    assert all(t["section_locator"] for t in traced)

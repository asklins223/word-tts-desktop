"""章节范围切片测试（阶段3③：通用标题切分，划分性质 + 定位稳定性）。"""
import glob
import os

from question_types.segmenter import load_document_once
from question_types.section_slice import slice_sections

DOC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "documents")


def test_partition_property_on_all_samples():
    """每个段落恰好属于一个范围：全覆盖、连续、边界对齐。"""
    for path in sorted(glob.glob(os.path.join(DOC_DIR, "*.docx"))):
        paras, _ = load_document_once(path)
        ranges = slice_sections(paras)
        assert ranges, path
        assert ranges[0].start == 0 and ranges[-1].end == len(paras), path
        assert sum(r.end - r.start for r in ranges) == len(paras), path
        assert all(ranges[i].end == ranges[i + 1].start
                   for i in range(len(ranges) - 1)), path


def test_locator_is_stable_and_carry_title():
    paras, _ = load_document_once(
        os.path.join(DOC_DIR, "7上-U2-信息获取.docx"))
    ranges = slice_sections(paras)
    assert len(ranges) == 4
    again = slice_sections(paras)
    assert [r.source_locator for r in ranges] == \
        [r.source_locator for r in again]


def test_headingless_document_is_one_full_range():
    ranges = slice_sections([(i, "plain line", None) for i in range(5)])
    assert len(ranges) == 1
    assert ranges[0].contains(0) and ranges[0].contains(4)

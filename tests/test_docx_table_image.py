from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from workflow.docx_table_image import (
    DocxTableImageError,
    _drawing_groups,
    _read_document_xml,
    _renderer_asset_paths,
    _table_paths,
    render_docx_block_image,
    render_docx_drawing_image,
    render_docx_table_image,
)


ROOT = Path(__file__).resolve().parents[1]
DRAWING_FIXTURE = ROOT / "examples/documents/信息转述及询问信息 7上- U1.docx"
GROUPED_DRAWING_FIXTURE = ROOT / "examples/documents/佛山七上Starter 1.docx"
TABLE_FIXTURE = ROOT / "examples/documents/七上Starter Unit1 Hello模仿朗读专项.docx"


def test_renderer_assets_are_pinned_and_available():
    paths = _renderer_asset_paths()

    assert [path.name for path in paths] == [
        "lodash.min.js",
        "konva.min.js",
        "jszip.min.js",
        "docx-renderer.umd.js",
    ]
    assert all(path.stat().st_size > 0 for path in paths)


def test_table_paths_ignore_non_renderable_bookmark_nodes():
    source = ROOT / "examples/documents/七上Starter Unit 1 听说测试题（2026新题型）.docx"
    root, _theme = _read_document_xml(source)

    assert _table_paths(root) == ("body/109", "body/124", "body/129")


def test_drawing_group_uses_document_grid_and_anchor_positions():
    root, theme = _read_document_xml(DRAWING_FIXTURE)
    groups = _drawing_groups(root, theme)

    # The current mixed-paper fixture contains one positioned mind-map group;
    # the second visual block is the record table below it.
    assert len(groups) == 1
    shapes = groups[0].shapes
    assert len(shapes) == 6
    assert [shape.geometry for shape in shapes] == [
        "rect",
        "leftBrace",
        "rect",
        "rect",
        "rect",
        "rect",
    ]
    assert shapes[0].x == pytest.approx(173.8)
    assert shapes[0].y == pytest.approx(8.75)
    assert shapes[2].y - shapes[0].y == pytest.approx(48.7)
    assert shapes[-1].y - shapes[0].y == pytest.approx(141.7)
    assert shapes[1].line_color == "#4874CB"


def test_recording_paper_exposes_both_table_blocks(tmp_path: Path):
    root, _theme = _read_document_xml(TABLE_FIXTURE)
    assert _table_paths(root) == ("body/5", "body/14")

    outputs = []
    for table_index in (0, 1):
        output = tmp_path / f"table-{table_index}.png"
        result = render_docx_table_image(
            TABLE_FIXTURE,
            output,
            table_index=table_index,
        )
        outputs.append(result)
        assert result["block_kind"] == "table"
        assert result["fragment_count"] == 1
        assert result["size_bytes"] == output.stat().st_size
        assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    assert outputs[0]["width"] == outputs[1]["width"]
    assert outputs[0]["height"] > outputs[1]["height"]


def test_grouped_drawing_is_flattened_with_nested_text_boxes():
    root, theme = _read_document_xml(GROUPED_DRAWING_FIXTURE)
    groups = _drawing_groups(root, theme)

    assert len(groups) == 1
    assert groups[0].shapes[0].geometry == "group"
    children = groups[0].shapes[0].children
    assert len(children) == 6
    assert children[0]["name"] == "文本框 1"
    assert children[0]["paragraphs"][1]["runs"][1]["color"] == "#FF0000"
    assert children[-1]["geometry"] == "leftBrace"


def test_block_renderer_rejects_unknown_kind(tmp_path: Path):
    with pytest.raises(DocxTableImageError, match="不支持"):
        render_docx_block_image(
            DRAWING_FIXTURE,
            tmp_path / "unknown.png",
            block_kind="page",
            block_index=0,
        )


@pytest.mark.parametrize(
    ("source", "table_index", "expected_pages"),
    [
        (
            ROOT / (
                "examples/documents/七上Starter Unit 1 Hello听后记录并转述信息专项-"
                "答案扩展(1).docx"
            ),
            0,
            [1],
        ),
        (
            ROOT / "examples/documents/七上Starter Unit 1 听说测试题（2026新题型）.docx",
            1,
            [5],
        ),
        (
            ROOT / "examples/documents/七上StarterUnit2听说测试题-信息转述答案扩展版.docx",
            1,
            [5],
        ),
    ],
    ids=[
        "starter-unit-1-recording",
        "starter-unit-1-2026-recording",
        "starter-unit-2-extended-recording",
    ],
)
def test_repository_recording_fixture_renders_directly_to_png(
    tmp_path: Path,
    source: Path,
    table_index: int,
    expected_pages: list[int],
):
    output = tmp_path / "recording-table.png"
    result = render_docx_table_image(source, output, table_index=table_index)

    assert result["block_kind"] == "table"
    assert result["fragment_count"] == 1
    assert result["pages"] == expected_pages
    assert result["size_bytes"] == output.stat().st_size
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    image = Image.open(output).convert("RGB")

    def is_dark(pixel):
        return max(pixel[:3]) <= 220

    assert sum(is_dark(image.getpixel((x, 0))) for x in range(image.width)) >= image.width * 0.9
    assert sum(
        is_dark(image.getpixel((x, image.height - 1)))
        for x in range(image.width)
    ) >= image.width * 0.9
    assert sum(is_dark(image.getpixel((0, y))) for y in range(image.height)) >= image.height * 0.9
    assert sum(
        is_dark(image.getpixel((image.width - 1, y)))
        for y in range(image.height)
    ) >= image.height * 0.9


def test_positioned_drawing_group_renders_without_full_page_conversion(tmp_path: Path):
    output = tmp_path / "mind-map.png"
    result = render_docx_drawing_image(
        DRAWING_FIXTURE,
        output,
        drawing_group_index=0,
    )

    assert result["block_kind"] == "drawing_group"
    assert result["fragment_count"] == 1
    assert result["pages"] == [1]
    assert result["width"] > result["height"] * 2
    image = Image.open(output).convert("RGB")
    pixels = list(image.get_flattened_data())
    red = sum(r >= 180 and g <= 110 and b <= 110 for r, g, b in pixels)
    blue = sum(b >= 140 and b >= r + 30 and b >= g + 20 for r, g, b in pixels)
    assert red > 1_000
    assert blue > 300


def test_grouped_positioned_drawing_renders_nested_text_boxes(tmp_path: Path):
    output = tmp_path / "grouped-mind-map.png"
    result = render_docx_drawing_image(
        GROUPED_DRAWING_FIXTURE,
        output,
        drawing_group_index=0,
    )

    assert result["block_kind"] == "drawing_group"
    assert result["pages"] == [3]
    image = Image.open(output).convert("RGB")
    pixels = list(image.get_flattened_data())
    red = sum(r >= 180 and g <= 110 and b <= 110 for r, g, b in pixels)
    black = sum(max(r, g, b) <= 40 for r, g, b in pixels)
    assert red > 1_000
    assert black > 5_000


def test_renderer_reuses_one_browser_worker(tmp_path: Path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    render_docx_drawing_image(DRAWING_FIXTURE, first, drawing_group_index=0)
    from workflow import docx_table_image

    worker = docx_table_image._BROWSER_RENDERER._thread
    render_docx_drawing_image(DRAWING_FIXTURE, second, drawing_group_index=0)

    assert worker is not None
    assert worker is docx_table_image._BROWSER_RENDERER._thread
    assert worker.is_alive()

"""Render selected DOCX blocks directly with the bundled Chromium runtime.

The paper-input adapter needs a PNG of one source table or drawing group. A
DOCX is already an OOXML archive, so converting the whole document through
LibreOffice and PDF are unnecessary: this module resolves the requested source
block from ``word/document.xml``, renders the document with a small browser
renderer, and screenshots only that block.

The browser is kept on a dedicated daemon thread and reused between requests.
This avoids Playwright's thread-affinity hazards in FastAPI workers and removes
repeated browser cold starts from the synchronous parse path.
"""

from __future__ import annotations

import atexit
import base64
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from io import BytesIO
import math
import os
from pathlib import Path
import queue
import sys
import threading
from typing import Any, Mapping
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


TABLE_IMAGE_MAX_BYTES = 16 * 1024 * 1024
TABLE_IMAGE_MAX_PAGES = 256
TABLE_IMAGE_DEFAULT_DPI = 200
TABLE_IMAGE_MAX_DPI = 300
TABLE_IMAGE_RENDER_TIMEOUT_SECONDS = 30
# The application includes this value in the deterministic artifact id. The
# direct OOXML/Chromium pipeline must never reuse a PDF-era cached image.
TABLE_IMAGE_RENDERER_VERSION = "3-direct-docx-block"
TABLE_IMAGE_DEFAULT_PADDING_POINTS = 0.75
TABLE_IMAGE_MAX_PADDING_POINTS = 12.0

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_WPG = "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"
_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
_NS = {"w": _W, "wp": _WP, "a": _A, "wpg": _WPG, "wps": _WPS}
_EMU_PER_POINT = 12_700.0
_TWIPS_PER_POINT = 20.0
_RENDERABLE_BODY_TAGS = {f"{{{_W}}}p", f"{{{_W}}}tbl"}

_ASSET_FILES = (
    "lodash.min.js",
    "konva.min.js",
    "jszip.min.js",
    "docx-renderer.umd.js",
)
_DEV_ASSET_PATHS = {
    "lodash.min.js": ("lodash", "lodash.min.js"),
    "konva.min.js": ("konva", "konva.min.js"),
    "jszip.min.js": ("jszip", "dist", "jszip.min.js"),
    "docx-renderer.umd.js": (
        "docx-renderer",
        "dist",
        "docx-renderer.umd.js",
    ),
}


class DocxTableImageError(RuntimeError):
    """A requested DOCX block could not be rendered safely."""

    code = "TABLE_IMAGE_ERROR"


@dataclass(frozen=True)
class _DrawingShape:
    path: str
    ordinal: int
    name: str
    geometry: str
    x: float
    y: float
    width: float
    height: float
    z_order: int
    line_color: str
    children: tuple[Mapping[str, Any], ...] = ()

    def as_browser_value(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ordinal": self.ordinal,
            "name": self.name,
            "geometry": self.geometry,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "zOrder": self.z_order,
            "lineColor": self.line_color,
            "children": list(self.children),
        }


@dataclass(frozen=True)
class _DrawingGroup:
    index: int
    shapes: tuple[_DrawingShape, ...]


@dataclass(frozen=True)
class _BrowserJob:
    source_bytes: bytes
    block_kind: str
    block_path: str | None
    drawing_group: Mapping[str, Any] | None
    dpi: int
    timeout_seconds: int


def _regular_path(
    value: str | os.PathLike[str],
    *,
    suffix: str | None = None,
) -> Path:
    original = Path(value).expanduser()
    if original.is_symlink():
        raise DocxTableImageError("文档块图片的源路径不能是符号链接")
    try:
        path = original.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DocxTableImageError("文档块图片的源文件不存在") from exc
    if not path.is_file():
        raise DocxTableImageError("文档块图片的源文件不是普通文件")
    if suffix and path.suffix.casefold() != suffix.casefold():
        raise DocxTableImageError("文档块图片只支持 DOCX 源文档")
    return path


def _validate_output_path(value: str | os.PathLike[str]) -> Path:
    output = Path(value).expanduser()
    if output.exists() or output.is_symlink():
        raise DocxTableImageError("文档块图片输出文件已经存在")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DocxTableImageError("文档块图片输出目录不可写") from exc
    return output


def _renderer_asset_paths() -> tuple[Path, ...]:
    """Resolve the pinned renderer scripts in source and frozen builds."""

    candidates: list[dict[str, Path]] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        root = Path(meipass) / "workflow" / "docx_renderer_assets"
        candidates.append({name: root / name for name in _ASSET_FILES})

    package_root = Path(__file__).resolve().parent / "docx_renderer_assets"
    candidates.append({name: package_root / name for name in _ASSET_FILES})

    repository_root = Path(__file__).resolve().parents[1]
    node_modules = repository_root / "electron" / "node_modules"
    candidates.append({
        name: node_modules.joinpath(*_DEV_ASSET_PATHS[name])
        for name in _ASSET_FILES
    })

    for manifest in candidates:
        try:
            paths = tuple(manifest[name].resolve(strict=True) for name in _ASSET_FILES)
        except (OSError, RuntimeError):
            continue
        if all(
            path.is_file() and 0 < path.stat().st_size < 8 * 1024 * 1024
            for path in paths
        ):
            return paths
    raise DocxTableImageError("DOCX 文档块渲染脚本缺失，请重新安装完整版本")


def _read_document_xml(source: Path) -> tuple[ET.Element, dict[str, str]]:
    try:
        with ZipFile(source) as archive:
            document_xml = archive.read("word/document.xml")
            theme_xml = (
                archive.read("word/theme/theme1.xml")
                if "word/theme/theme1.xml" in archive.namelist()
                else None
            )
    except (BadZipFile, KeyError, OSError) as exc:
        raise DocxTableImageError("DOCX 文档结构无法读取") from exc
    try:
        root = ET.fromstring(document_xml)
        theme = _theme_colors(ET.fromstring(theme_xml)) if theme_xml else {}
    except ET.ParseError as exc:
        raise DocxTableImageError("DOCX 文档结构已损坏") from exc
    return root, theme


def _theme_colors(root: ET.Element) -> dict[str, str]:
    colors: dict[str, str] = {}
    scheme = root.find(".//a:themeElements/a:clrScheme", _NS)
    if scheme is None:
        return colors
    for entry in list(scheme):
        name = entry.tag.rsplit("}", 1)[-1]
        child = next(iter(entry), None)
        if child is None:
            continue
        # ``sysClr`` stores a platform name in ``val`` and the actual RGB
        # fallback in ``lastClr``.  The latter is what a headless renderer
        # needs; otherwise Word's ``window`` becomes an invalid CSS color.
        value = child.get("lastClr") or child.get("val")
        if value and len(value) == 6:
            colors[name] = f"#{value.upper()}"
    return colors


def _shape_line_color(anchor: ET.Element, theme: Mapping[str, str]) -> str:
    line = anchor.find(".//wps:spPr/a:ln", _NS)
    if line is not None:
        direct = line.find("./a:solidFill/a:srgbClr", _NS)
        if direct is not None and direct.get("val"):
            return f"#{direct.get('val', '000000').upper()}"
        preset = line.find("./a:solidFill/a:prstClr", _NS)
        if preset is not None:
            return {"black": "#000000", "white": "#FFFFFF"}.get(
                str(preset.get("val") or "").casefold(),
                "#000000",
            )
        themed = line.find("./a:solidFill/a:schemeClr", _NS)
        if themed is not None:
            return theme.get(str(themed.get("val") or ""), "#000000")
    themed = anchor.find(".//wps:style/a:lnRef/a:schemeClr", _NS)
    if themed is not None:
        return theme.get(str(themed.get("val") or ""), "#000000")
    return "#000000"


def _number_text(element: ET.Element | None, path: str) -> float | None:
    if element is None:
        return None
    value = element.findtext(path, default="", namespaces=_NS).strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _paragraph_visible_text(paragraph: ET.Element) -> str:
    """Return normal paragraph text while excluding drawing fallbacks."""

    values: list[str] = []

    def visit(node: ET.Element, excluded: bool = False) -> None:
        local = node.tag.rsplit("}", 1)[-1]
        excluded = excluded or local in {"drawing", "pict", "txbxContent"}
        if not excluded and node.tag == f"{{{_W}}}t" and node.text:
            values.append(node.text)
        for child in list(node):
            visit(child, excluded)

    visit(paragraph)
    return "".join(values).strip()


def _paragraph_advance_points(paragraph: ET.Element, line_pitch: float) -> float:
    spacing = paragraph.find("./w:pPr/w:spacing", _NS)
    before = after = 0.0
    line = None
    line_rule = ""
    if spacing is not None:
        try:
            before = float(spacing.get(f"{{{_W}}}before") or 0) / _TWIPS_PER_POINT
            after = float(spacing.get(f"{{{_W}}}after") or 0) / _TWIPS_PER_POINT
            raw_line = spacing.get(f"{{{_W}}}line")
            line = float(raw_line) / _TWIPS_PER_POINT if raw_line else None
        except (TypeError, ValueError):
            before = after = 0.0
            line = None
        line_rule = str(spacing.get(f"{{{_W}}}lineRule") or "")
    if line is not None and line_rule == "exact":
        height = line
    elif line is not None and line_rule == "atLeast":
        height = max(line_pitch, line)
    else:
        height = line_pitch
    return max(1.0, before + height + after)


def _section_metrics(root: ET.Element) -> tuple[float, float, float]:
    section = root.find(".//w:sectPr", _NS)
    line_pitch = 15.6
    margin_left = 90.0
    margin_top = 72.0
    if section is None:
        return line_pitch, margin_left, margin_top
    grid = section.find("./w:docGrid", _NS)
    margins = section.find("./w:pgMar", _NS)
    try:
        if grid is not None and grid.get(f"{{{_W}}}linePitch"):
            line_pitch = float(grid.get(f"{{{_W}}}linePitch")) / _TWIPS_PER_POINT
        if margins is not None and margins.get(f"{{{_W}}}left"):
            margin_left = float(margins.get(f"{{{_W}}}left")) / _TWIPS_PER_POINT
        if margins is not None and margins.get(f"{{{_W}}}top"):
            margin_top = float(margins.get(f"{{{_W}}}top")) / _TWIPS_PER_POINT
    except (TypeError, ValueError):
        pass
    return line_pitch, margin_left, margin_top


def _axis_position(
    position: ET.Element | None,
    *,
    flow_position: float,
    margin_position: float,
    axis: str,
) -> float:
    if position is None:
        raise DocxTableImageError("DOCX 浮动对象缺少定位信息")
    offset = _number_text(position, "./wp:posOffset")
    if offset is None:
        raise DocxTableImageError("暂不支持按对齐方式定位的 DOCX 浮动对象")
    points = offset / _EMU_PER_POINT
    relative = str(position.get("relativeFrom") or "")
    if axis == "x":
        if relative == "page":
            return points
        if relative in {"column", "margin", "leftMargin"}:
            return margin_position + points
    else:
        if relative == "page":
            return points
        if relative == "margin":
            return margin_position + points
        if relative in {"paragraph", "line"}:
            return flow_position + points
    raise DocxTableImageError(
        f"暂不支持相对 {relative or 'unknown'} 定位的 DOCX 浮动对象"
    )


def _color_value(element: ET.Element | None, theme: Mapping[str, str], default: str) -> str:
    """Resolve the small subset of DrawingML colors used by text boxes."""

    if element is None:
        return default
    direct = element.find("./a:srgbClr", _NS)
    if direct is not None and direct.get("val"):
        return f"#{direct.get('val', '').upper()}"
    preset = element.find("./a:prstClr", _NS)
    if preset is not None:
        return {
            "black": "#000000",
            "white": "#FFFFFF",
            "red": "#FF0000",
            "blue": "#0000FF",
        }.get(str(preset.get("val") or "").casefold(), default)
    themed = element.find("./a:schemeClr", _NS)
    if themed is not None:
        return theme.get(str(themed.get("val") or ""), default)
    return default


def _shape_fill_color(shape: ET.Element, theme: Mapping[str, str]) -> str:
    fill = shape.find("./wps:spPr/a:solidFill", _NS)
    if fill is not None:
        return _color_value(fill, theme, "#FFFFFF")
    if shape.find("./wps:spPr/a:noFill", _NS) is not None:
        return "transparent"
    return "#FFFFFF"


def _shape_line_width(shape: ET.Element) -> float:
    line = shape.find("./wps:spPr/a:ln", _NS)
    if line is None:
        return 0.75
    try:
        # DrawingML line widths are EMUs. Keep a visible but bounded CSS width.
        return min(8.0, max(0.0, float(line.get("w") or 0.0) / _EMU_PER_POINT))
    except (TypeError, ValueError):
        return 0.75


def _shape_body_padding(shape: ET.Element) -> tuple[float, float, float, float]:
    body = shape.find("./wps:bodyPr", _NS)
    if body is None:
        return (7.2, 3.6, 7.2, 3.6)
    values = []
    for name, fallback in (("lIns", 7.2), ("tIns", 3.6), ("rIns", 7.2), ("bIns", 3.6)):
        try:
            values.append(max(0.0, float(body.get(name) or 0.0) / _EMU_PER_POINT))
        except (TypeError, ValueError):
            values.append(fallback)
    return tuple(values)  # type: ignore[return-value]


def _run_text(run: ET.Element) -> str:
    values: list[str] = []
    for child in run.iter():
        if child.tag == f"{{{_W}}}t" and child.text:
            values.append(child.text)
        elif child.tag == f"{{{_W}}}tab":
            values.append("\t")
        elif child.tag in {f"{{{_W}}}br", f"{{{_W}}}cr"}:
            values.append("\n")
    return "".join(values)


def _run_color(run: ET.Element, paragraph: ET.Element, theme: Mapping[str, str]) -> str:
    run_properties = run.find("./w:rPr", _NS)
    color = run_properties.find("./w:color", _NS) if run_properties is not None else None
    if color is None:
        paragraph_properties = paragraph.find("./w:pPr/w:rPr", _NS)
        color = (
            paragraph_properties.find("./w:color", _NS)
            if paragraph_properties is not None
            else None
        )
    if color is None:
        return "#000000"
    value = str(color.get("{%s}val" % _W) or color.get("val") or "").strip()
    if len(value) == 6 and all(char in "0123456789abcdefABCDEF" for char in value):
        return f"#{value.upper()}"
    theme_name = str(color.get("{%s}themeColor" % _W) or color.get("themeColor") or "")
    return theme.get(theme_name, "#000000")


def _run_font_size(run: ET.Element, paragraph: ET.Element) -> float:
    run_properties = run.find("./w:rPr", _NS)
    size = run_properties.find("./w:sz", _NS) if run_properties is not None else None
    if size is None:
        paragraph_properties = paragraph.find("./w:pPr/w:rPr", _NS)
        size = paragraph_properties.find("./w:sz", _NS) if paragraph_properties is not None else None
    try:
        return min(96.0, max(1.0, float(size.get(f"{{{_W}}}val") or 24.0) / 2.0)) if size is not None else 12.0
    except (TypeError, ValueError):
        return 12.0


def _group_shape_text(shape: ET.Element, theme: Mapping[str, str]) -> tuple[dict[str, Any], ...]:
    """Convert grouped text-box runs into safe, browser-ready text fragments."""

    paragraphs: list[dict[str, Any]] = []
    content = shape.find("./wps:txbx/w:txbxContent", _NS)
    if content is None:
        content = shape.find("./wps:txbxContent", _NS)
    if content is None:
        return ()
    for paragraph in content.findall("./w:p", _NS):
        runs: list[dict[str, Any]] = []
        for run in paragraph.iter(f"{{{_W}}}r"):
            text = _run_text(run)
            if not text:
                continue
            runs.append({
                "text": text,
                "color": _run_color(run, paragraph, theme),
                "fontSize": _run_font_size(run, paragraph),
            })
        if runs:
            properties = paragraph.find("./w:pPr/w:jc", _NS)
            paragraphs.append({
                "align": str(properties.get(f"{{{_W}}}val") or "left") if properties is not None else "left",
                "runs": runs,
            })
    return tuple(paragraphs)


def _group_children(
    anchor: ET.Element,
    *,
    width: float,
    height: float,
    theme: Mapping[str, str],
) -> tuple[Mapping[str, Any], ...]:
    """Flatten a DrawingML group into positioned child text boxes."""

    group = anchor.find(".//wpg:wgp", _NS)
    if group is None:
        return ()
    transform = group.find("./wpg:grpSpPr/a:xfrm", _NS)
    if transform is None:
        return ()
    try:
        child_offset = transform.find("./a:chOff", _NS)
        child_extent = transform.find("./a:chExt", _NS)
        origin_x = float(child_offset.get("x") or 0.0) if child_offset is not None else 0.0
        origin_y = float(child_offset.get("y") or 0.0) if child_offset is not None else 0.0
        extent_x = float(child_extent.get("cx") or 0.0) if child_extent is not None else 0.0
        extent_y = float(child_extent.get("cy") or 0.0) if child_extent is not None else 0.0
    except (TypeError, ValueError):
        return ()
    if extent_x <= 0 or extent_y <= 0:
        return ()

    children: list[Mapping[str, Any]] = []
    for ordinal, shape in enumerate(group.findall("./wps:wsp", _NS)):
        transform = shape.find("./wps:spPr/a:xfrm", _NS)
        if transform is None:
            continue
        offset = transform.find("./a:off", _NS)
        extent = transform.find("./a:ext", _NS)
        if offset is None or extent is None:
            continue
        try:
            child_x = (float(offset.get("x") or 0.0) - origin_x) / extent_x * width
            child_y = (float(offset.get("y") or 0.0) - origin_y) / extent_y * height
            child_width = float(extent.get("cx") or 0.0) / extent_x * width
            child_height = float(extent.get("cy") or 0.0) / extent_y * height
        except (TypeError, ValueError):
            continue
        if child_width <= 0 or child_height <= 0:
            continue
        geometry_node = shape.find("./wps:spPr/a:prstGeom", _NS)
        doc_pr = shape.find("./wps:cNvPr", _NS)
        children.append({
            "ordinal": ordinal,
            "name": str(doc_pr.get("name") or "") if doc_pr is not None else "",
            "geometry": str(geometry_node.get("prst") or "rect") if geometry_node is not None else "rect",
            "x": child_x,
            "y": child_y,
            "width": child_width,
            "height": child_height,
            "lineColor": _shape_line_color(shape, theme),
            "lineWidth": _shape_line_width(shape),
            "fillColor": _shape_fill_color(shape, theme),
            "padding": _shape_body_padding(shape),
            "paragraphs": list(_group_shape_text(shape, theme)),
        })
    return tuple(children)


def _drawing_groups(
    root: ET.Element,
    theme: Mapping[str, str],
) -> tuple[_DrawingGroup, ...]:
    """Collect adjacent anchored drawings into deterministic visual groups."""

    body = root.find(".//w:body", _NS)
    if body is None:
        raise DocxTableImageError("DOCX 正文结构缺失")
    line_pitch, margin_left, margin_top = _section_metrics(root)
    groups: list[_DrawingGroup] = []
    current: list[_DrawingShape] = []
    current_base_flow = 0.0
    flow_y = margin_top
    renderable_index = 0

    def finish_group() -> None:
        nonlocal current
        if current:
            groups.append(_DrawingGroup(len(groups), tuple(current)))
            current = []

    for child in list(body):
        if child.tag not in _RENDERABLE_BODY_TAGS:
            continue
        path = f"body/{renderable_index}"
        renderable_index += 1
        if child.tag == f"{{{_W}}}tbl":
            finish_group()
            continue

        paragraph = child
        anchors = list(paragraph.findall(".//wp:anchor", _NS))
        visible_text = _paragraph_visible_text(paragraph)
        if anchors and not current:
            current_base_flow = flow_y
        for ordinal, anchor in enumerate(anchors):
            horizontal = anchor.find("./wp:positionH", _NS)
            vertical = anchor.find("./wp:positionV", _NS)
            extent = anchor.find("./wp:extent", _NS)
            try:
                width = (
                    float(extent.get("cx")) / _EMU_PER_POINT
                    if extent is not None
                    else 0.0
                )
                height = (
                    float(extent.get("cy")) / _EMU_PER_POINT
                    if extent is not None
                    else 0.0
                )
            except (TypeError, ValueError):
                width = height = 0.0
            if width <= 0 or height <= 0:
                raise DocxTableImageError("DOCX 浮动对象尺寸无效")
            doc_pr = anchor.find("./wp:docPr", _NS)
            try:
                z_order = int(anchor.get("relativeHeight") or 0)
            except (TypeError, ValueError):
                z_order = 0
            group_node = anchor.find(".//wpg:wgp", _NS)
            geometry_node = anchor.find(".//a:prstGeom", _NS)
            current.append(_DrawingShape(
                path=path,
                ordinal=ordinal,
                name=str(doc_pr.get("name") or "") if doc_pr is not None else "",
                geometry=(
                    "group"
                    if group_node is not None
                    else (
                        str(geometry_node.get("prst") or "rect")
                        if geometry_node is not None
                        else "rect"
                    )
                ),
                x=_axis_position(
                    horizontal,
                    flow_position=0.0,
                    margin_position=margin_left,
                    axis="x",
                ),
                y=_axis_position(
                    vertical,
                    flow_position=flow_y - current_base_flow,
                    margin_position=margin_top,
                    axis="y",
                ),
                width=width,
                height=height,
                z_order=z_order,
                line_color=_shape_line_color(anchor, theme),
                children=_group_children(
                    anchor,
                    width=width,
                    height=height,
                    theme=theme,
                ) if group_node is not None else (),
            ))

        flow_y += _paragraph_advance_points(paragraph, line_pitch)
        # Empty paragraphs are layout rows inside a drawing group. Visible
        # normal text or a table is a semantic boundary between groups.
        if visible_text:
            finish_group()

    finish_group()
    return tuple(groups)


def _table_paths(root: ET.Element) -> tuple[str, ...]:
    body = root.find(".//w:body", _NS)
    if body is None:
        raise DocxTableImageError("DOCX 正文结构缺失")
    paths: list[str] = []
    renderable_index = 0
    for child in list(body):
        if child.tag not in _RENDERABLE_BODY_TAGS:
            continue
        if child.tag == f"{{{_W}}}tbl":
            paths.append(f"body/{renderable_index}")
        renderable_index += 1
    return tuple(paths)


def _drawing_group_browser_value(
    group: _DrawingGroup,
    *,
    padding_points: float,
) -> dict[str, Any]:
    min_x = min(shape.x for shape in group.shapes) - padding_points
    min_y = min(shape.y for shape in group.shapes) - padding_points
    max_x = max(shape.x + shape.width for shape in group.shapes) + padding_points
    max_y = max(shape.y + shape.height for shape in group.shapes) + padding_points
    return {
        "index": group.index,
        "minX": min_x,
        "minY": min_y,
        "width": max_x - min_x,
        "height": max_y - min_y,
        "shapes": [
            shape.as_browser_value()
            for shape in sorted(group.shapes, key=lambda item: item.z_order)
        ],
    }


_PAGE_HTML = """<!doctype html><meta charset="utf-8"><style>
html,body{margin:0;background:#fff}#docx-style{display:none}
#docx-source{position:absolute;left:0;top:0;opacity:0;pointer-events:none}
#docx-stage{position:relative;display:inline-block;background:#fff}
#docx-capture{position:relative;display:inline-block;background:#fff;overflow:hidden}
#docx-capture p{margin:0}
</style><div id="docx-style"></div><div id="docx-source"></div>
<div id="docx-stage"><div id="docx-capture" class="docx"></div></div>"""


_RENDER_SCRIPT = r"""async ({sourceBase64, blockKind, blockPath, drawingGroup, timeoutMs}) => {
  const bytes = Uint8Array.from(atob(sourceBase64), char => char.charCodeAt(0));
  const source = document.getElementById('docx-source');
  const styles = document.getElementById('docx-style');
  const capture = document.getElementById('docx-capture');
  const timeout = new Promise((_, reject) => setTimeout(
    () => reject(new Error('DOCX_RENDER_TIMEOUT')), timeoutMs,
  ));
  const result = await Promise.race([
    window.docx.render(bytes, source, styles, {
      breakPages: true,
      debug: true,
      useBase64URL: true,
    }),
    timeout,
  ]);
  result.pages.forEach((page, index) => {
    page.element.dataset.docxPageNumber = String(index + 1);
  });
  if (result.pages.length > 256) throw new Error('DOCX_PAGE_LIMIT');
  await document.fonts.ready;
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));

  const pageNumber = element => Number(
    element.closest('section.docx')?.dataset.docxPageNumber || 1,
  );
  const captures = [];
  if (blockKind === 'table') {
    const tables = [...document.querySelectorAll('table[data-renderer-path]')]
      .filter(element => element.getAttribute('data-renderer-path') === blockPath);
    if (!tables.length) throw new Error('DOCX_TABLE_NOT_FOUND');
    tables.forEach((table, index) => {
      const wrapper = document.createElement('div');
      wrapper.id = `docx-fragment-${index}`;
      wrapper.className = 'docx';
      Object.assign(wrapper.style, {
        display: 'block', width: 'fit-content', background: '#fff',
      });
      wrapper.appendChild(table.cloneNode(true));
      capture.appendChild(wrapper);
      captures.push({id: wrapper.id, page: pageNumber(table)});
    });
  } else if (blockKind === 'drawing_group') {
    const point = 96 / 72;
    Object.assign(capture.style, {
      width: `${drawingGroup.width * point}px`,
      height: `${drawingGroup.height * point}px`,
      display: 'block',
    });
    let anchorPage = 1;
    for (const shape of drawingGroup.shapes) {
      const roots = result.sourceMap.elementsFor(shape.path).filter(
        element => element.tagName === 'P' && !element.closest('.docx-textbox'),
      );
      const root = roots[0];
      if (root) anchorPage = pageNumber(root);
      if (shape.geometry === 'group') {
        if (!Array.isArray(shape.children) || !shape.children.length) {
          throw new Error('DOCX_DRAWING_NOT_FOUND');
        }
        const group = document.createElement('div');
        Object.assign(group.style, {
          position: 'absolute',
          display: 'block',
          left: `${(shape.x - drawingGroup.minX) * point}px`,
          top: `${(shape.y - drawingGroup.minY) * point}px`,
          width: `${shape.width * point}px`,
          height: `${shape.height * point}px`,
          margin: '0',
        });
        for (const child of shape.children) {
          const childNode = document.createElement('div');
          const padding = Array.isArray(child.padding) ? child.padding : [7.2, 3.6, 7.2, 3.6];
          Object.assign(childNode.style, {
            position: 'absolute',
            display: 'block',
            boxSizing: 'border-box',
            left: `${child.x * point}px`,
            top: `${child.y * point}px`,
            width: `${child.width * point}px`,
            height: `${child.height * point}px`,
            margin: '0',
            overflow: 'hidden',
            background: child.fillColor || '#fff',
            border: (child.geometry === 'leftBrace' || child.geometry === 'rightBrace')
              ? '0'
              : `${Math.max(0, Number(child.lineWidth) || 0.75) * point}px solid ${child.lineColor || '#000'}`,
            padding: `${padding[1] * point}px ${padding[2] * point}px ${padding[3] * point}px ${padding[0] * point}px`,
            color: '#000',
            fontFamily: 'Times New Roman, SimSun, serif',
            fontSize: '12pt',
            lineHeight: 'normal',
            whiteSpace: 'pre-wrap',
          });
          if (child.geometry === 'leftBrace' || child.geometry === 'rightBrace') {
            const right = child.geometry === 'leftBrace';
            const path = right
              ? 'M100 0 H42 Q20 0 20 15 V42 Q20 50 0 50 Q20 50 20 58 V85 Q20 100 42 100 H100'
              : 'M0 0 H58 Q80 0 80 15 V42 Q80 50 100 50 Q80 50 80 58 V85 Q80 100 58 100 H0';
            childNode.innerHTML = `<svg viewBox="0 0 100 100" preserveAspectRatio="none" style="position:absolute;inset:0;width:100%;height:100%;overflow:visible"><path d="${path}" fill="none" stroke="${child.lineColor || '#000'}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
          }
          for (const paragraph of (Array.isArray(child.paragraphs) ? child.paragraphs : [])) {
            const paragraphNode = document.createElement('div');
            paragraphNode.style.textAlign = paragraph.align === 'both' ? 'justify' : (paragraph.align || 'left');
            for (const run of (Array.isArray(paragraph.runs) ? paragraph.runs : [])) {
              const runNode = document.createElement('span');
              runNode.textContent = String(run.text || '');
              runNode.style.color = run.color || '#000';
              if (Number(run.fontSize) > 0) runNode.style.fontSize = `${Number(run.fontSize)}pt`;
              paragraphNode.appendChild(runNode);
            }
            childNode.appendChild(paragraphNode);
          }
          group.appendChild(childNode);
        }
        capture.appendChild(group);
        continue;
      }
      const original = root?.querySelectorAll('span[data-tag="shape"]')[shape.ordinal];
      if (!original) throw new Error('DOCX_DRAWING_NOT_FOUND');
      anchorPage = pageNumber(root);
      const clone = original.cloneNode(true);
      Object.assign(clone.style, {
        position: 'absolute',
        display: 'block',
        left: `${(shape.x - drawingGroup.minX) * point}px`,
        top: `${(shape.y - drawingGroup.minY) * point}px`,
        width: `${shape.width * point}px`,
        height: `${shape.height * point}px`,
        margin: '0',
      });
      if (shape.geometry === 'leftBrace' || shape.geometry === 'rightBrace') {
        const right = shape.geometry === 'leftBrace';
        const path = right
          ? 'M100 0 H42 Q20 0 20 15 V42 Q20 50 0 50 Q20 50 20 58 V85 Q20 100 42 100 H100'
          : 'M0 0 H58 Q80 0 80 15 V42 Q80 50 100 50 Q80 50 80 58 V85 Q80 100 58 100 H0';
        clone.innerHTML = `<svg viewBox="0 0 100 100" preserveAspectRatio="none" style="position:absolute;inset:0;width:100%;height:100%;overflow:visible"><path d="${path}" fill="none" stroke="${shape.lineColor}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
      }
      capture.appendChild(clone);
    }
    capture.id = 'docx-fragment-0';
    captures.push({id: capture.id, page: anchorPage});
  } else {
    throw new Error('DOCX_BLOCK_KIND');
  }
  source.remove();
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  return {captures, pageCount: result.pages.length};
}"""


class _BrowserRenderer:
    """Own one reusable Chromium instance on a thread-affine worker."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[_BrowserJob, Future] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def render(self, job: _BrowserJob) -> tuple[list[bytes], list[int]]:
        future: Future = Future()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._worker,
                    name="docx-block-renderer",
                    daemon=True,
                )
                self._thread.start()
            self._queue.put((job, future))
        try:
            return future.result(timeout=max(1, job.timeout_seconds) + 5)
        except FutureTimeoutError as exc:
            raise DocxTableImageError("DOCX 文档块渲染超时") from exc

    def close(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
            if thread is not None and thread.is_alive():
                self._queue.put(None)
        if thread is not None and threading.current_thread() is not thread:
            thread.join(timeout=2)

    def _worker(self) -> None:
        playwright = browser = None
        try:
            while True:
                task = self._queue.get()
                if task is None:
                    return
                job, future = task
                if future.cancelled():
                    continue
                try:
                    if playwright is None:
                        from playwright.sync_api import sync_playwright

                        playwright = sync_playwright().start()
                    if browser is None or not browser.is_connected():
                        browser = playwright.chromium.launch(headless=True)
                    value = self._render_job(browser, job)
                except Exception as exc:
                    if browser is not None and not browser.is_connected():
                        browser = None
                    if isinstance(exc, DocxTableImageError):
                        future.set_exception(exc)
                    elif isinstance(exc, ImportError):
                        future.set_exception(DocxTableImageError(
                            "当前运行环境缺少 Playwright 文档渲染依赖"
                        ))
                    else:
                        future.set_exception(DocxTableImageError(
                            "内置 Chromium 文档渲染依赖不可用"
                        ))
                else:
                    future.set_result(value)
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass

    @staticmethod
    def _render_job(browser: Any, job: _BrowserJob) -> tuple[list[bytes], list[int]]:
        assets = _renderer_asset_paths()
        context = None
        try:
            context = browser.new_context(
                viewport={"width": 1600, "height": 1200},
                device_scale_factor=job.dpi / 96.0,
                locale="zh-CN",
            )
            context.set_offline(True)
            page = context.new_page()
            page.set_content(_PAGE_HTML, wait_until="domcontentloaded")
            for asset in assets:
                page.add_script_tag(path=str(asset))
            result = page.evaluate(
                _RENDER_SCRIPT,
                {
                    "sourceBase64": base64.b64encode(job.source_bytes).decode("ascii"),
                    "blockKind": job.block_kind,
                    "blockPath": job.block_path,
                    "drawingGroup": job.drawing_group,
                    "timeoutMs": max(1, job.timeout_seconds) * 1000,
                },
            )
            captures = result.get("captures") if isinstance(result, Mapping) else None
            if not isinstance(captures, list) or not captures:
                raise DocxTableImageError("DOCX 文档块没有可输出的图片区域")
            images: list[bytes] = []
            pages: list[int] = []
            for capture in captures:
                capture_id = str(capture.get("id") or "")
                if not capture_id:
                    raise DocxTableImageError("DOCX 文档块截图标识无效")
                images.append(page.locator(f"#{capture_id}").screenshot(type="png"))
                pages.append(int(capture.get("page") or 1))
            return images, pages
        except DocxTableImageError:
            raise
        except ImportError as exc:
            raise DocxTableImageError("当前运行环境缺少 Playwright 文档渲染依赖") from exc
        except Exception as exc:
            reason = str(exc)
            messages = {
                "DOCX_RENDER_TIMEOUT": "DOCX 文档块渲染超时",
                "DOCX_PAGE_LIMIT": "源文档页数超过文档块处理上限",
                "DOCX_TABLE_NOT_FOUND": "DOCX 中没有找到指定表格",
                "DOCX_DRAWING_NOT_FOUND": "DOCX 中没有找到指定浮动对象",
            }
            message = next(
                (value for marker, value in messages.items() if marker in reason),
                "DOCX 文档块浏览器渲染失败",
            )
            raise DocxTableImageError(message) from exc
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass


_BROWSER_RENDERER = _BrowserRenderer()
atexit.register(_BROWSER_RENDERER.close)


def _validate_options(
    *,
    block_index: int,
    dpi: int,
    padding_points: float,
) -> float:
    if (
        isinstance(block_index, bool)
        or not isinstance(block_index, int)
        or block_index < 0
    ):
        raise DocxTableImageError("文档块序号无效")
    if (
        isinstance(dpi, bool)
        or not isinstance(dpi, int)
        or not 72 <= dpi <= TABLE_IMAGE_MAX_DPI
    ):
        raise DocxTableImageError("文档块图片分辨率超出安全范围")
    if isinstance(padding_points, bool):
        raise DocxTableImageError("文档块图片边框留白参数无效")
    try:
        padding = float(padding_points)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DocxTableImageError("文档块图片边框留白参数无效") from exc
    if not math.isfinite(padding) or not 0.0 <= padding <= TABLE_IMAGE_MAX_PADDING_POINTS:
        raise DocxTableImageError("文档块图片边框留白超出安全范围")
    return padding


_DARK_CHANNEL_LUT = bytes(1 if value <= 220 else 0 for value in range(256))


def _trim_outer_frame(image: Any):
    """Remove browser rounding whitespace while retaining the full frame."""

    from PIL import ImageChops

    rgb = image.convert("RGB") if image.mode != "RGB" else image
    mask = None
    for channel in rgb.split():
        binary = channel.point(_DARK_CHANNEL_LUT)
        mask = binary if mask is None else ImageChops.darker(mask, binary)
    bbox = mask.getbbox() if mask is not None else None
    return rgb.crop(bbox) if bbox else rgb


def _save_fragments(
    fragments: list[bytes],
    output: Path,
    *,
    trim_outer_frame: bool,
) -> tuple[int, int, int]:
    try:
        from PIL import Image

        images = [Image.open(BytesIO(value)).convert("RGB") for value in fragments]
        if trim_outer_frame:
            images = [_trim_outer_frame(image) for image in images]
        width = max(image.width for image in images)
        height = sum(image.height for image in images)
        combined = Image.new("RGB", (width, height), "white")
        offset_y = 0
        for image in images:
            combined.paste(image, (0, offset_y))
            offset_y += image.height
        combined.save(output, format="PNG")
    except Exception as exc:
        raise DocxTableImageError("文档块 PNG 写入失败") from exc
    if output.is_symlink() or not output.is_file():
        raise DocxTableImageError("文档块 PNG 未成功写入")
    try:
        size_bytes = output.stat().st_size
    except OSError as exc:
        raise DocxTableImageError("无法确认文档块 PNG 大小") from exc
    if size_bytes <= 0 or size_bytes > TABLE_IMAGE_MAX_BYTES:
        raise DocxTableImageError("文档块 PNG 超过安全大小上限")
    return width, height, size_bytes


def render_docx_block_image(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    block_kind: str,
    block_index: int,
    dpi: int = TABLE_IMAGE_DEFAULT_DPI,
    timeout_seconds: int = TABLE_IMAGE_RENDER_TIMEOUT_SECONDS,
    padding_points: float = TABLE_IMAGE_DEFAULT_PADDING_POINTS,
) -> dict[str, Any]:
    """Render one zero-based DOCX table or adjacent floating-drawing group."""

    padding = _validate_options(
        block_index=block_index,
        dpi=dpi,
        padding_points=padding_points,
    )
    if block_kind not in {"table", "drawing_group"}:
        raise DocxTableImageError("不支持的 DOCX 文档块类型")
    source = _regular_path(source_path, suffix=".docx")
    output = _validate_output_path(output_path)
    root, theme = _read_document_xml(source)
    block_path: str | None = None
    drawing_value: Mapping[str, Any] | None = None
    if block_kind == "table":
        paths = _table_paths(root)
        if block_index >= len(paths):
            raise DocxTableImageError("解析出的表格序号超出源文档范围")
        block_path = paths[block_index]
    else:
        groups = _drawing_groups(root, theme)
        if block_index >= len(groups):
            raise DocxTableImageError("解析出的浮动图形组序号超出源文档范围")
        drawing_value = _drawing_group_browser_value(
            groups[block_index],
            padding_points=padding,
        )

    try:
        source_bytes = source.read_bytes()
    except OSError as exc:
        raise DocxTableImageError("DOCX 源文件无法读取") from exc
    fragments, pages = _BROWSER_RENDERER.render(_BrowserJob(
        source_bytes=source_bytes,
        block_kind=block_kind,
        block_path=block_path,
        drawing_group=drawing_value,
        dpi=dpi,
        timeout_seconds=max(1, int(timeout_seconds)),
    ))
    width, height, size_bytes = _save_fragments(
        fragments,
        output,
        trim_outer_frame=block_kind == "table",
    )
    return {
        "block_kind": block_kind,
        "block_index": block_index,
        "fragment_count": len(fragments),
        "pages": sorted(set(pages)),
        "width": width,
        "height": height,
        "size_bytes": size_bytes,
        "padding_points": padding,
        "path": str(output),
    }


def render_docx_table_image(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    table_index: int,
    dpi: int = TABLE_IMAGE_DEFAULT_DPI,
    timeout_seconds: int = TABLE_IMAGE_RENDER_TIMEOUT_SECONDS,
    padding_points: float = TABLE_IMAGE_DEFAULT_PADDING_POINTS,
) -> dict[str, Any]:
    """Backward-compatible wrapper for one source table image."""

    result = render_docx_block_image(
        source_path,
        output_path,
        block_kind="table",
        block_index=table_index,
        dpi=dpi,
        timeout_seconds=timeout_seconds,
        padding_points=padding_points,
    )
    return {**result, "table_index": table_index}


def render_docx_drawing_image(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    drawing_group_index: int,
    dpi: int = TABLE_IMAGE_DEFAULT_DPI,
    timeout_seconds: int = TABLE_IMAGE_RENDER_TIMEOUT_SECONDS,
    padding_points: float = TABLE_IMAGE_DEFAULT_PADDING_POINTS,
) -> dict[str, Any]:
    """Render one adjacent group of anchored DOCX drawings."""

    return render_docx_block_image(
        source_path,
        output_path,
        block_kind="drawing_group",
        block_index=drawing_group_index,
        dpi=dpi,
        timeout_seconds=timeout_seconds,
        padding_points=padding_points,
    )


__all__ = [
    "DocxTableImageError",
    "TABLE_IMAGE_DEFAULT_DPI",
    "TABLE_IMAGE_DEFAULT_PADDING_POINTS",
    "TABLE_IMAGE_MAX_BYTES",
    "TABLE_IMAGE_RENDERER_VERSION",
    "render_docx_block_image",
    "render_docx_drawing_image",
    "render_docx_table_image",
]

"""文档解析共享工具：段落加载、文本清理与英文句子切分。"""

import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from zipfile import ZipFile
from xml.etree import ElementTree

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.oxml.ns import qn


SCRIPT_MARKER_RE = re.compile(
    r'^\s*(?:[【\[（(]\s*)?'
    r'(?:录音稿|听力原文|录音原文)\s*'
    r'(?=\s|[：:】\]）)]|$)'
    r'\s*(?:[：:]\s*)?(?:[】\]）)]\s*)?(?:[：:]\s*)?'
    r'(.*)\s*$'
)
ANSWER_MARKER_RE = re.compile(
    r'^\s*(?:[【\[（(]\s*)?'
    r'(?:参考答案|答案|解析)\s*'
    r'(?=\s|[：:【\[（(】\]）)]|$)'
    r'(?:[：:]\s*)?(?:[】\]）)]\s*)?(?:[：:]\s*)?'
    r'(.*)\s*$'
)

_MAJOR_SECTION_NAMES = (
    r'(?:信息获取|回答问题|听选信息|听后选择|听后应答|'
    r'听后记录并转述信息|听后记录|信息转述及询问|信息转述|询问信息|'
    r'模仿朗读|课文跟读|词汇)'
)
_MAJOR_TYPE_SUFFIX = r'(?:题型?|试题)?'
MAJOR_SECTION_RE = re.compile(
    rf'^\s*(?:'
    rf'[一二三四五六七八九十百]+\s*[、.．)]|'
    rf'第[一二三四五六七八九十百\d]+节|'
    rf'Section\s+[A-Z]\b|'
    rf'{_MAJOR_SECTION_NAMES}{_MAJOR_TYPE_SUFFIX}'
    rf'(?=\s*(?:[（(【：:]|$))|'
    rf'(?:\d+|[一二三四五六七八九十百]+)\s*[、.．)]\s*'
    rf'{_MAJOR_SECTION_NAMES}{_MAJOR_TYPE_SUFFIX}'
    rf'(?=\s*(?:[（(【：:]|$))'
    rf')',
    re.IGNORECASE,
)
MAJOR_TYPE_HEADING_RE = re.compile(
    rf'^\s*(?:'
    rf'{_MAJOR_SECTION_NAMES}{_MAJOR_TYPE_SUFFIX}'
    rf'(?=\s*(?:[（(【：:]|$))|'
    rf'(?:\d+|[一二三四五六七八九十百]+)\s*[、.．)]\s*'
    rf'{_MAJOR_SECTION_NAMES}{_MAJOR_TYPE_SUFFIX}'
    rf'(?=\s*(?:[（(【：:]|$))'
    rf')',
    re.IGNORECASE,
)

_GENERIC_ORDINAL_HEADING_RE = re.compile(
    r'^\s*[一二三四五六七八九十百]+\s*[、.．)]\s*(?P<title>.*)$'
)
_SECTION_PREFIX_RE = re.compile(
    r'^\s*(?:第[一二三四五六七八九十百\d]+节|Section\s+[A-Z]\b)',
    re.IGNORECASE,
)
# 中文序号本身不是可靠的大题边界：很多试卷在题型内部用“一、/二、”
# 写操作步骤。只对明显是祈使句/操作提示的序号行降级，保留“二、其他题型”
# 这类没有已知题型名、但确实需要切断扫描范围的标题。
_ORDINAL_INSTRUCTION_RE = re.compile(
    r'^(?:请|根据|听(?:下面|第|以下|录音|短文|一段)|'
    r'现在(?:[，,\s]|$)|你(?:将|希望|可以|有)|每(?:个|道|小题|段)|'
    r'将|完成|填写|判断|说明|回答(?:第|下列|以下)|'
    r'选择(?:正确|下列|以下))'
)


def is_major_section_heading(text: Any) -> bool:
    """判断一行是否足以作为跨题型扫描的安全边界。

    ``MAJOR_SECTION_RE`` 保留了对通用中文序号标题的兼容，但中文序号也
    常用于题型内部提示。这里增加一个轻量语义过滤：已知题型、``第X节``
    和 ``Section A`` 直接通过；未知的中文序号标题只有在不像操作提示时
    才通过。这样既能隔离未知大题，也不会因“请根据提示……”提前截断题目。
    """

    value = str(text or '').strip()
    if not value or not MAJOR_SECTION_RE.match(value):
        return False
    if MAJOR_TYPE_HEADING_RE.match(value) or _SECTION_PREFIX_RE.match(value):
        return True
    ordinal = _GENERIC_ORDINAL_HEADING_RE.match(value)
    if ordinal is None:
        return False
    title = ordinal.group('title').strip()
    return not title or not _ORDINAL_INSTRUCTION_RE.match(title)


@dataclass(frozen=True)
class DocumentBlock:
    """一个按 Word 文档顺序排列的可复用结构块。

    ``load_paragraphs`` 继续只返回普通正文段落，避免改变专项解析器的
    既有契约；需要处理 Word 表格或文本框的调用方通过 ``include_blocks``
    获取这条结构流。文本框只选择 OOXML 的首选分支，避免 DrawingML 和
    VML 回退内容重复进入解析结果。
    """

    kind: str
    index: int
    text: str = ""
    fragments: tuple[str, ...] = ()
    style: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


def match_script_marker(text: Any):
    """匹配统一的录音稿标签，并返回其余内容。

    不同来源把同一个边界写成“录音稿”“听力原文”或“录音原文”。
    题型解析器只应依赖这个共享入口，避免为每种题型复制正则。
    """

    return SCRIPT_MARKER_RE.match(str(text or '').strip())


def match_answer_marker(text: Any):
    """匹配参考答案/答案/解析标签，并返回标签后的内容。"""

    return ANSWER_MARKER_RE.match(str(text or '').strip())


# ============================================================================
# 工具函数
# ============================================================================

def _paragraph_heading_hint(paragraph):
    """返回 Word 段落是否带有明显的标题格式提示。

    课文跟读的新样本经常把小标题保留为 ``Normal`` 段落样式，但通过
    粗体或较大的字号区分。解析器原来只读取文本，导致无法区分“短标题”
    和“没有句末标点的短正文”。这里只记录保守的格式提示，显式 ``//``
    标记和文本规则仍由上层解析器处理。
    """
    style_name = str(paragraph.style.name if paragraph.style else "")
    if re.search(r"(?:heading|title|subtitle|标题|副标题)", style_name, re.I):
        return True

    runs = [run for run in paragraph.runs if str(run.text or "").strip()]
    if not runs:
        return False

    # 只有整段基本都被标成粗体/大字号时才作为标题提示，避免正文中
    # 单独加粗一个单词就改变音频边界。
    if all(run.bold is True for run in runs):
        return True

    sizes = [run.font.size.pt for run in runs if run.font.size is not None]
    if len(sizes) == len(runs) and sizes and min(sizes) >= 14:
        return True
    return False


def _build_numbering_model(document, filepath=None):
    """读取 Word 有序列表的起始值和编号格式。

    ``python-docx`` 的 ``Paragraph.text`` 不包含 Word 自动生成的列表编号。
    试卷模板有时把题号交给 ``w:numPr``，所以这里从 ``numbering.xml`` 建立
    一个尽量小的读取模型，供段落元数据记录实际显示的十进制题号。
    """
    try:
        numbering = document.part.numbering_part.element
    except (AttributeError, KeyError, NotImplementedError, ValueError):
        # 部分 Word 导出的文件虽然包含 ``word/numbering.xml``，却没有在
        # document.xml.rels 中建立 numbering relationship。python-docx 会在
        # 这种情况下尝试创建新 NumberingPart 并抛出 NotImplementedError，
        # 因此回退到 docx 包的原始 XML 读取。
        if filepath is None:
            return {}, {}, {}
        try:
            with ZipFile(filepath) as archive:
                numbering = ElementTree.fromstring(
                    archive.read("word/numbering.xml")
                )
        except (KeyError, OSError, ElementTree.ParseError):
            return {}, {}, {}

    starts = {}
    formats = {}
    for abstract in numbering.findall(qn("w:abstractNum")):
        abstract_id = abstract.get(qn("w:abstractNumId"))
        if abstract_id is None:
            continue
        for level in abstract.findall(qn("w:lvl")):
            level_id = level.get(qn("w:ilvl"), "0")
            start_element = level.find(qn("w:start"))
            format_element = level.find(qn("w:numFmt"))
            try:
                start = int(
                    start_element.get(qn("w:val"))
                    if start_element is not None
                    else 1
                )
            except (AttributeError, TypeError, ValueError):
                start = 1
            starts[(abstract_id, level_id)] = start
            formats[(abstract_id, level_id)] = (
                format_element.get(qn("w:val"))
                if format_element is not None
                else "decimal"
            )

    definitions = {}
    for num in numbering.findall(qn("w:num")):
        num_id = num.get(qn("w:numId"))
        abstract_element = num.find(qn("w:abstractNumId"))
        abstract_id = (
            abstract_element.get(qn("w:val"))
            if abstract_element is not None
            else None
        )
        if num_id is None or abstract_id is None:
            continue

        overrides = {}
        for override in num.findall(qn("w:lvlOverride")):
            level_id = override.get(qn("w:ilvl"), "0")
            start_element = override.find(qn("w:startOverride"))
            if start_element is None:
                level = override.find(qn("w:lvl"))
                if level is not None:
                    start_element = level.find(qn("w:start"))
            if start_element is not None:
                try:
                    overrides[level_id] = int(start_element.get(qn("w:val")))
                except (AttributeError, TypeError, ValueError):
                    pass
        definitions[num_id] = (abstract_id, overrides)

    return starts, formats, definitions


def _paragraph_numbering_number(paragraph, numbering_model, counters):
    """返回段落的自动十进制编号；手工题号或项目符号返回 ``None``。"""
    paragraph_properties = paragraph._p.pPr
    num_properties = (
        paragraph_properties.numPr if paragraph_properties is not None else None
    )
    if num_properties is None:
        return None

    num_id_element = num_properties.find(qn("w:numId"))
    if num_id_element is None:
        return None
    num_id = num_id_element.get(qn("w:val"))
    # numId=0 表示没有可用的编号定义；新版试卷的其他提示段落也会带上它。
    if not num_id or num_id == "0":
        return None

    level_element = num_properties.find(qn("w:ilvl"))
    level_id = (
        level_element.get(qn("w:val"))
        if level_element is not None
        else "0"
    )
    starts, formats, definitions = numbering_model
    definition = definitions.get(num_id)
    if definition is None:
        return None
    abstract_id, overrides = definition
    number_format = formats.get((abstract_id, level_id), "decimal")
    if number_format not in {"decimal", "decimalZero"}:
        return None

    start = overrides.get(
        level_id,
        starts.get((abstract_id, level_id), 1),
    )
    counter_key = (num_id, level_id)
    number = counters.get(counter_key, start - 1) + 1
    counters[counter_key] = number

    # 进入更高层级后，较低层级通常会重新从起点计数。
    for key in tuple(counters):
        if key[0] == num_id and key[1] > level_id:
            del counters[key]
    return number


_MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
_W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_A_NS = 'http://schemas.openxmlformats.org/drawingml/2006/main'


def _xml_text(node):
    """读取 OOXML 文本节点，同时兼容 Word/DrawingML 文本。"""

    values = []
    for child in node.iter():
        if child.tag in (f'{{{_W_NS}}}t', f'{{{_A_NS}}}t'):
            values.append(child.text or '')
        elif child.tag in (
            f'{{{_W_NS}}}tab',
            f'{{{_W_NS}}}br',
            f'{{{_W_NS}}}cr',
            f'{{{_A_NS}}}br',
        ):
            values.append('\n' if child.tag != f'{{{_W_NS}}}tab' else '\t')
    return ''.join(values).strip()


def _textbox_fragments(paragraph_element):
    """返回一个图形段落中按视觉/结构顺序排列的文本框内容。

    Office 文档经常在 ``mc:Choice`` 与 ``mc:Fallback`` 中各存一份同样
    的文本框。优先读取 Choice，只有不存在时才读取 Fallback，防止一题
    被解析两次。
    """

    def textbox_text(textbox):
        lines = []
        for paragraph in textbox.findall(f'./{{{_W_NS}}}p'):
            text = _xml_text(paragraph)
            if text:
                lines.append(text)
        if not lines:
            text = _xml_text(textbox)
            if text:
                lines.append(text)
        return '\n'.join(lines).strip()

    def node_key(node):
        # lxml 可能为同一个底层节点返回不同的 Python proxy；不能用
        # ``id(node)`` 跨两次遍历做去重，否则 Fallback 会再次进入结果。
        try:
            return node.getroottree().getpath(node)
        except AttributeError:
            return id(node)

    # 一个 w:p 里可能有多个独立的 AlternateContent。只查找第一个
    # Choice/Fallback 会漏掉同一段中的后续文本框，因此先为每个
    # AlternateContent 选出唯一分支，再按 XML 原顺序统一收集文本框。
    alternate_contents = paragraph_element.findall(
        f'.//{{{_MC_NS}}}AlternateContent'
    )
    alternate_nodes = {
        node_key(node)
        for alternate in alternate_contents
        for node in alternate.iter()
    }
    selected_textboxes = set()
    for alternate in alternate_contents:
        selected_branch = None
        # Choice 可能存在但为空（例如某些导出器只写了 AlternateContent
        # 壳），此时继续尝试 VML 回退分支。
        for branch_name in ('Choice', 'Fallback'):
            branch = alternate.find(f'./{{{_MC_NS}}}{branch_name}')
            if branch is None:
                continue
            branch_textboxes = branch.findall(f'.//{{{_W_NS}}}txbxContent')
            if any(textbox_text(textbox) for textbox in branch_textboxes):
                selected_branch = branch
                break
        if selected_branch is not None:
            selected_textboxes.update(
                node_key(node)
                for node in selected_branch.iter()
                if node.tag == f'{{{_W_NS}}}txbxContent'
            )

    fragments = []
    for textbox in paragraph_element.iter(f'{{{_W_NS}}}txbxContent'):
        textbox_id = node_key(textbox)
        # AlternateContent 中只保留选中的分支；没有 AlternateContent
        # 包裹的老式 VML 文本框全部保留。
        if textbox_id in alternate_nodes and textbox_id not in selected_textboxes:
            continue
        value = textbox_text(textbox)
        if value:
            fragments.append(value)
    return tuple(fragments)


def _table_fragments(table):
    """读取表格单元格文本并去除合并单元格的重复映射。"""

    fragments = []
    seen_cells = set()
    for row in table.rows:
        for cell in row.cells:
            cell_key = cell._tc.getroottree().getpath(cell._tc)
            if cell_key in seen_cells:
                continue
            seen_cells.add(cell_key)
            value = str(cell.text or '').strip()
            if value:
                fragments.append(value)
    return tuple(fragments)


def _build_document_blocks(document):
    """从同一个 ``Document`` 实例构建顺序稳定的结构块流。"""

    blocks = []
    paragraph_index = 0
    block_index = 0
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn('w:p'):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if text:
                style = paragraph.style.name if paragraph.style else ''
                blocks.append(DocumentBlock(
                    kind='paragraph',
                    index=block_index,
                    text=text,
                    fragments=(text,),
                    style=style,
                    metadata={'paragraph_index': paragraph_index},
                ))
                block_index += 1

            textbox_fragments = _textbox_fragments(child)
            if textbox_fragments:
                blocks.append(DocumentBlock(
                    kind='textbox',
                    index=block_index,
                    text='\n\n'.join(textbox_fragments),
                    fragments=textbox_fragments,
                    metadata={'paragraph_index': paragraph_index},
                ))
                block_index += 1
            paragraph_index += 1
            continue

        if child.tag == qn('w:tbl'):
            table = Table(child, document)
            fragments = _table_fragments(table)
            if fragments:
                blocks.append(DocumentBlock(
                    kind='table',
                    index=block_index,
                    text='\n'.join(fragments),
                    fragments=fragments,
                ))
                block_index += 1

    return tuple(blocks)


def load_paragraphs(filepath, *, include_metadata=False, include_blocks=False):
    """
    加载 Word 文档，返回非空段落列表。
    每个元素为 (原始索引, 文本, 样式名)。
    ``include_metadata=True`` 时额外返回与段落一一对应的格式提示列表。
    ``include_blocks=True`` 时再返回按文档顺序排列的 ``DocumentBlock``
    序列，用于复用同一次加载得到的表格和文本框内容。
    """
    doc = Document(filepath)
    if include_blocks:
        # 结构块是可选增强能力。某个异常表格/形状不能让普通正文解析
        # 整体失败，失败时保留段落结果并让依赖结构块的解析器自然降级。
        try:
            blocks = _build_document_blocks(doc)
        except Exception:
            blocks = ()
    else:
        blocks = ()
    numbering_model = _build_numbering_model(doc, filepath)
    numbering_counters = {}
    result = []
    metadata = []
    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip()
        if not text:
            # 空的 Word 列表占位段没有可展示题号，不能推进自动编号计数；
            # 否则后面的第一道题会被错误地记录成 2 号题。
            continue
        numbering_number = _paragraph_numbering_number(
            para,
            numbering_model,
            numbering_counters,
        )
        style = para.style.name if para.style else ""
        result.append((i, text, style))
        paragraph_metadata = {
            "heading_hint": _paragraph_heading_hint(para),
        }
        if numbering_number is not None:
            paragraph_metadata["numbering_number"] = numbering_number
        metadata.append(paragraph_metadata)
    if include_blocks:
        return result, metadata, blocks
    if include_metadata:
        return result, metadata
    return result


def is_chinese(text):
    """判断文本是否以中文为主（CJK 字符占比 > 30%）"""
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    total = len(text.replace(' ', '').replace('\n', ''))
    return total > 0 and cjk / total > 0.3


def clean_whitespace(text):
    """规范化空白：合并连续空格，去除行首尾空格，保留换行"""
    lines = text.split('\n')
    cleaned = [re.sub(r'[ \t\u00a0]+', ' ', line.strip()) for line in lines]
    return '\n'.join(l for l in cleaned if l)


def remove_zero_width(text):
    """移除零宽字符、BOM 等不可见字符"""
    for ch in ('\u200b', '\u200c', '\u200d', '\ufeff', '\u2060'):
        text = text.replace(ch, '')
    return text


def sanitize(text):
    """综合文本清理"""
    return clean_whitespace(remove_zero_width(text))


_SENTENCE_ABBREVIATIONS = frozenset({
    "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.",
    "vs.", "etc.", "e.g.", "i.e.", "no.", "fig.", "inc.", "p.s.",
})


def _is_sentence_abbreviation(current, normalized, punctuation_index):
    """判断当前句点是否属于缩写，而不是句末标点。"""
    match = re.search(r"([A-Za-z](?:[A-Za-z.]*)\.)$", current.rstrip())
    token = match.group(1).casefold() if match else ""
    if token in _SENTENCE_ABBREVIATIONS:
        return True

    # U.S.、e.g.、p.m. 等点号缩写在扫描到第一个点时，后面仍紧跟着
    # “字母 + 点”；先不切分，等整个缩写扫描完再继续判断。
    next_index = punctuation_index + 1
    if (
        next_index + 1 < len(normalized)
        and normalized[next_index].isalpha()
        and normalized[next_index + 1] == "."
    ):
        return True
    if re.fullmatch(r"(?:[A-Za-z]\.){2,}", token):
        return True
    return False


def split_sentences(text):
    """将英文文本按句子切分。

    使用状态机跟踪引号开闭，确保：
    - 引号内部的 . ! ? 不会触发切分
    - 闭合引号后跟大写字母时才切分（新句开始）
    - 非引号环境下的 . ! ? 后跟大写字母/开引号时切分
    - 常见称谓、缩写和点号缩写（如 Mr.、e.g.、U.S.）不误切分

    支持直引号 (") 和智能引号 (\u201c \u201d)。
    用于段落跟读和语篇跟读的逐句录音。
    """
    normalized = re.sub(r'\s+', ' ', text.strip())
    if not normalized:
        return []

    sentences = []
    current = ""
    in_quote = False          # 是否在引号内部
    quote_char = None         # 当前引号类型: '"' 或 '\u201c'

    i = 0
    n = len(normalized)
    while i < n:
        ch = normalized[i]

        # ---- 智能左引号 \u201c ----
        if ch == '\u201c':
            in_quote = True
            quote_char = '\u201c'
            current += ch
            i += 1
            continue

        # ---- 智能右引号 \u201d ----
        if ch == '\u201d':
            in_quote = False
            quote_char = None
            current += ch
            # 仅当引号内以 . ! ? 结尾时，才检查是否需要切分
            # （逗号结尾如 “Hello,” she said. 不切分）
            if len(current) >= 2 and current[-2] in '.!?':
                j = i + 1
                while j < n and normalized[j] == ' ':
                    j += 1
                if j < n and normalized[j].isupper():
                    sentences.append(current.strip())
                    current = ""
                    i = j
                    continue
            i += 1
            continue

        # ---- 直引号 " → 切换状态 ----
        if ch == '"':
            if not in_quote:
                # 开引号
                in_quote = True
                quote_char = '"'
                current += ch
                i += 1
                continue
            else:
                # 闭合引号
                in_quote = False
                quote_char = None
                current += ch
                # 仅当引号内以 . ! ? 结尾时，才检查是否需要切分
                # （逗号结尾如 "Hello," she said. 不切分）
                if len(current) >= 2 and current[-2] in '.!?':
                    j = i + 1
                    while j < n and normalized[j] == ' ':
                        j += 1
                    if j < n and normalized[j].isupper():
                        sentences.append(current.strip())
                        current = ""
                        i = j
                        continue
                i += 1
                continue

        # ---- 句末标点 . ! ? ----
        if ch in '.!?':
            current += ch
            if not in_quote:
                if ch == '.' and _is_sentence_abbreviation(current, normalized, i):
                    i += 1
                    continue
                # 向前吞掉闭合括号/方括号（不吞引号，引号单独处理）
                j = i + 1
                while j < n and normalized[j] in ')]':
                    current += normalized[j]
                    j += 1
                # 跳过空白
                k = j
                while k < n and normalized[k] == ' ':
                    k += 1
                # 后面是大写字母或开引号 → 切分
                if k >= n:
                    # 文本结束，剩余部分由末尾兜底
                    i = k
                    continue
                if normalized[k].isupper() or normalized[k] in '\u201c"':
                    sentences.append(current.strip())
                    current = ""
                    i = k
                    continue
                # 没有切分 → 继续从 j 开始
                i = j
                continue
            # 引号内部：不切分，继续
            i += 1
            continue

        current += ch
        i += 1

    if current.strip():
        sentences.append(current.strip())

    if not sentences:
        sentences.append(normalized)
    return sentences

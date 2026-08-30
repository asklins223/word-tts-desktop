"""文档解析共享工具：段落加载、文本清理与英文句子切分。"""

import re

from docx import Document


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


def load_paragraphs(filepath, *, include_metadata=False):
    """
    加载 Word 文档，返回非空段落列表。
    每个元素为 (原始索引, 文本, 样式名)。
    ``include_metadata=True`` 时额外返回与段落一一对应的格式提示列表。
    """
    doc = Document(filepath)
    result = []
    metadata = []
    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip()
        if text:
            style = para.style.name if para.style else ""
            result.append((i, text, style))
            metadata.append({"heading_hint": _paragraph_heading_hint(para)})
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

#!/usr/bin/env python3
"""
Word 文档解析脚本
=================
从 /word 文件夹下的 Word 文档中提取各类英语听说考试素材。

支持文档类型：
  1. 信息获取        — 提取「听选信息」「回答问题」的题目与录音稿
  2. 课文跟读        — 提取句子跟读（去序号、按序号排序）、段落跟读、语篇跟读
  3. 信息转述及询问   — 提取「第一节 信息转述」的录音稿
  4. 模仿朗读        — 提取每个单元的「外网」(2篇) 和「教材」(1篇)
  5. 词汇            — 预留接口，未来接入

设计说明：
  - 每种文档类型对应一个 Parser 子类，通过文件名自动识别
  - 使用状态机 + 循环遍历段落，天然支持同一文件内包含多篇试题
  - 循环终止条件为段落列表遍历完毕，辅以章节标记进行状态切换
  - 输出结构化 JSON，方便后续 TTS 处理

用法:
    python word_parser.py

输出:
    word_parsed/parsed_results.json
"""

import os
import re
import json
from docx import Document

# ============================================================================
# 路径配置
# ============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORD_DIR = os.path.join(BASE_DIR, "word")
OUTPUT_DIR = os.path.join(BASE_DIR, "word_parsed")
# OUTPUT_DIR 仅在 __main__ 独立运行时使用；打包模式下 .app 内部只读，
# 跳过创建以避免 PermissionError 导致 import 失败。
try:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
except (PermissionError, OSError):
    pass


# ============================================================================
# 工具函数
# ============================================================================

def load_paragraphs(filepath):
    """
    加载 Word 文档，返回非空段落列表。
    每个元素为 (原始索引, 文本, 样式名)。
    """
    doc = Document(filepath)
    result = []
    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip()
        if text:
            style = para.style.name if para.style else ""
            result.append((i, text, style))
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


# 句末标点（可带闭合引号/括号）后跟空白和大写字母/开引号 → 切分点
_RE_SENTENCE_END = re.compile(
    r"([.!?][\u201d\u2019\u0022\u0027\u0029\u005d]?)\s+(?=[A-Z\u201c\u0022])"
)


def split_sentences(text):
    """将英文文本按句子切分。

    在 . ! ? 后切分，正确处理闭合引号和括号。
    用于段落跟读和语篇跟读的逐句录音。
    """
    normalized = re.sub(r'\s+', ' ', text.strip())
    if not normalized:
        return []
    parts = _RE_SENTENCE_END.split(normalized)
    sentences = []
    for i in range(0, len(parts), 2):
        sentence = parts[i]
        if i + 1 < len(parts):
            sentence += parts[i + 1]
        sentence = sentence.strip()
        if sentence:
            sentences.append(sentence)
    if not sentences:
        # 无句末标点时，整段作为一条
        sentences.append(normalized)
    return sentences


# ============================================================================
# 解析器基类
# ============================================================================

class BaseParser:
    """Word 文档解析器基类，子类需实现 parse() 方法。"""

    DOC_TYPE = "未知"

    def __init__(self, filepath):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.paras = load_paragraphs(filepath)

    def parse(self):
        """子类实现：返回解析结果字典"""
        raise NotImplementedError

    def _result(self, items):
        """构造标准输出结构"""
        return {
            "source_file": self.filename,
            "doc_type": self.DOC_TYPE,
            "item_count": len(items),
            "items": items,
        }


# ============================================================================
# 1. 信息获取解析器
# ============================================================================

class InfoAcquisitionParser(BaseParser):
    """
    解析「信息获取」文档。
    提取「第一节 听选信息」和「第二节 回答问题」中的题目及每段录音稿。

    题目规则：
      - 两节共用文档中的连续题号，题号不会在第二节重新开始
      - 每道题单独生成一条结果，文本去掉题号
      - 按题目出现顺序使用男声、女声交替朗读（第一题为男声）
      - filename_stem 使用"问题x"，供 TTS 阶段生成问题1.mp3等文件

    文档结构示例:
        第一节 听选信息
          听第一段对话，回答第1—2两个问题。
          1. Question?
          (Options)
          录音稿：
          W: ...
          M: ...
          听第二段对话，回答第3—4两个问题。
          ...
        第二节 回答问题
          听下面一段独白，录音播放两遍。
          7. Question?
          ...
          录音稿：
          (W) ...
        参考答案               ← 到此结束

    支持同一文件内包含多篇试题（多次出现「第一节 听选信息」）。
    """

    DOC_TYPE = "信息获取"

    # 第一节 听选信息
    RE_SECTION_START = re.compile(r'第一节\s*听选信息')
    # 第二节 回答问题
    RE_SECTION2_START = re.compile(r'第二节\s*回答问题')
    # 参考答案 → 结束所有收集
    RE_SECTION_END = re.compile(r'参考答案')
    # 任何「第X节」标记（用于检测其他题型的章节边界）
    RE_ANY_SECTION = re.compile(r'第[一二三四五六七八九十]+节')
    # 录音稿：（可能后面紧跟内容，也可能单独一行）
    RE_SCRIPT = re.compile(r'录音稿\s*[：:]\s*(.*)')
    # 听第X段对话 → 上一段录音稿结束
    RE_DIALOG = re.compile(r'听第.+段对话')
    # 题目编号：1. / 1． / 1、 / 1) / 1）
    RE_QUESTION = re.compile(r'^(\d+)\s*[.．、）)]\s*(.+)')

    def parse(self):
        items = []
        in_section = False       # 是否在任一节内
        current_category = ""    # 当前节的 category
        collecting = False       # 是否正在收集录音稿
        current_lines = []       # 当前录音稿的文本行
        question_order = 0       # 当前试题内的题目顺序；第二节不重置
        # 每节独立编号
        idx_by_cat = {}          # {category: count}

        def flush():
            nonlocal collecting, current_lines
            if collecting and current_lines:
                cat = current_category
                idx_by_cat[cat] = idx_by_cat.get(cat, 0) + 1
                items.append({
                    "category": cat,
                    "index": idx_by_cat[cat],
                    "text": sanitize('\n'.join(current_lines)),
                })
            collecting = False
            current_lines = []

        for _, text, _ in self.paras:
            # ---- 第一节 听选信息 开始 ----
            if self.RE_SECTION_START.search(text):
                flush()
                in_section = True
                current_category = "听选信息录音稿"
                question_order = 0
                continue

            # ---- 第二节 回答问题 开始 ----
            if self.RE_SECTION2_START.search(text):
                flush()
                in_section = True
                current_category = "回答问题录音稿"
                continue

            # ---- 其他「第X节」→ 遇到其他题型的章节，停止采集 ----
            if in_section and self.RE_ANY_SECTION.search(text):
                # 不是自己的章节标记，停止采集
                if not self.RE_SECTION_START.search(text) and not self.RE_SECTION2_START.search(text):
                    flush()
                    in_section = False
                    current_category = ""
                    continue

            # ---- 参考答案 → 结束所有收集 ----
            if in_section and self.RE_SECTION_END.search(text):
                flush()
                in_section = False
                current_category = ""
                continue

            if not in_section:
                continue

            # ---- 题目：去题号，按出现顺序男/女交替 ----
            # 只在非录音稿状态识别，避免把录音稿中偶然以数字开头的句子误判为题目。
            question_match = self.RE_QUESTION.match(text) if not collecting else None
            if question_match:
                question_number = int(question_match.group(1))
                question_text = sanitize(question_match.group(2))
                if question_text:
                    question_order += 1
                    speaker = "M" if question_order % 2 == 1 else "W"
                    section_name = (
                        "听选信息" if current_category == "听选信息录音稿" else "回答问题"
                    )
                    items.append({
                        "category": f"{section_name}题目",
                        "number": question_number,
                        "filename_stem": f"问题{question_number}",
                        "voice": "male" if speaker == "M" else "female",
                        "text": f"{speaker}: {question_text}",
                    })
                continue

            # ---- 录音稿标记 ----
            m = self.RE_SCRIPT.match(text)
            if m:
                flush()  # 先保存上一段录音稿
                collecting = True
                remainder = m.group(1).strip()
                if remainder:
                    current_lines.append(remainder)
                continue

            # ---- 新对话开始 → 当前录音稿结束 ----
            if collecting and self.RE_DIALOG.search(text):
                flush()
                continue

            # ---- 收集录音稿行 ----
            if collecting:
                current_lines.append(text)

        # 文档末尾兜底
        flush()

        return self._result(items)


# ============================================================================
# 2. 课文跟读解析器
# ============================================================================

class TextReadingParser(BaseParser):
    """
    解析「课文跟读」文档。
    提取三类内容：
      - 句子跟读：去掉序号前缀，按序号排序
      - 段落跟读：整段英文
      - 语篇跟读：按「语篇N」分组，每组可含多段

    音色/命名规则（仅 Understanding Idea / Reading for writing 章节）：
      语速和停顿由前端动态配置，解析器不固定。
      Understanding Idea (前缀 U)：
        - 句子跟读：男声，命名 U-句子1、U-句子2 …
        - 段落跟读：男声，逐句切分，命名 U-段落1、U-段落2 …
        - 语篇1：男声，逐句切分，命名 U-语篇1-1、U-语篇1-2 …
        - 语篇2：女声，逐句切分，命名 U-语篇2-1、U-语篇2-2 …
        - 语篇（仅一篇时）：男声，逐句切分，命名 U-语篇1-1、U-语篇1-2 …
      Reading for writing (前缀 R)：
        - 句子跟读：男声，命名 R-句子1、R-句子2 …
        - 段落跟读：男声，逐句切分，命名 R-段落1、R-段落2 …
        - 语篇跟读：女声，逐句切分，命名 R-语篇1-1、R-语篇1-2 …

    文档结构示例（两种标题层级均支持）：
        格式一（旧）：
        [Heading 1] 课文跟读-7上U1
        [Heading 2] Understanding Idea
          [Heading 3] 句子跟读
            1. English sentence
            中文：翻译
            2. English sentence
            ...
          [Heading 3] 段落跟读
            English paragraph
          [Heading 3] 语篇跟读
            语篇1
            English paragraph 1
            English paragraph 2
            语篇2
            ...
        [Heading 2] Reading for writing
          ...

        格式二（新）：
        [Normal] 课文跟读-7 上 U2
        [Heading 1] Understanding Idea
          [Heading 2] 句子跟读
            ...
          [Heading 2] 段落跟读
            ...
          [Heading 2] 语篇跟读
            English paragraph 1
            English paragraph 2
            （无显式「语篇N」标记，仅一篇时自动归为一组）
        [Heading 1] Reading for writing
          ...

    支持同一文件内包含多个单元/章节。
    """

    DOC_TYPE = "课文跟读"

    # 子章节标记（精确匹配）
    SUB_SENTENCE = "句子跟读"
    SUB_PARAGRAPH = "段落跟读"
    SUB_DISCOURSE = "语篇跟读"

    # 编号句：1. / 1、 / 1) / 1） 后跟内容
    RE_NUMBERED = re.compile(r'^(\d+)\s*[.、）)]\s*(.+)')
    # 语篇编号：语篇1 / 语篇 1 / 语篇1：
    RE_DISCOURSE_NUM = re.compile(r'^语篇\s*(\d+)\s*[：:]?\s*$')
    # 中文翻译行
    RE_CHINESE_PREFIX = re.compile(r'^中文\s*[：:]')
    # 已知的章节名（用于内容匹配，不依赖样式）
    KNOWN_SECTIONS = {
        'understanding idea', 'reading for writing',
        'developing ideas', 'developing idea',
    }

    @staticmethod
    def _section_prefix(section):
        """根据章节名返回命名前缀，未知章节返回空字符串。"""
        s = section.strip().lower()
        if 'understanding' in s:
            return "U"
        if 'reading' in s:
            return "R"
        return ""

    @classmethod
    def _discourse_voice(cls, section, discourse_number):
        """确定语篇跟读的音色。
        Understanding Idea: 语篇1→男声, 语篇2→女声
        Reading for writing: 全部女声
        """
        prefix = cls._section_prefix(section)
        if prefix == "U":
            return "male" if discourse_number == 1 else "female"
        if prefix == "R":
            return "female"
        return "female"

    # 子章节名称集合，用于排除 heading 样式误匹配
    _SUB_SECTION_NAMES = frozenset({SUB_SENTENCE, SUB_PARAGRAPH, SUB_DISCOURSE})

    def parse(self):
        items = []
        current_sub = None        # 当前子章节类型
        current_section = ""      # 当前章节名

        # 句子跟读临时存储: [(序号, 英文), ...]
        sentence_buf = []
        # 段落跟读临时存储: [行, ...]
        paragraph_buf = []
        # 语篇跟读临时存储: {序号: [行, ...]}
        discourse_buf = {}

        def flush_sentences():
            nonlocal sentence_buf
            if sentence_buf:
                prefix = self._section_prefix(current_section)
                sentence_buf.sort(key=lambda x: x[0])
                for num, text in sentence_buf:
                    item = {
                        "category": "句子跟读",
                        "section": current_section,
                        "number": num,
                        "text": text,
                    }
                    if prefix:
                        item["voice"] = "male"
                        item["filename_stem"] = f"{prefix}-句子{num}"
                    items.append(item)
                sentence_buf = []

        def flush_paragraph():
            nonlocal paragraph_buf
            if paragraph_buf:
                prefix = self._section_prefix(current_section)
                full_text = sanitize('\n'.join(paragraph_buf))
                # 逐句切分，每句一个音频文件
                sentences = split_sentences(full_text)
                if not sentences:
                    sentences = [full_text]
                for sent_idx, sent_text in enumerate(sentences, 1):
                    item = {
                        "category": "段落跟读",
                        "section": current_section,
                        "sentence_number": sent_idx,
                        "text": sent_text,
                    }
                    if prefix:
                        item["voice"] = "male"
                        item["filename_stem"] = f"{prefix}-段落{sent_idx}"
                    items.append(item)
                paragraph_buf = []

        def flush_discourse():
            nonlocal discourse_buf
            if discourse_buf:
                prefix = self._section_prefix(current_section)
                discourse_count = len(discourse_buf)
                for num in sorted(discourse_buf.keys()):
                    lines = discourse_buf[num]
                    if not lines:
                        continue
                    full_text = sanitize('\n'.join(lines))
                    # 确定音色和显示编号
                    if prefix == "U":
                        if discourse_count == 1:
                            voice = "male"
                            display_num = 1  # 仅一篇时也用「语篇1」
                        else:
                            voice = self._discourse_voice(current_section, num)
                            display_num = num
                    elif prefix == "R":
                        voice = self._discourse_voice(current_section, num)
                        display_num = 1  # R 章节仅一篇，用「语篇1」
                    else:
                        voice = "female"
                        display_num = max(num, 1)
                    # 逐句切分，每句一个音频文件
                    sentences = split_sentences(full_text)
                    if not sentences:
                        sentences = [full_text]
                    for sent_idx, sent_text in enumerate(sentences, 1):
                        item = {
                            "category": "语篇跟读",
                            "section": current_section,
                            "discourse_number": num,
                            "sentence_number": sent_idx,
                            "text": sent_text,
                        }
                        if prefix:
                            item["voice"] = voice
                            item["filename_stem"] = (
                                f"{prefix}-语篇{display_num}-{sent_idx}"
                            )
                        items.append(item)
                discourse_buf = {}

        def flush_all():
            flush_sentences()
            flush_paragraph()
            flush_discourse()

        for _, text, style in self.paras:
            # ---- 子章节标记（优先于 Heading 检查，兼容新格式中
            #      子章节使用 Heading 2 样式的情况）----
            if text == self.SUB_SENTENCE:
                flush_all()
                current_sub = self.SUB_SENTENCE
                continue
            if text == self.SUB_PARAGRAPH:
                flush_all()
                current_sub = self.SUB_PARAGRAPH
                continue
            if text == self.SUB_DISCOURSE:
                flush_all()
                current_sub = self.SUB_DISCOURSE
                continue

            # ---- Heading：新章节 ----
            if self._is_section_heading(style, text):
                flush_all()
                current_section = text
                current_sub = None
                continue

            # ---- 根据子章节处理内容 ----
            if current_sub == self.SUB_SENTENCE:
                self._handle_sentence(text, sentence_buf)

            elif current_sub == self.SUB_PARAGRAPH:
                # 跳过中文翻译行
                if not self.RE_CHINESE_PREFIX.match(text):
                    paragraph_buf.append(text)

            elif current_sub == self.SUB_DISCOURSE:
                self._handle_discourse(text, discourse_buf)

        # 文档末尾兜底
        flush_all()
        return self._result(items)

    def _is_section_heading(self, style, text):
        """判断是否为章节标题（Understanding Idea / Reading for writing 等）。

        兼容两种文档格式：
        - 旧格式：章节为 Heading 2，子章节为 Heading 3
        - 新格式：章节为 Heading 1，子章节为 Heading 2

        策略：
        1. 已知章节名直接匹配（不依赖样式）
        2. Heading 1/2 样式且不是子章节名时，也视为章节标题
        """
        # 已知章节名直接匹配
        if text.strip().lower() in self.KNOWN_SECTIONS:
            return True
        # Heading 1 或 Heading 2 样式（排除子章节名，避免误匹配）
        if style:
            sl = style.lower()
            if ('heading 1' in sl or 'heading 2' in sl):
                if text.strip() not in self._SUB_SECTION_NAMES:
                    return True
        return False

    def _handle_sentence(self, text, buf):
        """处理句子跟读的一个段落"""
        # 取第一行（段落内可能内嵌中文翻译换行）
        first_line = text.split('\n')[0].strip()
        m = self.RE_NUMBERED.match(first_line)
        if m:
            num = int(m.group(1))
            eng = m.group(2).strip()
            # 跳过纯中文行
            if eng and not is_chinese(eng):
                buf.append((num, eng))

    def _handle_discourse(self, text, buf):
        """处理语篇跟读的一个段落"""
        m = self.RE_DISCOURSE_NUM.match(text)
        if m:
            num = int(m.group(1))
            if num not in buf:
                buf[num] = []
        else:
            # 跳过中文翻译行，将英文内容追加到当前语篇
            if not self.RE_CHINESE_PREFIX.match(text) and buf:
                last_key = max(buf.keys())
                buf[last_key].append(text)
            elif not self.RE_CHINESE_PREFIX.match(text):
                # 没有显式「语篇N」标记时，归入默认组 0
                if 0 not in buf:
                    buf[0] = []
                buf[0].append(text)


# ============================================================================
# 3. 信息转述及询问解析器
# ============================================================================

class InfoRetellingParser(BaseParser):
    """
    解析「信息转述及询问」文档。
    提取「第一节 信息转述」的录音稿。

    文档结构示例:
        第一节 信息转述
          ... (题目描述)
          录音稿：(W) I'm Emma...     ← 录音稿可能与标记在同一行
          参考答案：...               ← 到此结束
        第二节 询问信息               ← 或到这里结束

    支持同一文件内包含多篇试题。
    """

    DOC_TYPE = "信息转述及询问"

    RE_SECTION_START = re.compile(r'第一节\s*信息转述')
    RE_SECTION_END = re.compile(r'第二节|参考答案')
    # 任何「第X节」标记（用于检测其他题型的章节边界）
    RE_ANY_SECTION = re.compile(r'第[一二三四五六七八九十]+节')
    RE_SCRIPT = re.compile(r'录音稿\s*[：:]\s*(.*)')

    def parse(self):
        items = []
        in_section = False
        collecting = False
        current_lines = []
        script_idx = 0

        def flush():
            nonlocal collecting, current_lines, script_idx
            if collecting and current_lines:
                script_idx += 1
                items.append({
                    "category": "信息转述录音稿",
                    "index": script_idx,
                    "text": sanitize('\n'.join(current_lines)),
                })
            collecting = False
            current_lines = []

        for _, text, _ in self.paras:
            # ---- 章节开始 ----
            if self.RE_SECTION_START.search(text):
                flush()
                in_section = True
                continue

            # ---- 其他「第X节」→ 遇到其他题型的章节，停止采集 ----
            if in_section and self.RE_ANY_SECTION.search(text):
                if not self.RE_SECTION_START.search(text):
                    flush()
                    in_section = False
                    continue

            # ---- 章节结束（第二节 / 参考答案） ----
            if in_section and self.RE_SECTION_END.search(text):
                flush()
                in_section = False
                continue

            if not in_section:
                continue

            # ---- 录音稿标记 ----
            m = self.RE_SCRIPT.match(text)
            if m:
                flush()
                collecting = True
                remainder = m.group(1).strip()
                if remainder:
                    current_lines.append(remainder)
                continue

            # ---- 收集录音稿内容 ----
            if collecting:
                current_lines.append(text)

        flush()
        return self._result(items)


# ============================================================================
# 4. 模仿朗读解析器
# ============================================================================

class ImitationReadingParser(BaseParser):
    """
    解析「模仿朗读」文档。
    提取每个单元的「外网」(通常2篇) 和「教材」(通常1篇) 朗读素材。

    文档结构示例:
        7年级上学期模仿朗读试题
        U5：
          一、模仿朗读（共 6 分）
          听以下短文一遍...
          外网：
          English text 1
          外网：
          English text 2
          教材：
          English text 3
          请听录音。...
        U6：
          ...

    支持同一文件内包含多个单元。
    """

    DOC_TYPE = "模仿朗读"

    # 单元标记：U5 / U5： / u5 / U 5
    RE_UNIT = re.compile(r'^[Uu]\s*(\d+)\s*[：:]?\s*$')
    # 来源标记：外网： / 教材：
    RE_SOURCE = re.compile(r'^(外网|教材)\s*[：:]\s*$')
    # 结束标记
    RE_END = re.compile(r'请听录音|开始录音|停止录音')

    def parse(self):
        items = []
        current_unit = ""
        current_source = None    # "外网" 或 "教材"
        current_lines = []

        def flush():
            nonlocal current_lines, current_source
            if current_source and current_lines:
                items.append({
                    "category": f"模仿朗读-{current_source}",
                    "unit": current_unit,
                    "source": current_source,
                    "text": sanitize('\n'.join(current_lines)),
                })
            current_lines = []
            current_source = None

        for _, text, _ in self.paras:
            # ---- 单元标记 ----
            m_unit = self.RE_UNIT.match(text)
            if m_unit:
                flush()
                current_unit = f"U{m_unit.group(1)}"
                continue

            # ---- 来源标记 ----
            m_src = self.RE_SOURCE.match(text)
            if m_src:
                flush()
                current_source = m_src.group(1)
                continue

            # ---- 结束标记 ----
            if self.RE_END.search(text):
                flush()
                continue

            # ---- 收集朗读内容 ----
            if current_source:
                # 跳过纯中文说明文字
                if not is_chinese(text):
                    current_lines.append(text)

        flush()
        return self._result(items)


# ============================================================================
# 5. 词汇解析器（预留接口）
# ============================================================================

class VocabularyParser(BaseParser):
    """
    词汇解析器（预留接口，未来接入）。

    预期文档结构:
        [Title]  七年级上册 Unit X 词汇整理
        [Heading 1] 一、基础词汇整理
        [Heading 2] （一）Unit X 词汇例句整理
        （01） word /phonetic/ pos. 中文释义
        例句：English sentence
        翻译：中文翻译
        ...
        [Heading 2] 二、重点词汇与短语搭配整理
        ...

    未来实现时，重写 parse() 方法即可，无需修改其他代码。
    """

    DOC_TYPE = "词汇"

    def parse(self):
        # TODO: 未来实现词汇解析
        # return self._result(items)
        return self._result([])


# ============================================================================
# 文档类型自动识别
# ============================================================================

PARSER_MAP = {
    "信息获取": InfoAcquisitionParser,
    "课文跟读": TextReadingParser,
    "信息转述及询问": InfoRetellingParser,
    "模仿朗读": ImitationReadingParser,
    "词汇": VocabularyParser,
}


def detect_doc_type(filename):
    """根据文件名自动识别文档类型，返回类型名或 None"""
    if '信息获取' in filename:
        return "信息获取"
    if '课文跟读' in filename:
        return "课文跟读"
    if '信息转述' in filename:
        return "信息转述及询问"
    if '模仿朗读' in filename:
        return "模仿朗读"
    if '词汇' in filename:
        return "词汇"
    return None


# ============================================================================
# 内容自动检测（支持单文档含多题型）
# ============================================================================

# 题型 → 内容标记正则列表（任一匹配即认为文档包含该题型）
CONTENT_MARKERS = {
    "信息获取": [
        re.compile(r'第一节\s*听选信息'),
        re.compile(r'听选信息'),
    ],
    "课文跟读": [
        re.compile(r'句子跟读'),
        re.compile(r'段落跟读'),
        re.compile(r'语篇跟读'),
    ],
    "信息转述及询问": [
        re.compile(r'第一节\s*信息转述'),
        re.compile(r'信息转述'),
    ],
    "模仿朗读": [
        re.compile(r'模仿朗读'),
        re.compile(r'外网\s*[：:]'),
        re.compile(r'教材\s*[：:]'),
    ],
    "词汇": [
        re.compile(r'词汇例句'),
        re.compile(r'词汇整理'),
    ],
}


def detect_types_in_content(paras):
    """
    根据文档内容自动识别包含的题型。
    返回检测到的题型名称列表，保持固定顺序。
    """
    full_text = '\n'.join(text for _, text, _ in paras)
    detected = []
    for doc_type in PARSER_MAP:  # 按 PARSER_MAP 的固定顺序
        markers = CONTENT_MARKERS.get(doc_type, [])
        for marker in markers:
            if marker.search(full_text):
                detected.append(doc_type)
                break
    return detected


def parse_document_auto(filepath):
    """
    自动检测文档类型并解析，支持包含多个题型的文档。

    对上传的文档运行所有匹配的解析器，收集非空结果。
    返回 (results_list, summary_str)。
    """
    try:
        paras = load_paragraphs(filepath)
    except Exception as e:
        return [], f"文档加载失败: {e}"

    if not paras:
        return [], "文档内容为空"

    detected_types = detect_types_in_content(paras)

    if not detected_types:
        return [], "未识别到任何题型内容"

    results = []
    errors = []
    for doc_type in detected_types:
        parser_cls = PARSER_MAP.get(doc_type)
        if parser_cls is None:
            continue
        try:
            parser = parser_cls(filepath)
            result = parser.parse()
            if result["item_count"] > 0:
                results.append(result)
        except Exception as e:
            errors.append(f"{doc_type}: {e}")

    total = sum(r["item_count"] for r in results)
    parts = [f"检测到 {len(detected_types)} 种题型"]
    if results:
        parts.append(f"成功提取 {total} 条内容")
    if errors:
        parts.append(f"{len(errors)} 种解析出错")
    summary = "，".join(parts)

    return results, summary


# ============================================================================
# 主函数
# ============================================================================

def main():
    """主函数：遍历 word 文件夹，解析所有 Word 文档"""
    print("=" * 70)
    print("Word 文档解析脚本")
    print(f"输入目录: {WORD_DIR}")
    print(f"输出目录: {OUTPUT_DIR}")
    print("=" * 70)

    word_files = [
        f for f in os.listdir(WORD_DIR)
        if f.endswith('.docx') and not f.startswith('~$')
    ]

    if not word_files:
        print("[警告] word 文件夹中没有找到 .docx 文件")
        return

    all_results = []

    for fname in sorted(word_files):
        filepath = os.path.join(WORD_DIR, fname)
        doc_type = detect_doc_type(fname)

        if doc_type is None:
            print(f"\n[跳过] 未识别类型的文件: {fname}")
            continue

        parser_cls = PARSER_MAP[doc_type]
        try:
            parser = parser_cls(filepath)
            result = parser.parse()
        except Exception as e:
            print(f"\n[错误] 解析失败: {fname} — {e}")
            continue

        all_results.append(result)

        # 打印摘要
        print(f"\n[{doc_type}] {fname}")
        print(f"  共提取 {result['item_count']} 条内容:")

        for item in result["items"]:
            preview = item["text"][:80].replace('\n', ' ')
            cat = item["category"]

            if "number" in item:
                print(f"  · [{cat}] #{item['number']:>2}  {preview}...")
            elif "sentence_number" in item and "discourse_number" in item:
                print(f"  · [{cat}] 语篇{item['discourse_number']}-{item['sentence_number']}  {preview}...")
            elif "sentence_number" in item:
                print(f"  · [{cat}] 句{item['sentence_number']}  {preview}...")
            elif "index" in item:
                print(f"  · [{cat}] #{item['index']:>2}  {preview}...")
            elif "unit" in item:
                print(f"  · [{cat}] ({item['unit']:<3}) {preview}...")
            elif "discourse_number" in item:
                print(f"  · [{cat}] 语篇{item['discourse_number']}  {preview}...")
            else:
                print(f"  · [{cat}] {preview}...")

    # 保存汇总 JSON
    output_path = os.path.join(OUTPUT_DIR, "parsed_results.json")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 70)
    total_items = sum(r["item_count"] for r in all_results)
    print(f"解析完成！共处理 {len(all_results)} 个文件，提取 {total_items} 条内容")
    print(f"结果已保存到: {output_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()

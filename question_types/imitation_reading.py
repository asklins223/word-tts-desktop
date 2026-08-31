"""「模仿朗读」题型切片：解析器与题型元数据。"""

import re

from audio_naming import audio_filename_stem, is_exam_paper_bundle
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.oxml.ns import qn

from question_types.base import BaseParser
from question_types.text_utils import is_chinese, sanitize


# ============================================================================
# 6. 模仿朗读解析器
# ============================================================================

class ImitationReadingParser(BaseParser):
    """
    解析「模仿朗读」文档。
    同时支持旧版每个单元的「外网」/「教材」素材，以及新版题目边框内的英文短文。

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

    新版文档结构示例:
        16.（计算机语音和屏幕文字提示）...模仿朗读...
        [带边框的英文表格]
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
    # 新版试卷把题号提示放在普通段落，把真正朗读的英文放在带边框的表格中。
    # 只把含“模仿朗读”的编号段落作为新规则的判定依据，避免普通英文表格
    # 或旧版“外网/教材”文档被误切换到新规则。
    RE_BOXED_QUESTION = re.compile(
        r'^\s*(?P<number>\d+)\s*[.．、）)]\s*.*?模仿朗读',
        re.IGNORECASE,
    )
    RE_CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff]+')

    def __init__(self, filepath, **kwargs):
        super().__init__(filepath, **kwargs)
        self._boxed_table_texts_cache = None

    def parse(self):
        """按文档结构选择新旧规则；旧规则的输出保持不变。"""
        if self._is_boxed_english_format():
            boxed_result = self._parse_boxed_english_format()
            legacy_result = self._parse_legacy_format()
            # 同一份资料偶尔会把旧版单元素材和新版框选短文放在一起；
            # 两套规则都命中时合并结果，不能因为新版存在就丢掉旧素材。
            if legacy_result["items"]:
                return self._result(legacy_result["items"] + boxed_result["items"])
            return boxed_result
        return self._parse_legacy_format()

    def _boxed_question_numbers(self):
        numbers = []
        for _, text, _ in self.paras:
            match = self.RE_BOXED_QUESTION.match(text)
            if match:
                numbers.append(int(match.group('number')))
        return numbers

    @classmethod
    def _english_only(cls, text):
        """从表格单元格中去掉中文说明，只保留带英文的内容。"""
        value = sanitize(str(text or ''))
        if not re.search(r'[A-Za-z]', value):
            return ''
        value = sanitize(cls.RE_CJK.sub(' ', value)).strip(' ：:')
        return value if re.search(r'[A-Za-z]', value) else ''

    @classmethod
    def _table_text(cls, table):
        """按表格阅读顺序合并一个框，避免一个朗读框被拆成多个条目。"""
        cell_texts = []
        seen_cells = set()
        for row in table.rows:
            for cell in row.cells:
                # 合并单元格会在 python-docx 中多次映射到同一个 XML 节点。
                # 去重后才能保证一篇文章只产生一个音频条目。
                cell_key = cell._tc.getroottree().getpath(cell._tc)
                if cell_key in seen_cells:
                    continue
                seen_cells.add(cell_key)
                if cell.text and cell.text.strip():
                    cell_texts.append(cell.text)
        return cls._english_only('\n'.join(cell_texts))

    def _boxed_table_texts(self):
        if self._boxed_table_texts_cache is not None:
            return self._boxed_table_texts_cache

        texts = []
        try:
            document = Document(self.filepath)
            # 表格本身没有“属于哪道题”的字段。按正文顺序绑定：只有紧跟在
            # “模仿朗读”题目提示之后的第一个英文表格才属于该题，后面的
            # 听后记录表、答题表不能再被误识别成朗读稿。
            waiting_for_table = False
            for child in document.element.body.iterchildren():
                if child.tag == qn("w:p"):
                    paragraph = Paragraph(child, document)
                    if self.RE_BOXED_QUESTION.match(paragraph.text.strip()):
                        waiting_for_table = True
                    continue
                if child.tag != qn("w:tbl") or not waiting_for_table:
                    continue
                table = Table(child, document)
                text = self._table_text(table)
                if text:
                    texts.append(text)
                    waiting_for_table = False
        except Exception:
            # 普通旧格式不依赖表格；新格式识别失败时回退旧规则，
            # 不因为可选的表格读取影响已有文档解析。
            texts = []
        self._boxed_table_texts_cache = texts
        return texts

    def _is_boxed_english_format(self):
        return bool(self._boxed_question_numbers() and self._boxed_table_texts())

    def _parse_boxed_english_format(self):
        """新规则：每个英文框是一篇模仿朗读稿，默认使用女声。"""
        question_numbers = self._boxed_question_numbers()
        texts = self._boxed_table_texts()
        items = []
        use_exam_naming = is_exam_paper_bundle(self.paras)
        for index, text in enumerate(texts):
            number = question_numbers[index] if index < len(question_numbers) else index + 1
            item = {
                "category": "模仿朗读-框内英文",
                "number": number,
                "source": "框内英文",
                "voice": "female",
                "text": text,
            }
            if use_exam_naming:
                filename_stem = audio_filename_stem(["模仿朗读"], index + 1)
                item.update({
                    "question_numbers": [number],
                    "type_path": ["模仿朗读"],
                    "filename_stem": filename_stem,
                    "audio_filename_stem": filename_stem,
                })
            items.append(item)
        return self._result(items)

    def _parse_legacy_format(self):
        """旧版 U 单元 + 外网/教材规则，保留原有行为。"""
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

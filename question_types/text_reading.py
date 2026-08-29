"""「课文跟读」题型切片：解析器与题型元数据。"""

import re

from question_types.base import BaseParser, QuestionType
from question_types.text_utils import is_chinese, sanitize, split_sentences


# ============================================================================
# 4. 课文跟读解析器
# ============================================================================

class TextReadingParser(BaseParser):
    """
    解析「课文跟读」文档。
    提取三类内容：
      - 句子跟读：去掉序号前缀，按序号排序
      - 段落跟读：整段英文
      - 语篇跟读：按「语篇N」分组，每组可含多段

    Section A/B 新格式优先：显式 Conversation 对话按角色边界拆分，
    普通文章按标题和句子边界生成音频；旧版章节格式继续使用历史规则。

    音色/命名规则（仅 Understanding Idea / Reading for writing 章节）：
      讯飞声音参数由前端动态配置，解析器不固定。
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

    # ------------------------------------------------------------------
    # Section A/B 新版课文跟读格式
    # ------------------------------------------------------------------
    # 新版样本中 Section A/B 和子题型有时只是 Normal 样式，不能依赖
    # Heading 层级判断；而且「段落/语篇跟读」的音频边界由 Conversation
    # 或 Word 段落决定，不能套用旧版的逐句切分规则。
    RE_SECTION_AB = re.compile(r'^Section\s+([A-Za-z])\s*[：:]?\s*$', re.I)
    RE_CONVERSATION = re.compile(r'^Conversation\s*(\d+)\s*[：:]?\s*$', re.I)
    RE_NEW_SUB_SECTION = re.compile(r'^(句子跟读|段落跟读|语篇跟读)\s*[：:]?\s*$')
    RE_ROLE_LABEL = re.compile(r'^([^:：\n]{1,60}?)\s*[:：]\s*(.*)$')
    RE_ARTICLE_SUBTITLE = re.compile(r'^//\s*(.*)$')
    RE_CONTENT_HEADING = re.compile(r'^(Reading\s+Plus)\s*[：:]?\s*$', re.I)

    def __init__(self, filepath):
        super().__init__(filepath)
        self._section_ab_profile_cache = None

    def parse(self):
        """按格式优先级解析课文跟读。

        新版不是由某一个标题字符串单独决定的：只有 Section 标记、跟读
        子题型和有效内容形成连续结构时，才切换到新版解析；否则继续走
        原有 Understanding Idea / Reading for writing 逻辑，避免旧文档中
        偶然出现的 ``Section A`` 被误判。
        """
        if self._is_section_ab_format():
            return self._parse_section_ab_format()
        return self._parse_legacy_format()

    def _is_section_ab_format(self):
        """判断是否为新版 Section A/B 课文跟读文档。

        Section A/B 只是候选信号。真正的判定要求至少有一个 Section
        标记，后面紧跟一个跟读子题型，并且该子题型下存在可朗读内容。
        这样旧文档正文、目录或备注中的孤立 ``Section A`` 不会触发新版
        状态机；如果新旧结构都完整存在，则按产品要求让新版优先。
        """
        return self._detect_section_ab_profile()["is_new"]

    @classmethod
    def _normalized_heading(cls, text):
        """统一标题末尾中英文冒号和空白，供结构检测使用。"""
        value = str(text or '').strip()
        return re.sub(r'[：:]\s*$', '', value).strip()

    @classmethod
    def _new_subsection_name(cls, text):
        """返回新版跟读子题型名；普通正文返回 None。"""
        value = cls._normalized_heading(text)
        return value if value in cls._SUB_SECTION_NAMES else None

    @classmethod
    def _is_reading_plus_heading(cls, text):
        """只做内容层面的候选匹配，是否为结构标题由 profile 决定。"""
        return bool(cls.RE_CONTENT_HEADING.match(str(text or '').strip()))

    def _detect_section_ab_profile(self):
        """从段落序列提取新版格式证据，不依赖文件名或年级名称。

        ``Section A`` 只有在同一 Section 范围内连接到跟读子题型，且子
        题型下有英文/角色等有效载荷时才算成立。这里还同时提取
        Reading Plus 的结构位置和对话的角色拆分模式，解析阶段复用这份
        profile，避免不同分支各自用启发式重新猜测。
        """
        if self._section_ab_profile_cache is not None:
            return self._section_ab_profile_cache

        entries = list(self.paras)
        section_positions = []
        legacy_section_positions = []
        subsection_positions = []
        reading_plus_candidates = []
        conversation_positions = []
        role_line_positions = []
        article_marker_positions = []

        for position, (_, text, _) in enumerate(entries):
            value = str(text or '').strip()
            if self.RE_SECTION_AB.match(value):
                section_positions.append(position)
            if self._normalized_heading(value).casefold() in self.KNOWN_SECTIONS:
                legacy_section_positions.append(position)
            subsection = self._new_subsection_name(value)
            if subsection:
                subsection_positions.append((position, subsection))
            if self._is_reading_plus_heading(value):
                reading_plus_candidates.append(position)
            if self.RE_CONVERSATION.match(value):
                conversation_positions.append(position)
            if self.RE_ARTICLE_SUBTITLE.match(value):
                article_marker_positions.append(position)
            if any(self._role_label(line) for line in value.splitlines()):
                role_line_positions.append(position)

        def next_section_after(position):
            return next(
                (
                    candidate
                    for candidate in sorted(section_positions + legacy_section_positions)
                    if candidate > position
                ),
                len(entries),
            )

        # Section 与子题型必须在同一个 Section 范围内，避免文档前言中的
        # Section A 和后面完全无关的跟读标题被拼成新版结构。
        linked_subsections = []
        for section_position in section_positions:
            section_end = next_section_after(section_position)
            linked_subsections.extend(
                (position, name)
                for position, name in subsection_positions
                if section_position < position < section_end
            )

        # 仅把跟读子题型之后、下一个结构标题之前的英文/角色内容算作
        # 有效载荷。标题、Conversation 编号、语篇编号本身不算内容。
        payload_subsections = []
        all_boundary_positions = sorted(
            section_positions
            + legacy_section_positions
            + [position for position, _ in subsection_positions]
            + reading_plus_candidates
        )
        for subsection_position, subsection_name in linked_subsections:
            next_boundary = next(
                (candidate for candidate in all_boundary_positions
                 if candidate > subsection_position),
                len(entries),
            )
            has_payload = False
            for position in range(subsection_position + 1, next_boundary):
                value = str(entries[position][1] or '').strip()
                if not value:
                    continue
                if self.RE_CONVERSATION.match(value):
                    continue
                if self.RE_DISCOURSE_NUM.match(value):
                    continue
                if self._is_reading_plus_heading(value):
                    continue
                if self._new_english_lines(value):
                    has_payload = True
                    break
            if has_payload:
                payload_subsections.append((subsection_position, subsection_name))

        # Reading Plus 只有在短距离内确实引出新的跟读子题型时才视为章节
        # 边界；普通正文中提到这个词不会改变前面的 Section 和命名空间。
        reading_plus_positions = set()
        for candidate in reading_plus_candidates:
            candidate_end = next_section_after(candidate)
            if any(
                candidate < subsection_position <= candidate + 8
                and subsection_position < candidate_end
                for subsection_position, _ in subsection_positions
            ):
                reading_plus_positions.add(candidate)

        # 对话模式同样从结构判断：显式 Conversation 块中出现至少两个
        # 有效角色时，说明文档给出了“按角色分块”的边界。没有显式
        # Conversation 的新格式对话，则在解析阶段按 Word 段落保留边界，
        # 由 flush_dialogue_buffer 进一步拆成逐行音频；普通非对话段落不受影响。
        conversation_blocks = []
        active_block = None
        current_subsection = None
        in_new_section = False

        def flush_conversation_block():
            nonlocal active_block
            if active_block is not None:
                conversation_blocks.append(active_block)
                active_block = None

        for position, (_, text, _) in enumerate(entries):
            value = str(text or '').strip()
            section_match = self.RE_SECTION_AB.match(value)
            if section_match:
                flush_conversation_block()
                current_subsection = None
                in_new_section = True
                continue
            if self._normalized_heading(value).casefold() in self.KNOWN_SECTIONS:
                flush_conversation_block()
                current_subsection = None
                in_new_section = False
                continue
            if not in_new_section:
                continue
            subsection = self._new_subsection_name(value)
            if subsection:
                flush_conversation_block()
                current_subsection = subsection
                continue
            if self.RE_CONVERSATION.match(value):
                flush_conversation_block()
                if current_subsection in (self.SUB_PARAGRAPH, self.SUB_DISCOURSE):
                    active_block = {
                        "position": position,
                        "roles": set(),
                        "role_lines": 0,
                    }
                continue
            if active_block is not None:
                for line in self._new_english_lines(value):
                    role = self._role_label(line)
                    if role:
                        active_block["roles"].add(
                            re.sub(r'\s+', ' ', role).casefold()
                        )
                        active_block["role_lines"] += 1
        flush_conversation_block()

        role_dialogue_blocks = [
            block for block in conversation_blocks
            if len(block["roles"]) >= 2 and block["role_lines"] >= 2
        ]
        role_audio_mode = "per_role" if role_dialogue_blocks else "aggregate"

        # 新版置信条件：Section 标记、同范围跟读子题型、子题型载荷三者
        # 缺一不可。没有使用单个字符串、文件名、标题或年级名做决策。
        is_new = bool(
            section_positions
            and linked_subsections
            and payload_subsections
        )
        self._section_ab_profile_cache = {
            "is_new": is_new,
            "section_positions": tuple(section_positions),
            "legacy_section_positions": tuple(legacy_section_positions),
            "subsection_positions": tuple(subsection_positions),
            "reading_plus_positions": frozenset(reading_plus_positions),
            "conversation_positions": tuple(conversation_positions),
            "role_line_positions": tuple(role_line_positions),
            "article_marker_positions": tuple(article_marker_positions),
            "role_audio_mode": role_audio_mode,
            "conversation_blocks": tuple(conversation_blocks),
        }
        return self._section_ab_profile_cache

    @staticmethod
    def _role_label_is_valid(label):
        """判断冒号前文本是否更像角色名，而不是普通正文。"""
        value = str(label or '').strip()
        if not value or len(value) > 48:
            return False
        if len(re.split(r'\s+', value)) > 4:
            return False
        if value[0].isdigit() or '://' in value or '/' in value or '\\' in value:
            return False
        if re.search(r'[.!?。！？；;，,]', value):
            return False
        return True

    @classmethod
    def _role_label(cls, text):
        """返回一行中的角色名；普通带冒号文本返回 None。"""
        match = cls.RE_ROLE_LABEL.match(str(text or '').strip())
        if not match or not cls._role_label_is_valid(match.group(1)):
            return None
        return match.group(1).strip()

    @classmethod
    def _contains_multiple_roles(cls, texts):
        """新版对话至少出现两个不同角色时，按对话整体保留换行。"""
        labels = set()
        for text in texts:
            for line in str(text or '').splitlines():
                label = cls._role_label(line)
                if label:
                    labels.add(re.sub(r'\s+', ' ', label).casefold())
        return len(labels) >= 2

    @staticmethod
    def _new_english_lines(text):
        """从新版内容中去掉中文翻译行，保留英文/角色行。"""
        lines = []
        for line in str(text or '').splitlines():
            line = line.strip()
            if not line:
                continue
            if TextReadingParser.RE_CHINESE_PREFIX.match(line) or is_chinese(line):
                continue
            lines.append(line)
        return lines

    @classmethod
    def _new_clean_text(cls, texts):
        """清理新版音频文本，同时保留角色行之间的换行。"""
        lines = []
        for text in texts:
            lines.extend(cls._new_english_lines(text))
        return sanitize('\n'.join(lines))

    @classmethod
    def _new_article_units(cls, text):
        """按新版文章中的 ``//`` 小标题标记拆分同一 Word 段落。"""
        units = []
        current_lines = []
        for line in cls._new_english_lines(text):
            if cls.RE_ARTICLE_SUBTITLE.match(line):
                if current_lines:
                    units.append('\n'.join(current_lines))
                    current_lines = []
                # 双斜杠本身是结构标记，不与下一行正文混为一个原始段落；
                # 后续 append_article_items 会把它作为独立标题音频输出。
                units.append(line)
                continue
            current_lines.append(line)
        if current_lines:
            units.append('\n'.join(current_lines))
        return units

    @classmethod
    def _article_heading(cls, text):
        """返回文章标题/小标题文本及是否显式使用 // 标记。"""
        value = sanitize(text)
        if not value:
            return '', False
        match = cls.RE_ARTICLE_SUBTITLE.match(value)
        if match:
            return match.group(1).strip(), True
        return value, False

    @classmethod
    def _looks_like_article_heading(
        cls,
        text,
        *,
        formatting_hint=False,
        is_first_unit=False,
    ):
        """识别新规则中的文章大标题/小标题。

        新样本的标题通常使用短文本、粗体或字号区分。格式提示由
        ``load_paragraphs`` 提供；没有格式提示时，仅允许文章组的第一段
        使用“短文本且没有句末标点”的保守兜底，避免把普通短正文误当标题。
        显式 ``//`` 始终优先。
        """
        value, explicit = cls._article_heading(text)
        if not value:
            return False
        if explicit:
            return True
        if formatting_hint:
            return True
        if not is_first_unit:
            return False
        if cls._role_label(value):
            return False
        if len(value) > 48 or len(re.split(r'\s+', value)) > 8:
            return False
        if re.match(r'^\d+\s*[.、）)]', value):
            return False
        if re.search(r'[.!?。！？]$', value):
            return False
        return True

    @staticmethod
    def _section_ab_code(section):
        match = re.match(r'^Section\s+([A-Za-z])$', str(section or '').strip(), re.I)
        return f"S{match.group(1).upper()}" if match else "S"

    @classmethod
    def _role_segments(cls, text):
        """按角色行拆分一条对话，返回 ``[(角色名, 文本), ...]``。"""
        segments = []
        current_role = None
        current_lines = []

        def flush():
            nonlocal current_lines
            clean = sanitize('\n'.join(current_lines))
            if clean:
                segments.append((current_role, clean))
            current_lines = []

        for line in str(text or '').splitlines():
            value = line.strip()
            if not value:
                continue
            role = cls._role_label(value)
            if role:
                flush()
                current_role = role
            current_lines.append(value)
        flush()
        return segments or [(None, sanitize(text))]

    def _parse_section_ab_format(self):
        """解析讯飞新版 Section A/B 课文跟读格式。

        新版规则：
          - 句子跟读按编号输出，默认女声；
          - 没有显式 Conversation 的多角色段落按 Word 段落/角色行输出，
            角色名通过结构元数据提供给用户配置；显式 Conversation 块中
            每个角色单独输出，拆分模式由文档结构 profile 决定；
          - 语篇跟读的对话遵循同样的角色拆分规则；文章的大标题/小标题
            单独输出，正文按英文句子拆分为音频。
        """
        format_profile = self._detect_section_ab_profile()
        split_role_audio = format_profile["role_audio_mode"] == "per_role"
        reading_plus_positions = format_profile["reading_plus_positions"]
        items = []
        current_section = ''
        current_sub = None
        current_audio_prefix = 'S'
        in_new_section = False

        sentence_buf = []
        paragraph_units = []
        paragraph_blocks = []
        paragraph_conversation_mode = False
        paragraph_current_number = None
        paragraph_current_lines = []

        # 非 Conversation 语篇单元保存为 ``(文本, 是否带标题格式提示)``；
        # 这样 Normal 样式但粗体/大字号的小标题也能被正确识别。
        discourse_units = []
        discourse_blocks = []
        discourse_conversation_mode = False
        discourse_current_number = None
        discourse_current_lines = []
        new_sequence_by_category = {}

        def reset_paragraph_state():
            nonlocal paragraph_units, paragraph_blocks
            nonlocal paragraph_conversation_mode, paragraph_current_number
            nonlocal paragraph_current_lines
            paragraph_units = []
            paragraph_blocks = []
            paragraph_conversation_mode = False
            paragraph_current_number = None
            paragraph_current_lines = []

        def reset_discourse_state():
            nonlocal discourse_units, discourse_blocks
            nonlocal discourse_conversation_mode, discourse_current_number
            nonlocal discourse_current_lines
            discourse_units = []
            discourse_blocks = []
            discourse_conversation_mode = False
            discourse_current_number = None
            discourse_current_lines = []

        def reset_section_sequences():
            new_sequence_by_category.clear()

        def flush_sentences():
            nonlocal sentence_buf
            if not sentence_buf:
                return
            for number, text in sorted(sentence_buf, key=lambda value: value[0]):
                items.append({
                    "category": self.SUB_SENTENCE,
                    "section": current_section,
                    "number": number,
                    "filename_stem": f"{current_audio_prefix}句子{number}",
                    "voice": "female",
                    "text": text,
                })
            sentence_buf = []

        def append_new_block_item(category, text, conversation_number=None, role=None):
            clean = sanitize(text)
            if not clean:
                return
            # 语篇跟读含多个 Conversation 时，要求一个 Conversation 一个语篇，
            # 命名 SB语篇-C1-1，且每个 Conversation 内题目序号重置
            if category == self.SUB_DISCOURSE and conversation_number is not None:
                sequence_key = f"{current_audio_prefix}:{category}:C{conversation_number}"
                new_sequence_by_category[sequence_key] = new_sequence_by_category.get(sequence_key, 0) + 1
                sequence = new_sequence_by_category[sequence_key]
                filename_stem = f"{current_audio_prefix}语篇-C{conversation_number}-{sequence}"
            elif (
                category in (self.SUB_PARAGRAPH, self.SUB_DISCOURSE)
                and conversation_number is not None
            ):
                # 段落跟读仍保留原有 SA-段-Cx-y 规则（全局序号）
                sequence_key = f"{current_audio_prefix}:{category}"
                new_sequence_by_category[sequence_key] = new_sequence_by_category.get(sequence_key, 0) + 1
                sequence = new_sequence_by_category[sequence_key]
                mode_prefix = "段" if category == self.SUB_PARAGRAPH else "语"
                filename_stem = (
                    f"{current_audio_prefix}-{mode_prefix}-C"
                    f"{conversation_number}-{sequence}"
                )
            else:
                sequence_key = f"{current_audio_prefix}:{category}"
                new_sequence_by_category[sequence_key] = new_sequence_by_category.get(sequence_key, 0) + 1
                sequence = new_sequence_by_category[sequence_key]
                filename_stem = f"{current_audio_prefix}{category[:2]}{sequence}"
            item = {
                "category": category,
                "section": current_section,
                "number": sequence,
                "filename_stem": filename_stem,
                "text": clean,
            }
            # 对话可能有多个角色，不能给整条结果写死男女声；未知角色
            # 在合成阶段按默认女声处理，已选择的角色由 role_voices 覆盖。
            if role:
                item["role"] = role
            elif not self._contains_multiple_roles(clean.splitlines()):
                item["voice"] = "female"
            if conversation_number is not None:
                item["conversation_number"] = conversation_number
            items.append(item)

        def flush_dialogue_buffer(category, units, blocks, conversation_mode):
            """输出新版段落/语篇对话。"""
            if conversation_mode:
                # 文档偶尔会在第一个 Conversation 前放一段引导正文。进入
                # conversation 模式时不能把已经收集的 units 丢掉；语篇正文
                # 仍按文章规则处理，段落正文则沿用“一段一个音频”。
                if units:
                    if category == self.SUB_DISCOURSE:
                        append_article_items(units)
                    else:
                        for unit in units:
                            unit_text = (
                                unit[0]
                                if isinstance(unit, (tuple, list)) and len(unit) == 2
                                else unit
                            )
                            append_new_block_item(
                                category,
                                self._new_clean_text([unit_text]),
                            )
                groups = [
                    (conversation_number, text, None)
                    for conversation_number, text in blocks
                ]
            else:
                # 新版“段落跟读”的对话通常没有 Conversation 标记，而是
                # 每个 Word 段落一行角色台词。检测到至少两个角色后，必须
                # 保留这些段落边界，否则整个对话会被错误合成一条音频。
                # 非对话文本仍沿用原来的“一段 Word 段落一条音频”规则。
                groups = []
                is_dialogue = self._contains_multiple_roles(units)
                for unit in units:
                    role_segments = self._role_segments(unit)
                    if is_dialogue and len(role_segments) > 1:
                        groups.extend(
                            (None, role_text, role)
                            for role, role_text in role_segments
                        )
                        continue
                    role_hint = (
                        role_segments[0][0]
                        if is_dialogue and len(role_segments) == 1
                        else None
                    )
                    groups.append((
                        None,
                        self._new_clean_text([unit]),
                        role_hint,
                    ))
            for conversation_number, text, role_hint in groups:
                if isinstance(text, list):
                    text = self._new_clean_text(text)
                if (
                    split_role_audio
                    and conversation_mode
                    and self._contains_multiple_roles([text])
                ):
                    for role, role_text in self._role_segments(text):
                        append_new_block_item(
                            category,
                            role_text,
                            conversation_number,
                            role=role,
                        )
                else:
                    append_new_block_item(
                        category,
                        text,
                        conversation_number,
                        role=role_hint,
                    )

        def flush_paragraph():
            nonlocal paragraph_current_lines, paragraph_current_number
            if paragraph_conversation_mode and paragraph_current_lines:
                paragraph_blocks.append((
                    paragraph_current_number,
                    self._new_clean_text(paragraph_current_lines),
                ))
                paragraph_current_lines = []
                paragraph_current_number = None
            if paragraph_conversation_mode:
                flush_dialogue_buffer(
                    self.SUB_PARAGRAPH,
                    paragraph_units,
                    paragraph_blocks,
                    True,
                )
            elif paragraph_units:
                flush_dialogue_buffer(
                    self.SUB_PARAGRAPH,
                    paragraph_units,
                    [],
                    False,
                )
            reset_paragraph_state()

        def append_article_items(units):
            """按文章标题/小标题规则输出语篇音频（新版语篇文章）。

            录制要求：
            - 对话形式（含角色名如 Teng Fei:）按角色一个音频（已在外层通过
              conversation_mode 处理，此处仅处理无显式 Conversation 的对话）
            - 标题（大标题/小标题）单独一段，无标点结尾，各自一个音频
            - 正文按句拆分，一句一个音频，默认女声
            - 单独一段话也按句拆分
            """
            article_units = []
            for unit in units:
                if isinstance(unit, (tuple, list)) and len(unit) == 2:
                    article_units.append((str(unit[0]), bool(unit[1])))
                else:
                    article_units.append((str(unit), False))
            article_texts = [unit for unit, _formatting_hint in article_units]

            # 无显式 Conversation 但包含多角色的对话：按角色行一个音频
            if self._contains_multiple_roles(article_texts):
                for unit, _formatting_hint in article_units:
                    cleaned_unit = self._new_clean_text([unit])
                    if not cleaned_unit:
                        continue
                    # 若一行内含多角色（极少），按角色拆分
                    role_segments = self._role_segments(cleaned_unit)
                    if len(role_segments) > 1:
                        for role, role_text in role_segments:
                            append_new_block_item(self.SUB_DISCOURSE, role_text, role=role)
                    else:
                        role, _ = role_segments[0] if role_segments else (None, cleaned_unit)
                        append_new_block_item(self.SUB_DISCOURSE, cleaned_unit, role=role)
                return
            for unit_index, (unit, formatting_hint) in enumerate(article_units):
                cleaned_unit = self._new_clean_text([unit])
                if not cleaned_unit:
                    continue
                heading_text, is_explicit = self._article_heading(cleaned_unit)
                # 显式 //、Word 格式提示或符合首段无标点短标题规则的段落视为标题
                is_heading = self._looks_like_article_heading(
                    cleaned_unit,
                    formatting_hint=formatting_hint,
                    is_first_unit=unit_index == 0,
                )
                if is_heading:
                    # 标题单独一个音频，去掉 // 前缀后的标题文本
                    text_to_append = heading_text if is_explicit else cleaned_unit
                    if text_to_append:
                        append_new_block_item(self.SUB_DISCOURSE, text_to_append)
                    continue
                # 正文：按句拆分，一句一个音频
                sentences = split_sentences(cleaned_unit)
                if not sentences:
                    sentences = [cleaned_unit]
                for sent in sentences:
                    append_new_block_item(self.SUB_DISCOURSE, sent)

        def flush_discourse():
            nonlocal discourse_current_lines, discourse_current_number
            if discourse_conversation_mode and discourse_current_lines:
                discourse_blocks.append((
                    discourse_current_number,
                    self._new_clean_text(discourse_current_lines),
                ))
                discourse_current_lines = []
                discourse_current_number = None
            if discourse_conversation_mode:
                flush_dialogue_buffer(
                    self.SUB_DISCOURSE,
                    discourse_units,
                    discourse_blocks,
                    True,
                )
            elif discourse_units:
                # 兼容旧式「语篇1」标记：标记本身不朗读，组内仍按新版
                # “一段一个音频/标题规则”处理。
                groups = []
                current = []
                for unit in discourse_units:
                    unit_text = unit[0] if isinstance(unit, (tuple, list)) else unit
                    if self.RE_DISCOURSE_NUM.match(unit_text):
                        if current:
                            groups.append(current)
                            current = []
                        continue
                    current.append(unit)
                if current:
                    groups.append(current)
                if not groups:
                    groups = [discourse_units]
                for group in groups:
                    append_article_items(group)
            reset_discourse_state()

        def flush_all_new():
            flush_sentences()
            flush_paragraph()
            flush_discourse()

        def append_new_content(target, text, *, heading_hint=False):
            lines = self._new_english_lines(text)
            if not lines:
                return
            if target == self.SUB_PARAGRAPH:
                if paragraph_conversation_mode:
                    paragraph_current_lines.extend(lines)
                else:
                    paragraph_units.append('\n'.join(lines))
            elif target == self.SUB_DISCOURSE:
                if discourse_conversation_mode:
                    discourse_current_lines.extend(lines)
                else:
                    article_units = self._new_article_units(text)
                    discourse_units.extend(
                        (unit, bool(heading_hint and unit_index == 0))
                        for unit_index, unit in enumerate(article_units)
                    )

        for position, (_, text, _) in enumerate(self.paras):
            paragraph_metadata = (
                self.paragraph_metadata[position]
                if position < len(self.paragraph_metadata)
                else {}
            )
            heading_hint = bool(paragraph_metadata.get("heading_hint"))
            section_match = self.RE_SECTION_AB.match(text)
            if section_match:
                flush_all_new()
                current_section = f"Section {section_match.group(1).upper()}"
                current_audio_prefix = self._section_ab_code(current_section)
                reset_section_sequences()
                current_sub = None
                in_new_section = True
                continue

            # 混合文档中如果在新版 Section 之间出现完整的旧章节，先把
            # 它从新版状态机隔离出来，避免旧章节的「句子跟读」被错误
            # 命名成 S句子N。新版分支只处理已经确认属于新版的 Section
            # 范围；完整旧格式文档仍由 legacy 入口处理。
            if self._normalized_heading(text).casefold() in self.KNOWN_SECTIONS:
                flush_all_new()
                current_section = ''
                current_audio_prefix = 'S'
                reset_section_sequences()
                current_sub = None
                in_new_section = False
                continue

            if not in_new_section:
                continue

            # 样本中的 Reading Plus 是语篇跟读下一个内容组的标题，
            # 不属于上一篇文章的音频文本；下一个「语篇跟读」会再次建立
            # 音频边界。样式可能是 Normal，因此用内容标记兜底。
            if position in reading_plus_positions:
                flush_all_new()
                current_section = "Reading Plus"
                current_audio_prefix = "RP"
                reset_section_sequences()
                current_sub = None
                continue

            sub_match = self.RE_NEW_SUB_SECTION.match(text)
            if sub_match:
                flush_all_new()
                current_sub = sub_match.group(1)
                continue

            if current_sub == self.SUB_SENTENCE:
                self._handle_sentence(text, sentence_buf)
                continue

            if current_sub == self.SUB_PARAGRAPH:
                conversation_match = self.RE_CONVERSATION.match(text)
                if conversation_match:
                    if paragraph_conversation_mode and paragraph_current_lines:
                        paragraph_blocks.append((
                            paragraph_current_number,
                            self._new_clean_text(paragraph_current_lines),
                        ))
                    paragraph_conversation_mode = True
                    paragraph_current_number = int(conversation_match.group(1))
                    paragraph_current_lines = []
                    continue
                append_new_content(self.SUB_PARAGRAPH, text)
                continue

            if current_sub == self.SUB_DISCOURSE:
                conversation_match = self.RE_CONVERSATION.match(text)
                if conversation_match:
                    if discourse_conversation_mode and discourse_current_lines:
                        discourse_blocks.append((
                            discourse_current_number,
                            self._new_clean_text(discourse_current_lines),
                        ))
                    discourse_conversation_mode = True
                    discourse_current_number = int(conversation_match.group(1))
                    discourse_current_lines = []
                    continue
                append_new_content(
                    self.SUB_DISCOURSE,
                    text,
                    heading_hint=heading_hint,
                )

        flush_all_new()
        return self._result(items)

    def _parse_legacy_format(self):
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
        """处理句子跟读的一个段落

        支持两种格式：
        - 带编号：1. English sentence / 1、English sentence
        - 无编号：English sentence（如 Understanding Idea 中的格式，
          每个英文句子后跟一行中文翻译）
        """
        # 跳过中文翻译行
        if self.RE_CHINESE_PREFIX.match(text):
            return
        # 取第一行（段落内可能内嵌中文翻译换行）
        first_line = text.split('\n')[0].strip()
        m = self.RE_NUMBERED.match(first_line)
        if m:
            num = int(m.group(1))
            eng = m.group(2).strip()
            # 跳过纯中文行
            if eng and not is_chinese(eng):
                buf.append((num, eng))
        else:
            # 无编号格式：英文句子后跟中文翻译行
            # 跳过纯中文行，保留英文句子
            if not is_chinese(text):
                eng = sanitize(text)
                if eng:
                    # 自动编号：基于当前缓冲区大小
                    num = len(buf) + 1
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


QUESTION_TYPE = QuestionType(
    key="课文跟读",
    parser=TextReadingParser,
    color="#15803d",
    filename_keywords=("课文跟读",),
    content_markers=(
        re.compile(r'句子跟读'),
        re.compile(r'段落跟读'),
        re.compile(r'语篇跟读'),
    ),
)

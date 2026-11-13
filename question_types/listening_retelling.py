"""Parse standalone listening-retelling questions and their visual guide."""

import re

from audio_naming import audio_filename_stem
from document_profiles import LISTENING_RETELLING_PROFILE, LISTENING_RETELLING_ENTRY_PROFILE
from question_types.base import BaseParser
from question_types.text_utils import (
    is_chinese,
    is_major_section_heading,
    listening_play_count,
    match_answer_marker,
    match_script_marker,
    sanitize,
)


class ListeningRetellingParser(BaseParser):
    DOC_TYPE = "听后转述"
    _REQUIRES_DOCUMENT_BLOCKS = True
    _REPEAT_MAJOR_SECTIONS = True
    RE_HEADING = re.compile(
        r"^\s*(?:[一二三四五六七八九十]+\s*[、.．)]\s*)?"
        r"听后转述(?:[（(\s:：]|$)"
    )
    RE_NUMBER = re.compile(r"^\s*(\d+)\s*[.．、）)]")
    RE_SCORE = re.compile(r"满分\s*([0-9０-９]+(?:\.\d+)?)\s*分")
    RE_TIME = re.compile(r"(?:在|答题(?:时间|时长)\s*(?:为|是)?)\s*([0-9０-９]+)\s*秒")
    RE_ANSWER_LABEL = re.compile(
        r"^\s*(?P<explicit>Answer\s*)?(?P<number>\d+)\s*[：:.．、）)](?!\d)\s*", re.I,
    )

    def parse(self):
        start = next((i for i, (_, text, _) in enumerate(self.paras)
                      if self.RE_HEADING.match(str(text or ''))), None)
        if start is None:
            return self._result([])

        source_start = self.paras[start][0]
        source_end = None
        number = score = answer_time = None
        prompt = ''
        script_lines = []
        answers = []
        answer_parts = []
        answer_labeled = False
        mode = 'preamble'
        script_start = None
        times = None

        def answer_label(value):
            label = self.RE_ANSWER_LABEL.match(value)
            # Bare numbering is a list marker only when it continues the
            # alternatives in order. A year/decimal in an answer is content.
            expected = len(answers) + bool(answer_parts) + 1
            if label and (label.group('explicit') or int(label.group('number')) == expected):
                return label
            return None

        def finish_answer():
            if answer_parts:
                answer = sanitize(' '.join(answer_parts)).strip()
                if answer:
                    answers.append(answer)
                answer_parts.clear()

        for position, (source_index, raw, _) in enumerate(self.paras[start:]):
            value = str(raw or '').strip()
            if position and is_major_section_heading(value):
                source_end = source_index
                break
            if not value:
                continue
            score_match = self.RE_SCORE.search(value)
            if score_match and score is None:
                score_value = float(score_match.group(1).translate(str.maketrans('０１２３４５６７８９', '0123456789')))
                score = int(score_value) if score_value.is_integer() else score_value
            number_match = self.RE_NUMBER.match(value)
            if number_match and number is None and ('计算机' in value or '转述' in value):
                number = int(number_match.group(1))
            time_match = self.RE_TIME.search(value)
            if time_match and answer_time is None:
                answer_time = int(time_match.group(1).translate(str.maketrans('０１２３４５６７８９', '0123456789')))
            if mode == 'preamble':
                explicit_times = listening_play_count(value)
                if explicit_times is not None:
                    times = explicit_times

            script = match_script_marker(value)
            if script:
                mode = 'script'
                script_start = source_index
                if script.group(1):
                    script_lines.append(script.group(1))
                continue
            answer_marker = match_answer_marker(value)
            if answer_marker:
                mode = 'answers'
                remainder = str(answer_marker.group(1) or '').strip()
                label = answer_label(remainder)
                if label:
                    answer_labeled = True
                    remainder = remainder[label.end():].strip()
                if remainder:
                    answer_parts.append(remainder)
                continue
            if mode == 'script':
                if '计算机' not in value and not is_chinese(value):
                    script_lines.append(value)
                continue
            if mode == 'answers':
                label = answer_label(value)
                if label:
                    finish_answer()
                    answer_labeled = True
                    remainder = value[label.end():].strip()
                    if remainder:
                        answer_parts.append(remainder)
                elif re.search(r'[A-Za-z]', value):
                    # Plain unlabelled paragraphs are separate alternatives;
                    # labelled alternatives may wrap across paragraphs.
                    if answer_parts and not answer_labeled:
                        finish_answer()
                    answer_parts.append(value)
                continue
            if not prompt and re.search(r'[A-Za-z]', value) and not is_chinese(value):
                prompt = sanitize(value)
        finish_answer()

        text = sanitize('\n'.join(script_lines))
        if not text:
            return self._result([])
        item = {
            'category': '听后转述录音稿', 'index': 1, 'text': text,
            'type_path': ['听后转述'],
            'filename_stem': audio_filename_stem(['听后转述'], 1),
            'audio_filename_stem': audio_filename_stem(['听后转述'], 1),
            'capabilities': {'parse': True, 'audio': True, 'normalize': True, 'external_input': False},
        }
        if number is not None:
            item.update(number=number, question_numbers=[number])
        if score is not None:
            item['score'] = score
        recording = {'listening_text': text}
        if times is not None:
            recording['times'] = times
        image_end = script_start if script_start is not None else source_end
        candidates = [block for block in self.document_blocks
                      if block.kind in {'embedded_image', 'textbox'}
                      and block.metadata.get('paragraph_index', -1) >= source_start
                      and (image_end is None or block.metadata.get('paragraph_index', -1) < image_end)]
        images = {block.metadata.get('embedded_image_index') for block in candidates if block.kind == 'embedded_image'}
        drawings = {block.metadata.get('drawing_group_index') for block in candidates if block.kind == 'textbox'}
        if len(images) == 1:
            kind, index = 'embedded_image', next(iter(images))
        elif not images and len(drawings) == 1:
            kind, index = 'drawing_group', next(iter(drawings))
        else:
            kind = index = None
        if kind and isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            recording.update(block_image_required=True, block_kind=kind, block_index=index)
        item['capabilities']['external_input'] = bool(
            recording.get('block_image_required') and prompt and answers and score is not None
        )
        item['major_section_profile'] = LISTENING_RETELLING_PROFILE
        item['entry_profile'] = LISTENING_RETELLING_ENTRY_PROFILE if item['capabilities']['external_input'] else None
        result = self._result([item])
        result['recording'] = recording
        result['retelling'] = {'number': number, 'prompt': prompt, 'score': score,
                               'answer_time': answer_time, 'reference_answers': answers}
        if score is not None:
            result['section_score'] = score
        return result

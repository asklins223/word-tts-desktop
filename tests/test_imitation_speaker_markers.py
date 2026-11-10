"""Explicit reading-task gender survives Word layout and voice planning."""

from pathlib import Path

import pytest
from docx import Document

from question_types.imitation_reading import ImitationReadingParser
from workflow.engine import WorkflowEngine
from workflow.parser import DocumentParser
from workflow.providers import XunfeiTTSAdapter
from workflow.system_input_content import build_page_input_facts
from wordtts.config import PARSER_VERSION
from wordtts.synthesis import build_synthesis_segments
from wordtts.tts_config import normalize_tts_config


FIXTURES = Path(__file__).resolve().parents[1] / 'examples' / 'documents'
PROFILE = normalize_tts_config({
    'default_female_voice': 'speaker:test-female',
    'default_male_voice': 'speaker:test-male',
    'role_configs': {
        '__default_female__': {'rate': 61, 'pitch': 48, 'volume': 55},
        '__default_male__': {'rate': 32, 'pitch': 52, 'volume': 46},
    },
})


def _voice_plan(item):
    plan = WorkflowEngine._effective_plan_item({
        'item_id': 'reading-21', 'item_type': item.item_type,
        'content': item.normalized_content, 'voice_key': item.voice_key, 'role': item.role,
    }, PROFILE, metadata=item.metadata)
    payload = {'plan': [plan], 'profile': PROFILE}
    spec = XunfeiTTSAdapter._legacy_item_specs(payload)[0]
    segments = build_synthesis_segments(
        spec['text'], spec['rate'], spec['volume'], spec['pitch'],
        default_voice=spec['default_voice'], female_voice=spec['female_voice'],
        male_voice=spec['male_voice'], role_configs=spec['role_configs'],
        role_voices=spec['role_voices'], default_role=spec['default_role'],
    )
    adapter = XunfeiTTSAdapter(account_scope='local-gender-regression', allow_real=False)
    work = adapter._legacy_works('local-gender-regression', payload)[0]
    assert work['items'][0]['segments'] == segments
    return segments


@pytest.mark.parametrize('name', ['武汉新题型-U4.docx', '武汉新题型-U5-1.docx'])
@pytest.mark.parametrize('placement,marker,gender', [
    ('before-box', '(M)', 'male'),
    ('before-box', '（ ｍ ）', 'male'),
    ('inside-box', '(M)', 'male'),
    ('inside-box', '（Ｍ）：', 'male'),
    ('own-line-in-box', '(m)', 'male'),
    ('before-box', '(W)', 'female'),
])
def test_actual_reading_passage_keeps_explicit_gender(tmp_path, name, placement, marker, gender):
    document = Document(FIXTURES / name)
    table = document.tables[0]
    if placement == 'before-box':
        paragraph = document.add_paragraph(marker)
        table._element.addprevious(paragraph._p)
    elif placement == 'own-line-in-box':
        table.cell(0, 0).paragraphs[0].insert_paragraph_before(marker)
    else:
        paragraph = table.cell(0, 0).paragraphs[0]
        paragraph.text = marker + paragraph.text
    source = tmp_path / name
    document.save(source)

    parsed = DocumentParser().parse(source)
    assert parsed.parser_version == str(PARSER_VERSION)
    item = next(item for item in parsed.items if item.metadata['doc_type'] == '模仿朗读')
    assert item.metadata['voice'] == gender
    segments = _voice_plan(item)
    assert len(segments) == 1
    segment = segments[0]
    assert segment['role'] == f'__default_{gender}__'
    assert segment['voice_key'] == f'speaker:test-{gender}'
    assert (segment['speed'], segment['pitch'], segment['volume']) == (
        (32, 52, 46) if gender == 'male' else (61, 48, 55)
    )
    question = item.metadata['page_input']['questions'][0]
    assert question['listening_text'] == segment['text']
    assert question['reference_answers'] == [segment['text']]


@pytest.mark.parametrize('layout', ['paragraph', 'legacy'])
def test_nonboxed_reading_also_reports_explicit_male_gender(tmp_path, layout):
    document = Document()
    lines = (
        ['三、模仿朗读（共1题，满分5分）',
         '21. 请听以下短文两遍，然后模仿朗读。', '（Ｍ）', 'Hello, I am a reader.']
        if layout == 'paragraph' else
        ['U5', '一、模仿朗读（共6分）', '教材：', '(M)', 'Hello, I am a reader.']
    )
    for line in lines:
        document.add_paragraph(line)
    source = tmp_path / 'reading.docx'
    document.save(source)
    result = ImitationReadingParser(source).parse()
    assert len(result['items']) == 1
    assert result['items'][0]['voice'] == 'male'


def test_box_annotation_does_not_change_the_next_reading_task(tmp_path):
    document = Document()
    document.add_paragraph('三、模仿朗读（共2题，满分10分）')
    document.add_paragraph('21. 请听短文，然后模仿朗读。')
    document.add_paragraph('(M)')
    document.add_table(rows=1, cols=1).cell(0, 0).text = 'The first reading task.'
    document.add_paragraph('22. 请听短文，然后模仿朗读。')
    document.add_table(rows=1, cols=1).cell(0, 0).text = 'The second reading task.'
    source = tmp_path / 'two-readings.docx'
    document.save(source)
    items = ImitationReadingParser(source).parse()['items']
    assert [item['voice'] for item in items] == ['male', 'female']
    assert items[1]['text'] == 'The second reading task.'


def test_inline_edit_can_override_stale_female_metadata():
    from workflow.parser import ParsedItem

    item = ParsedItem(
        identity_key='reading-21', item_type='模仿朗读-框内英文', sequence=0,
        normalized_content='（ M ）: First line.\n(W) Second line.',
        metadata={'voice': 'female'},
    )
    segments = _voice_plan(item)
    assert [(segment['role'], segment['voice_key'], segment['text']) for segment in segments] == [
        ('__default_male__', 'speaker:test-male', 'First line.'),
        ('__default_female__', 'speaker:test-female', 'Second line.'),
    ]


def test_cleaning_reference_gender_markers_keeps_string_answers_whole():
    facts = build_page_input_facts('模仿朗读', {'exam_form': 'paper'}, {
        'text': '(M) A full reading passage.', 'reference_answers': '(M) A full reading passage.',
        'score': 5, 'major_section_profile': 'imitation_boxed_special',
        'entry_profile': 'imitation_reading_v1', 'capabilities': {'external_input': True},
    }, 0)
    assert facts['questions'][0]['reference_answers'] == ['A full reading passage.']

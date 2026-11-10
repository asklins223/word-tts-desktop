"""The two November 2026 source papers through the real local entry pipeline."""

import io
import re
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from application.workflow_service import WorkflowApplicationService
from platform_entry.adapter.content_imitation import PlatformInputImitationContentMixin
from platform_entry.adapter.content_record import PlatformInputRecordContentMixin
from platform_entry.paper_input import normalize_spec
from question_types.segmenter import load_document_once, parse_document_once
from question_types.imitation_reading import ImitationReadingParser
from question_types.listening_response import ListeningResponseParser
from question_types.listening_retelling import ListeningRetellingParser
from question_types.text_utils import listening_play_count
from workflow.artifact_store import ArtifactStore
from workflow.database import WorkflowDatabase
from workflow.fake_provider import FakeProvider
from workflow.external import ExternalRecordService
from workflow.parser import DocumentParser
from workflow.providers import ProviderRegistry
from workflow.repositories import WorkflowRepository
from workflow.source_imports import SourceImportService
from workflow.system_input import SystemInputService
from workflow.system_input_executor import PlatformInputWorkflowPageExecutor


FIXTURES = Path(__file__).resolve().parents[1] / 'examples' / 'documents'
CASES = [
    ('武汉新题型-U4.docx', 'ABB CBCBCAC'.replace(' ', ''), 'BAABC', [5, 6, 3, 5, 10], 15),
    ('武汉新题型-U5-1.docx', 'ABCA BCABBB'.replace(' ', ''), 'BCBCA', [21, 4, 5, 5, 9], 20),
]


@pytest.mark.parametrize('name,selection_keys,response_keys,answer_counts,retelling_count', CASES)
def test_actual_paper_keeps_questions_answers_and_recording_controls(
    name, selection_keys, response_keys, answer_counts, retelling_count,
):
    results, summary = parse_document_once(FIXTURES / name)
    assert summary == '检测到 4 种题型，成功提取 19 条内容'
    selection, response, imitation, retelling = results
    assert [q['number'] for q in selection['questions']] == list(range(1, 11))
    assert ''.join(q['answer'] for q in selection['questions']) == selection_keys
    assert [q['script_ordinal'] for q in selection['questions']] == [1, 2, 3, 4, 5, 5, 6, 6, 7, 7]
    assert [item['times'] for item in selection['items']] == [2] * 7
    assert [q['number'] for q in response['questions']] == list(range(11, 21))
    assert ''.join(q['answer'] for q in response['questions'][:5]) == response_keys
    assert [q['section'] for q in response['questions']] == ['第一节'] * 5 + ['第二节'] * 5
    assert response['questions'][5]['number_inferred'] is True
    assert [len(q['reference_answers']) for q in response['questions'][5:]] == answer_counts
    assert [q['times'] for q in response['questions']] == [1] * 10
    assert [q['answer_time'] for q in response['questions']] == [5] * 10
    assert imitation['items'][0]['number'] == 21
    assert imitation['items'][0]['times'] == 2
    assert imitation['items'][0]['answer_time'] == 90
    assert retelling['retelling']['number'] == 22
    assert retelling['retelling']['answer_time'] == 90
    assert retelling['recording']['times'] == 2
    answers = retelling['retelling']['reference_answers']
    assert len(answers) == retelling_count
    assert all(not re.match(r'^(?:Answer\s*)?\d+[.:]', answer, re.I) for answer in answers)
    assert sum(q['score'] for q in selection['questions'] + response['questions']) + 5 + 5 == 30
    assert all('参考答案' not in item['text'] and 'Answer 1' not in item['text']
               for result in results for item in result['items'])


def _page_configuration(name):
    grade = 9 if 'U5-1' in name else 7
    return {
        'input_type': 'paper', 'paperCategory': '听说考试',
        'platformTemplateName': '武汉新题型', 'paperName': name,
        'platformTemplateId': 'wuhan-fixture', 'platformTemplateVersion': '1',
        'provinceId': {'id': 420000, 'name': '湖北省'},
        'cityId': {'id': 420100, 'name': '武汉市'}, 'districtIds': [],
        'stageId': {'id': 2, 'name': '初中'},
        'gradeId': {'id': grade, 'name': '九年级' if grade == 9 else '七年级'},
        'year': 2026, 'answerTimeMinutes': 20,
        'paperType': {'id': 1, 'name': '单元测试题'},
    }


@pytest.mark.parametrize('name,_selection,_response,answer_counts,retelling_count', CASES)
def test_actual_paper_import_generation_delivery_and_entry(
    tmp_path, name, _selection, _response, answer_counts, retelling_count,
):
    database = WorkflowDatabase(tmp_path / 'workflow.db', profile='full')
    database.initialize()
    try:
        artifacts = ArtifactStore(tmp_path / 'artifacts')
        repository = WorkflowRepository(database)
        registry = ProviderRegistry()
        provider = FakeProvider()
        registry.register(provider)
        service = WorkflowApplicationService(
            repository, SourceImportService(database, artifacts), artifacts,
            parser=DocumentParser(), providers=registry,
        )
        draft = service.create_draft('tts', {'source_filename': name, 'input_type': 'paper'})
        imported = service.import_source(
            draft.workflow_id, (FIXTURES / name).read_bytes(), filename=name, request_key=name,
        )
        parsed = service.parse(
            draft.workflow_id,
            expected_state_version=repository.get_workflow(draft.workflow_id).state_version,
            source_artifact_id=imported['source_artifact_id'],
        )
        assert parsed['workflow'].item_count == 19
        assert len(parsed['block_image_artifact_ids']) == 2
        _, generated = service.start_generation(
            draft.workflow_id, expected_state_version=parsed['workflow'].state_version,
            generation_mode='composite_cut', provider='fake', account_scope=provider.account_scope,
        )
        assert generated.status == 'SUCCEEDED'
        assert provider.submit_calls == 1
        export = service.create_export_zip(draft.workflow_id)
        storage = repository.get_artifact_storage(export['artifact_id'], workflow_id=draft.workflow_id)
        with artifacts.read(storage['storage_key']) as handle:
            with zipfile.ZipFile(io.BytesIO(handle.read())) as archive:
                names = [path for path in archive.namelist() if path.endswith('.mp3')]
        assert len(names) == len(set(names)) == 19

        input_service = SystemInputService(
            database, repository,
            external=ExternalRecordService(database, intent_log=repository.intent_log),
        )
        projection = input_service.sync_projection(draft.workflow_id)
        assert projection['document_entry_support']['supported'] is True
        assert projection['page_content_status']['status'] == 'complete'
        assert projection['audio_gate']['technical_status'] == 'passed'
        assert len(projection['units']) == 1
        unit = {**projection['units'][0], 'segments': projection['content_segments'],
                'configuration': _page_configuration(name)}
        executor = PlatformInputWorkflowPageExecutor(
            repository, artifacts, profile_dir=tmp_path / 'profile',
            admin_url='http://localhost', api_base='http://localhost/api', login_timeout=1,
        )
        base = tmp_path / 'page-assets'
        base.mkdir()
        raw = executor._build_raw_spec({'workflow_id': draft.workflow_id, 'unit': unit}, base)
        spec = normalize_spec(raw, base_dir=base)
        selection, response, imitation, retelling = spec.groups
        assert len(selection['materials']) == 7
        assert all(material['times'] == 2 for material in selection['materials'])
        assert len({material['audio_path'] for material in selection['materials']}) == 7
        assert [len(q['reference_answers']) for q in response['questions'][5:]] == answer_counts
        assert all(q['times'] == 1 for q in response['questions'])
        assert response['questions'][5]['listening_text'].startswith('W:')
        question_image = Path(response['questions'][4]['image_path'])
        guide_image = Path(retelling['recording']['image_path'])
        assert question_image != guide_image
        assert question_image.read_bytes() != guide_image.read_bytes()
        assert len(retelling['retelling']['reference_answers']) == retelling_count
        assert not re.match(r'^\d+\.', retelling['retelling']['reference_answers'][0])
        assert retelling['recording']['times'] == 2

        # Exercise the page handlers with the actual normalized source facts.
        owner = PlatformInputImitationContentMixin()
        owner._activate_group_section = Mock(return_value=([object()], [object()]))
        owner._next_listening_editor = Mock()
        owner._replace_rich_text = Mock()
        owner._upload_original_audio = Mock()
        owner._fill_material_numbers = Mock()
        owner._fill_recording_card = Mock()
        assert owner._fill_imitation_groups({'模仿朗读': [imitation]}) == {'录音题': 1}
        question = owner._fill_recording_card.call_args.args[1]
        assert question['answer_time'] == 90
        assert owner._fill_recording_card.call_args.kwargs['fill_answer_time'] is True
        assert owner._fill_material_numbers.call_args.args[0]['times'] == 2

        owner = PlatformInputRecordContentMixin()
        owner._activate_group_section = Mock(return_value=([object()], [object()]))
        owner._question_page_scope = Mock()
        owner._next_listening_editor = Mock()
        owner._replace_rich_text = Mock()
        owner._upload_original_audio = Mock()
        owner._upload_image = Mock()
        owner._fill_material_numbers = Mock()
        owner._fill_recording_card = Mock()
        assert owner._fill_listening_retelling_groups({'听后转述': [retelling]}) == {'录音题': 1}
        assert owner._fill_material_numbers.call_args.args[0]['times'] == 2
        assert owner._fill_recording_card.call_args.args[1]['reference_answers'] == retelling['retelling']['reference_answers']

        # Run the durable entry/receipt lifecycle with a local page stand-in.
        # Its input still passes the real artifact and page-schema gates.
        calls = []

        def local_page(payload):
            destination = tmp_path / f'local-entry-{len(calls)}'
            destination.mkdir()
            checked = executor._validated_spec(payload, destination)
            assert [group['type'] for group in checked.groups] == [
                '听后选择', '听后应答', '模仿朗读', '听后转述',
            ]
            assert checked.groups[1]['questions'][5]['reference_answers'] == response['questions'][5]['reference_answers']
            assert checked.groups[-1]['retelling']['reference_answers'] == retelling['retelling']['reference_answers']
            calls.append(payload)
            return {'external_record_id': 'local-fixture-paper', 'feedback': {'status': 'saved'}}

        input_service.executor = local_page
        saved = input_service.save_configuration(
            draft.workflow_id, repository.get_workflow(draft.workflow_id).state_version,
            {'delivery_mode': 'audio_and_input', 'input_type': 'paper', 'paper_category': '听说考试',
             'units': [{**_page_configuration(name), 'unit_id': unit['unit_id']}]},
        )
        accepted = input_service.accept_audio(draft.workflow_id, saved['snapshot']['state_version'])
        started = input_service.start_input(
            draft.workflow_id, accepted['snapshot']['state_version'],
            idempotency_key=f'local-wuhan-regression-{name}',
        )
        finished = input_service.execute_input_run(started['input_run_id'])
        assert finished['input_status'] == 'succeeded'
        assert len(calls) == 1
        assert input_service.execute_input_run(started['input_run_id'])['input_status'] == 'succeeded'
        assert len(calls) == 1
    finally:
        database.close()


@pytest.mark.parametrize('text,count', [
    ('每个句子听一遍。', 1), ('短文读两遍。', 2), ('录音播放２次。', 2),
    ('请听以下短文两遍，然后模仿朗读一遍。', 2),
    ('请朗读一遍。', None), ('请在90秒钟内朗读短文一遍。', None),
])
def test_recording_repeat_count_excludes_student_reading(text, count):
    assert listening_play_count(text) == count


@pytest.mark.parametrize('answers,expected', [
    (['1. John likes IT.', 'He studies in the computer room.',
      '2. IT is his favourite subject.', 'It is useful.'],
     ['John likes IT. He studies in the computer room.',
      'IT is his favourite subject. It is useful.']),
    (['Answer 1:', 'John likes IT.', 'Answer 2: IT is useful.'],
     ['John likes IT.', 'IT is useful.']),
    (['1876. That was the year.', '1.5 litres of water.'],
     ['1876. That was the year.', '1.5 litres of water.']),
])
def test_retelling_answer_labels_preserve_wrapped_prose_years_and_decimals(answers, expected):
    lines = [
        '四、听后转述（共1题，满分5分）',
        '22.（计算机语音提示）短文读两遍。',
        'John likes IT.', '【录音原文】', 'My favourite subject is IT.',
        '【参考答案】', *answers,
    ]
    parser = ListeningRetellingParser(
        'answer-labels.docx',
        preloaded_paras=([(i, line, '') for i, line in enumerate(lines)], []),
    )
    assert parser.parse()['retelling']['reference_answers'] == expected


def test_imitation_keeps_recording_time_separate_from_preparation():
    source = FIXTURES / '武汉新题型-U5-1.docx'
    paragraphs, metadata, blocks = load_document_once(source, include_structure=True)
    changed = []
    for index, text, style in paragraphs:
        if text.startswith('21.'):
            text = text.replace('90秒钟的时间准备', '30秒钟的时间准备')
            text = text.replace('90秒钟内朗读', '75秒钟内朗读')
        elif '录音时间进度条' in text:
            text = text.replace('90秒钟录音', '75秒钟录音')
        changed.append((index, text, style))
    parser = ImitationReadingParser(source, preloaded_paras=(changed, metadata, blocks))
    item = parser.parse()['items'][0]
    assert item['times'] == 2
    assert item['answer_time'] == 75


def test_response_sections_can_declare_different_play_counts():
    source = FIXTURES / '武汉新题型-U5-1.docx'
    paragraphs, metadata, blocks = load_document_once(source, include_structure=True)
    changed = [(index, text.replace('每段对话听一遍', '每段对话听两遍'), style)
               for index, text, style in paragraphs]
    parser = ListeningResponseParser(source, preloaded_paras=(changed, metadata, blocks))
    questions = parser.parse()['questions']
    assert [q['times'] for q in questions] == [1] * 5 + [2] * 5

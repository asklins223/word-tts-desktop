"""Information-acquisition specials through the local delivery and entry gates."""

from pathlib import Path
from unittest.mock import Mock

from docx import Document

from application.workflow_service import WorkflowApplicationService
from platform_entry.adapter.automation import PlatformInputPageAutomation
from platform_entry.paper_input import normalize_spec
from workflow.artifact_store import ArtifactStore
from workflow.database import WorkflowDatabase
from workflow.external import ExternalRecordService
from workflow.fake_provider import FakeProvider
from workflow.parser import DocumentParser
from workflow.providers import ProviderRegistry
from workflow.repositories import WorkflowRepository
from workflow.source_imports import SourceImportService
from workflow.system_input import SystemInputService
from workflow.system_input_executor import PlatformInputWorkflowPageExecutor


def _configuration():
    return {
        "input_type": "paper", "paperCategory": "题型专项",
        "platformTemplateName": "信息获取", "paperName": "U3 信息获取专项",
        "platformTemplateId": "info-acquisition-fixture", "platformTemplateVersion": "1",
        "provinceId": {"id": 440000, "name": "广东省"},
        "cityId": {"id": 440600, "name": "佛山市"}, "districtIds": [],
        "stageId": {"id": 2, "name": "初中"},
        "gradeId": {"id": 7, "name": "七年级"},
        "year": 2026, "answerTimeMinutes": 10,
        "paperType": {"id": 1, "name": "单元测试题"},
    }


def _run_local_entry(root: Path, source: Path):
    database = WorkflowDatabase(root / "workflow.db", profile="full")
    database.initialize()
    try:
        artifacts = ArtifactStore(root / "artifacts")
        repository = WorkflowRepository(database)
        registry = ProviderRegistry()
        provider = FakeProvider()
        registry.register(provider)
        service = WorkflowApplicationService(
            repository, SourceImportService(database, artifacts), artifacts,
            parser=DocumentParser(), providers=registry,
        )
        draft = service.create_draft("tts", {"source_filename": source.name, "input_type": "paper"})
        imported = service.import_source(
            draft.workflow_id, source.read_bytes(), filename=source.name, request_key="source",
        )
        parsed = service.parse(
            draft.workflow_id,
            expected_state_version=repository.get_workflow(draft.workflow_id).state_version,
            source_artifact_id=imported["source_artifact_id"],
        )
        assert parsed["workflow"].item_count == 14
        _, generated = service.start_generation(
            draft.workflow_id, expected_state_version=parsed["workflow"].state_version,
            generation_mode="composite_cut", provider="fake", account_scope=provider.account_scope,
        )
        assert generated.status == "SUCCEEDED"

        inputs = SystemInputService(
            database, repository,
            external=ExternalRecordService(database, intent_log=repository.intent_log),
        )
        projection = inputs.sync_projection(draft.workflow_id)
        assert projection["document_entry_support"]["supported"] is True
        assert projection["document_entry_support"]["format"] == "info_acquisition"
        assert projection["page_content_status"]["status"] == "complete"
        assert projection["audio_gate"]["technical_status"] == "passed"
        assert len(projection["units"]) == 1
        unit = {**projection["units"][0], "segments": projection["content_segments"],
                "configuration": _configuration()}
        executor = PlatformInputWorkflowPageExecutor(
            repository, artifacts, profile_dir=root / "profile",
            admin_url="http://localhost", api_base="http://localhost/api", login_timeout=1,
        )
        base = root / "page-assets"
        base.mkdir()
        payload = {"workflow_id": draft.workflow_id, "unit": unit}
        spec = normalize_spec(executor._build_raw_spec(payload, base), base_dir=base)
        assert spec.question_count == 10
        assert spec.bundle_rule == "platform_input-info-acquisition-special-v1"
        materials = spec.groups[0]["materials"]
        assert [len(material["questions"]) for material in materials] == [2, 2, 2, 4]
        assert all(question["score"] == 1.5 for material in materials for question in material["questions"])
        assert all(question["reference_answers"] for material in materials for question in material["questions"])
        assert all(Path(question["prompt_audio_path"]).is_file()
                   for material in materials for question in material["questions"])
        assert len({question["prompt_audio_path"] for material in materials
                    for question in material["questions"]}) == 10
        assert len({material["audio_path"] for material in materials}) == 4

        # The two lazy panels consume all ten recording cards with separate
        # listening/audio cursors, rather than looking for a preceding exam part.
        owner = object.__new__(PlatformInputPageAutomation)
        owner.spec = spec
        owner._activate_group_section = Mock(side_effect=[
            ([object() for _ in range(6)], [object() for _ in range(3)]),
            ([object() for _ in range(4)], [object()]),
        ])
        owner._replace_rich_text = Mock()
        owner._upload_original_audio = Mock()
        owner._fill_recording_card = Mock()
        owner._fill_grouped_content()
        assert [call.kwargs["outline_target"] for call in owner._activate_group_section.call_args_list] == [
            ("第1题(录音题)", 0), ("第7题(录音题)", 0),
        ]
        assert owner._upload_original_audio.call_count == 4
        assert owner._fill_recording_card.call_count == 10

        # Exercise the durable start/receipt gates with a local page stand-in.
        calls = []

        def local_page(input_payload):
            destination = root / f"local-entry-{len(calls)}"
            destination.mkdir()
            checked = executor._validated_spec(input_payload, destination)
            assert checked.question_count == 10
            assert checked.groups[0]["materials"][3]["questions"][0]["reference_answers"] == (
                materials[3]["questions"][0]["reference_answers"]
            )
            calls.append(input_payload)
            return {"external_record_id": "local-info-acquisition", "feedback": {"status": "saved"}}

        inputs.executor = local_page
        saved = inputs.save_configuration(
            draft.workflow_id, repository.get_workflow(draft.workflow_id).state_version,
            {"delivery_mode": "audio_and_input", "input_type": "paper", "paper_category": "题型专项",
             "units": [{**_configuration(), "unit_id": unit["unit_id"]}]},
        )
        accepted = inputs.accept_audio(draft.workflow_id, saved["snapshot"]["state_version"])
        started = inputs.start_input(
            draft.workflow_id, accepted["snapshot"]["state_version"],
            idempotency_key="local-info-acquisition",
        )
        assert inputs.execute_input_run(started["input_run_id"])["input_status"] == "succeeded"
        assert inputs.execute_input_run(started["input_run_id"])["input_status"] == "succeeded"
        assert len(calls) == 1
        return spec
    finally:
        database.close()


def test_special_import_generation_and_entry(tmp_path):
    document = Document()
    document.add_paragraph("U3")
    document.add_paragraph("二、信息获取（共15分）")
    document.add_paragraph("第一节 听选信息（1.5 分 ×6=9）")
    for pair in range(3):
        document.add_paragraph(
            f"听第{'一二三'[pair]}段对话，回答第{pair * 2 + 1}-{pair * 2 + 2}两个问题。"
        )
        for number in (pair * 2 + 1, pair * 2 + 2):
            document.add_paragraph(f"{number}. What is answer {number}?")
            document.add_paragraph("(First. / Second. / Third.)")
            document.add_paragraph("参考答案：First. / The first answer.")
        document.add_paragraph("录音稿：")
        document.add_paragraph(f"M: The first answer in conversation {pair + 1}.")
    document.add_paragraph("第二节 回答问题（1.5 分 ×4=6）")
    for number in range(7, 11):
        document.add_paragraph(f"{number}. What is answer {number}?")
        document.add_paragraph("参考答案：At school. / They practice at school.")
    document.add_paragraph("录音稿：(M) They practice at school every Thursday.")
    source = tmp_path / "anonymous.docx"
    document.save(source)

    spec = _run_local_entry(tmp_path, source)
    assert [len(q["reference_answers"]) for material in spec.groups[0]["materials"]
            for q in material["questions"]] == [2] * 10

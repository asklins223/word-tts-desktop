from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.workflow_routes import _dispatch_input_runs_once
from workflow.artifact_store import ArtifactStore
from workflow.database import WorkflowDatabase
from workflow.external import ExternalRecordService
from workflow.repositories import WorkflowRepository
from workflow.system_input import (
    SUPPORTED_EXTERNAL_INPUT_TYPES,
    SystemInputError,
    SystemInputService,
    _is_browser_closed_error,
    _unit_configuration,
    system_input_capabilities,
    system_input_capability,
    validate_system_input_configuration,
)
from workflow.system_input import _classify_input_type, _classify_paper_category
from workflow.system_input_executor import PlatformInputWorkflowPageExecutor, SystemInputExecutorRouter


class SystemInputServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wordtts-system-input-")
        root = Path(self.temp.name)
        self.database = WorkflowDatabase(root / "workflow.db", profile="full")
        self.database.initialize()
        self.artifacts = ArtifactStore(root / "artifacts")
        self.repository = WorkflowRepository(self.database)
        self.external = ExternalRecordService(self.database, intent_log=self.repository.intent_log)
        self.workflow = self.repository.create_workflow(
            "tts",
            {
                "source_filename": "模仿朗读-9上-U6.docx",
                "delivery_mode": "audio_and_input",
                "input_type": "paper",
            },
        )
        self.item_ids = []
        for sequence, unit_label in enumerate(("第1套", "第2套")):
            item_id = self.repository.create_item(
                self.workflow.workflow_id,
                item_type="imitation_reading",
                sequence=sequence,
                normalized_content=f"Reading passage {sequence + 1}.",
                item_identity_key=f"imitation:{sequence + 1}",
                metadata={
                    "unit": unit_label,
                    "type_path": ["模仿朗读", "教材"],
                    "raw_text": f"Listening original {sequence + 1}.",
                    "page_input": {
                        "schema_version": "paper-page-input-v1",
                        "input_type": "paper",
                        "type": "模仿朗读",
                        "questions": [{
                            "number": sequence + 1,
                            "listening_text": f"Reading passage {sequence + 1}.",
                            "score": 7,
                            "reference_answers": [f"Reading passage {sequence + 1}."],
                        }],
                    },
                    "score": 7,
                    "answer": "must stay source-only",
                    "source": "fixture",
                    "major_section_profile": "imitation_boxed_special",
                    "entry_profile": "imitation_reading_v1",
                    "capabilities": {
                        "parse": True,
                        "audio": True,
                        "normalize": True,
                        "external_input": True,
                    },
                },
                status="SUCCEEDED",
                source_locator=f"page:{sequence + 1}",
            )
            staged = self.artifacts.stage_stream(io.BytesIO(f"ID3-audio-{sequence}".encode()))
            blob = self.artifacts.promote(staged, format="mp3")
            self.repository.attach_imported_artifact(
                self.workflow.workflow_id,
                artifact_id=f"audio-{sequence + 1}",
                blob=blob,
                artifact_type="tts-segment",
                producer="test",
                producer_version="1",
                item_id=item_id,
            )
            self.item_ids.append(item_id)
        self.service = SystemInputService(
            self.database,
            self.repository,
            external=self.external,
        )
        self.projection = self.service.sync_projection(
            self.workflow.workflow_id,
            source_filename="模仿朗读-9上-U6.docx",
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_input_type_suggestion_uses_parser_facts_instead_of_filename(self) -> None:
        paper_item = {
            "item_type": "听后选择录音稿",
            "metadata_json": json.dumps({"doc_type": "听后选择", "category": "听后选择录音稿"}, ensure_ascii=False),
        }
        self.assertEqual(
            _classify_input_type([paper_item], "词汇导入模板.xlsx", {}),
            ("paper", "suggested"),
        )

        vocabulary_item = {
            "item_type": "单词",
            "metadata_json": json.dumps({"doc_type": "词汇", "category": "单词"}, ensure_ascii=False),
        }
        self.assertEqual(
            _classify_input_type([vocabulary_item], "听后选择-误命名.docx", {}),
            ("vocabulary", "suggested"),
        )

        textbook_item = {
            "item_type": "句子跟读",
            "metadata_json": json.dumps({"doc_type": "课文跟读", "category": "句子跟读"}, ensure_ascii=False),
        }
        self.assertEqual(
            _classify_input_type([textbook_item], "听说测试题-误命名.docx", {}),
            ("textbook", "suggested"),
        )

    def test_input_type_suggestion_fails_closed_for_unknown_or_mixed_parser_facts(self) -> None:
        self.assertEqual(
            _classify_input_type([], "词汇导入模板.xlsx", {}),
            ("paper", "unknown"),
        )
        mixed = [
            {"item_type": "单词", "metadata_json": json.dumps({"doc_type": "词汇"}, ensure_ascii=False)},
            {"item_type": "听后选择录音稿", "metadata_json": json.dumps({"doc_type": "听后选择"}, ensure_ascii=False)},
        ]
        self.assertEqual(
            _classify_input_type(mixed, "anything.docx", {}),
            ("paper", "conflict"),
        )

    def test_paper_category_suggestion_uses_parser_structure_and_respects_override(self) -> None:
        def item(doc_type: str, **metadata: object) -> dict[str, object]:
            facts = {"doc_type": doc_type, **metadata}
            return {
                "item_type": doc_type,
                "metadata_json": json.dumps(facts, ensure_ascii=False),
            }

        full_paper, status, evidence = _classify_paper_category(
            [item("听后选择"), item("模仿朗读")],
            {},
        )
        self.assertEqual((full_paper, status), ("听说考试", "suggested"))
        self.assertEqual(evidence["strategy"], "multiple_paper_types")

        special, status, evidence = _classify_paper_category(
            [item("听后选择")],
            {},
        )
        self.assertEqual((special, status), ("题型专项", "suggested"))
        self.assertEqual(evidence["strategy"], "single_paper_type")

        overridden, status, evidence = _classify_paper_category(
            [item("听后选择"), item("模仿朗读")],
            {"paper_category": "题型专项"},
        )
        self.assertEqual((overridden, status), ("题型专项", "user_override"))
        self.assertEqual(evidence["strategy"], "configuration")

        mixed, status, evidence = _classify_paper_category(
            [
                item("听后选择", paper_category="听说考试", exam_form="paper"),
                item("模仿朗读", paper_category="题型专项", exam_form="special"),
            ],
            {},
        )
        self.assertEqual((mixed, status), ("题型专项", "conflict"))
        self.assertEqual(evidence["strategy"], "conflicting_metadata")

        unresolved, status, evidence = _classify_paper_category(
            [item("听后选择", exam_form="unknown"), item("模仿朗读", exam_form="unknown")],
            {},
        )
        self.assertEqual((unresolved, status), ("题型专项", "conflict"))
        self.assertEqual(evidence["strategy"], "conflicting_metadata")

    def _configuration(self) -> dict[str, object]:
        common = {
            "paperCategory": "题型专项",
            "provinceId": {"id": 440000, "name": "广东省"},
            "cityId": {"id": 440600, "name": "佛山市"},
            "districtIds": [{"id": 440605, "name": "南海区"}],
            "stageId": {"id": 2, "name": "初中"},
            "gradeId": {"id": 9, "name": "九年级"},
            "year": 2026,
            "answerTimeMinutes": 20,
            "platformTemplateId": "mimic-reading-template",
            "platformTemplateName": "模仿朗读",
            "platformTemplateVersion": "1",
        }
        return {
            "delivery_mode": "audio_and_input",
            "input_type": "paper",
            "paper_category": "题型专项",
            "units": [
                {
                    **common,
                    "unit_id": unit["unit_id"],
                    "paperName": f"外研9上-U6-{unit['label']}",
                }
                for unit in self.projection["units"]
            ],
        }

    def test_input_type_capability_matrix_is_the_single_external_support_boundary(self) -> None:
        capabilities = {row["input_type"]: row for row in system_input_capabilities()}
        self.assertEqual(set(capabilities), {"paper", "textbook", "vocabulary"})
        self.assertEqual(SUPPORTED_EXTERNAL_INPUT_TYPES, {"paper", "textbook"})
        self.assertTrue(capabilities["paper"]["external_supported"])
        self.assertEqual(capabilities["paper"]["adapter_key"], "platform_input.paper")
        self.assertTrue(capabilities["textbook"]["external_supported"])
        self.assertEqual(capabilities["textbook"]["adapter_key"], "platform_input.textbook")
        self.assertFalse(capabilities["vocabulary"]["external_supported"])
        self.assertEqual(system_input_capability(" PAPER ")["input_type"], "paper")
        self.assertIsNone(system_input_capability("not-registered"))

    def test_reserved_content_types_can_be_saved_without_inheriting_paper_fields(self) -> None:
        canonical = validate_system_input_configuration({
            "delivery_mode": "audio_and_input",
            "input_type": "textbook",
            "paperName": "must-not-leak",
            "paperCategory": "题型专项",
            "platformTemplateName": "textbook-template",
            "textbookNameZh": "Section A",
            "textbookNameEn": "How do we get to know each other?",
            "units": [{
                "unit_id": self.projection["units"][0]["unit_id"],
                "paperName": "also-must-not-leak",
                "paperCategory": "题型专项",
                "platformTemplateName": "unit-template",
                "textbookNameZh": "Section A",
            }],
        })
        self.assertNotIn("paperName", canonical)
        self.assertNotIn("paperCategory", canonical)
        self.assertEqual(canonical["platformTemplateName"], "textbook-template")
        self.assertEqual(canonical["textbookNameZh"], "Section A")
        self.assertNotIn("paperName", canonical["units"][0])
        self.assertNotIn("paperCategory", canonical["units"][0])
        self.assertEqual(canonical["units"][0]["textbookNameZh"], "Section A")

        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            {"delivery_mode": "audio_and_input", "input_type": "textbook"},
        )
        self.assertTrue(saved["projection"]["supported_external_input"])
        self.assertEqual(saved["projection"]["input_capability"]["status"], "supported")
        self.assertTrue(all(
            "paperName" not in unit.get("configuration", {})
            and "paperCategory" not in unit.get("configuration", {})
            for unit in saved["projection"]["units"]
        ))

        self._finish_audio_workflow()
        # 课文能力已开放，但文档结构门禁仍然生效：fixture 是模仿朗读
        # 文档，不能按课文录入。
        with self.assertRaises(SystemInputError) as error:
            self.service.start_input(
                self.workflow.workflow_id,
                self.repository.get_workflow(self.workflow.workflow_id).state_version,
                idempotency_key="reserved-textbook-input-0001",
            )
        self.assertEqual(error.exception.code, "SYSTEM_INPUT_DOCUMENT_UNSUPPORTED")

    def test_textbook_start_requires_connected_adapter(self) -> None:
        workflow = self.repository.create_workflow(
            "tts",
            {
                "source_filename": "课文跟读-7上-Unit1 You and Me.docx",
                "delivery_mode": "audio_and_input",
                "input_type": "textbook",
            },
        )
        for sequence in range(2):
            item_id = self.repository.create_item(
                workflow.workflow_id,
                item_type="text_reading",
                sequence=sequence,
                normalized_content=f"Hello {sequence + 1}!",
                item_identity_key=f"text:{sequence + 1}",
                metadata={
                    "doc_type": "课文跟读",
                    "category": "句子跟读",
                    "section": "Section A",
                    "raw_text": f"Hello {sequence + 1}!",
                    "translation": f"你好{sequence + 1}！",
                },
                status="SUCCEEDED",
                source_locator=f"page:{sequence + 1}",
            )
            staged = self.artifacts.stage_stream(io.BytesIO(f"textbook-audio-{sequence}".encode()))
            blob = self.artifacts.promote(staged, format="mp3")
            self.repository.attach_imported_artifact(
                workflow.workflow_id,
                artifact_id=f"textbook-audio-{sequence + 1}",
                blob=blob,
                artifact_type="tts-segment",
                producer="test",
                producer_version="1",
                item_id=item_id,
            )
        projection = self.service.sync_projection(
            workflow.workflow_id,
            source_filename="课文跟读-7上-Unit1 You and Me.docx",
        )
        self.assertTrue(projection["document_entry_support"]["supported"])
        self.assertEqual(projection["input_type"], "textbook")
        configuration = {
            "delivery_mode": "audio_and_input",
            "input_type": "textbook",
            "units": [{
                "unit_id": projection["units"][0]["unit_id"],
                "textbookNameZh": "Section A",
                "textbookNameEn": "How do we get to know each other?",
                "textbookForm": "同步课文",
                "textbookVersion": "人教版",
                "textbookStage": "初中",
                "textbookGrade": "七年级",
                "textbookVolume": "上册",
                "textbookUnit": "Unit 1",
                "textbookLesson": "Section A",
            }],
        }
        self.service.save_configuration(
            workflow.workflow_id,
            self.repository.get_workflow(workflow.workflow_id).state_version,
            configuration,
        )
        with self.database.transaction() as con:
            con.execute(
                """UPDATE workflows SET status='ACTIVE', execution_state='TERMINAL',
                   control_state='TERMINATED', cleanup_state='SUCCEEDED',
                   result_status='SUCCEEDED', finished_at=updated_at
                   WHERE workflow_id=?""",
                (workflow.workflow_id,),
            )
        self.service.accept_audio(
            workflow.workflow_id,
            self.repository.get_workflow(workflow.workflow_id).state_version,
        )
        # 能力矩阵开放了课文，但执行器路由没有连接课文适配器时，
        # start 仍然必须在任何外部副作用之前拒绝。
        router = SystemInputExecutorRouter({"platform_input.paper": object()})
        service = SystemInputService(
            self.database,
            self.repository,
            external=self.external,
            executor=router,
        )
        with self.assertRaises(SystemInputError) as error:
            service.start_input(
                workflow.workflow_id,
                self.repository.get_workflow(workflow.workflow_id).state_version,
                idempotency_key="textbook-adapter-missing-0001",
            )
        self.assertEqual(error.exception.code, "SYSTEM_INPUT_TYPE_UNSUPPORTED")

    def test_executor_router_dispatches_registered_type_and_fails_closed(self) -> None:
        calls: list[str] = []

        class PaperAdapter:
            def preflight(self, payload: dict[str, object]) -> None:
                calls.append(f"preflight:{payload['unit']['unit_id']}")

            def __call__(self, payload: dict[str, object]) -> dict[str, object]:
                calls.append(f"execute:{payload['unit']['unit_id']}")
                return {"external_record_id": "paper-1"}

        router = SystemInputExecutorRouter({"platform_input.paper": PaperAdapter()})
        payload = {"unit": {"unit_id": "unit-1", "input_type": "paper"}, "target": {"input_type": "paper"}}
        router.preflight(payload)
        self.assertEqual(router(payload)["external_record_id"], "paper-1")
        self.assertEqual(calls, ["preflight:unit-1", "execute:unit-1"])
        self.assertTrue(router.supports_input_type("paper"))
        self.assertFalse(router.supports_input_type("textbook"))

        with self.assertRaises(SystemInputError) as error:
            router({"unit": {"unit_id": "unit-2", "input_type": "textbook"}})
        self.assertEqual(error.exception.code, "SYSTEM_INPUT_TYPE_UNSUPPORTED")

    def _finish_audio_workflow(self) -> None:
        with self.database.transaction() as con:
            con.execute(
                """UPDATE workflows SET status='ACTIVE', execution_state='TERMINAL',
                   control_state='TERMINATED', cleanup_state='SUCCEEDED',
                   result_status='SUCCEEDED', finished_at=updated_at
                   WHERE workflow_id=?""",
                (self.workflow.workflow_id,),
            )

    def test_projection_keeps_two_units_and_source_facts_without_mixing_config(self) -> None:
        self.assertEqual(len(self.projection["units"]), 2)
        self.assertEqual(self.projection["unit_count_status"], "multiple_confirmed")
        self.assertEqual(self.projection["parse_coverage_status"], "complete")
        self.assertEqual(self.projection["paper_category"], "题型专项")
        self.assertEqual(self.projection["paper_category_status"], "suggested")
        self.assertEqual(self.projection["audio_gate"]["technical_status"], "passed")
        self.assertEqual(len(self.projection["content_segments"]), 2)
        self.assertEqual(self.projection["content_segments"][0]["score"], 7.0)
        self.assertIn("模仿朗读", self.projection["structure_nodes"][1]["path"])
        for unit in self.projection["units"]:
            configuration = unit["configuration"]
            self.assertNotIn("score", configuration)
            self.assertNotIn("answer", configuration)
            self.assertNotIn("audio", configuration)

    def test_history_projection_exposes_system_input_phase_facts(self) -> None:
        record = self._history_record(self.workflow.workflow_id)
        self.assertEqual(record["delivery_mode"], "audio_and_input")
        self.assertEqual(record["input_status"], "pending_config")
        self.assertEqual(record["input_type"], "paper")
        self.assertEqual(record["input_units_total"], 2)
        self.assertEqual(record["input_units_succeeded"], 0)

        # 仅音频任务没有录入事实时保持 not_enabled，不被误标成录入任务。
        audio_only = self.repository.create_workflow("tts", {"source_filename": "仅音频.docx"})
        record = self._history_record(audio_only.workflow_id)
        self.assertEqual(record["delivery_mode"], "audio_only")
        self.assertEqual(record["input_status"], "not_enabled")
        self.assertEqual(record["input_units_total"], 0)

        # 顶层 delivery_mode 是规范事实，过期的嵌套 system_input 副本不能覆盖它。
        stale_nested = self.repository.create_workflow("tts", {
            "source_filename": "过期嵌套副本.docx",
            "delivery_mode": "audio_and_input",
            "system_input": {"delivery_mode": "audio_only"},
        })
        record = self._history_record(stale_nested.workflow_id)
        self.assertEqual(record["delivery_mode"], "audio_and_input")
        self.assertEqual(record["input_status"], "pending_config")
        demoted = self.repository.create_workflow("tts", {
            "source_filename": "顶层降级.docx",
            "delivery_mode": "audio_only",
            "system_input": {"delivery_mode": "audio_and_input"},
        })
        record = self._history_record(demoted.workflow_id)
        self.assertEqual(record["delivery_mode"], "audio_only")
        self.assertEqual(record["input_status"], "not_enabled")

        # 录入运行与条目状态直接决定历史卡片上的录入阶段，与录入投影一致。
        with self.database.transaction() as con:
            batch = con.execute(
                "SELECT audio_batch_id FROM audio_batches WHERE workflow_id=?",
                (self.workflow.workflow_id,),
            ).fetchone()
            units = con.execute(
                "SELECT unit_id, label FROM input_units WHERE workflow_id=? ORDER BY ordinal",
                (self.workflow.workflow_id,),
            ).fetchall()
            now = "2026-01-01T00:00:00+00:00"
            con.execute(
                """INSERT INTO input_runs(input_run_id, workflow_id, input_type, status,
                       audio_revision, artifact_manifest_hash, configuration_revision,
                       target_snapshot_json, payload_hash, idempotency_key, created_at, updated_at)
                   VALUES ('run-history-1', ?, 'paper', 'RUNNING', 1, 'manifest-hash', 1,
                           '{}', ?, 'idem-history-1', ?, ?)""",
                (self.workflow.workflow_id, "a" * 64, now, now),
            )
            for index, unit in enumerate(units):
                con.execute(
                    """INSERT INTO input_entries(entry_id, workflow_id, unit_id, audio_batch_id,
                           input_type, unit_label, input_status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'paper', ?, 'succeeded', ?, ?)""",
                    (
                        f"entry-history-{index + 1}",
                        self.workflow.workflow_id,
                        unit["unit_id"],
                        batch["audio_batch_id"],
                        unit["label"],
                        now,
                        now,
                    ),
                )
        record = self._history_record(self.workflow.workflow_id)
        self.assertEqual(record["input_status"], "running")
        self.assertEqual(record["input_units_total"], 2)
        self.assertEqual(record["input_units_succeeded"], 2)

    def _history_record(self, workflow_id: str) -> dict[str, object]:
        return next(
            record
            for record in self.repository.list_history_records(limit=20)
            if record["workflow_id"] == workflow_id
        )

    def test_projection_leaves_paper_names_blank_until_user_fills_them(self) -> None:
        names = [unit["configuration"].get("paperName") for unit in self.projection["units"]]
        self.assertEqual(names, [None, None])

    def test_multiple_units_drop_legacy_top_level_paper_name(self) -> None:
        canonical = validate_system_input_configuration({
            "input_type": "paper",
            "paperName": "旧版全局名称",
            "units": [
                {"unit_id": "unit-1", "paperName": "第一套"},
                {"unit_id": "unit-2", "paperName": "第二套"},
            ],
        })

        self.assertNotIn("paperName", canonical)
        self.assertEqual(
            [unit["paperName"] for unit in canonical["units"]],
            ["第一套", "第二套"],
        )

    def test_first_multiple_unit_save_drops_legacy_name_from_old_snapshot(self) -> None:
        legacy = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            {
                "delivery_mode": "audio_and_input",
                "input_type": "paper",
                "paperName": "旧版全局名称",
            },
        )
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            legacy["snapshot"]["state_version"],
            {
                "delivery_mode": "audio_and_input",
                "input_type": "paper",
                "units": [
                    {"unit_id": self.projection["units"][0]["unit_id"], "paperName": "第一套"},
                    {"unit_id": self.projection["units"][1]["unit_id"], "paperName": "第二套"},
                ],
            },
        )

        with self.database.read_transaction() as con:
            row = con.execute(
                "SELECT configuration_snapshot FROM workflows WHERE workflow_id=?",
                (self.workflow.workflow_id,),
            ).fetchone()
        stored = json.loads(row["configuration_snapshot"])
        self.assertNotIn("paperName", stored)
        self.assertEqual(
            [unit["configuration"]["paperName"] for unit in saved["projection"]["units"]],
            ["第一套", "第二套"],
        )

    def test_legacy_global_workflow_name_is_not_spread_to_multiple_units(self) -> None:
        config = {
            "input_type": "paper",
            "paper_category": "题型专项",
            "paperName": "外研九上-1-第17题专项卷-第16题专项卷",
            "units": [{"unit_id": "old-unit"}],
        }
        labels = ["第16题专项卷", "第17题专项卷"]

        first = _unit_configuration(
            config,
            "new-unit-1",
            unit_label=labels[0],
            unit_index=0,
            unit_total=2,
            unit_labels=labels,
            input_type="paper",
        )
        second = _unit_configuration(
            config,
            "new-unit-2",
            unit_label=labels[1],
            unit_index=1,
            unit_total=2,
            unit_labels=labels,
            input_type="paper",
        )

        expected = "外研九上-1-第17题专项卷-第16题专项卷"
        self.assertNotIn("paperName", first)
        self.assertNotIn("paperName", second)

        single = _unit_configuration(
            config,
            "single-unit",
            unit_label=labels[0],
            unit_index=0,
            unit_total=1,
            unit_labels=labels[:1],
            input_type="paper",
        )
        self.assertEqual(single["paperName"], expected)

        explicit = _unit_configuration(
            {
                "input_type": "paper",
                "paperName": "旧版全局名称",
                "units": [
                    {"unit_id": "new-unit-1"},
                    {"unit_id": "new-unit-2", "paperName": "单元自定义名称"},
                ],
            },
            "new-unit-2",
            unit_label=labels[1],
            unit_index=1,
            unit_total=2,
            unit_labels=labels,
            input_type="paper",
        )
        self.assertEqual(explicit["paperName"], "单元自定义名称")

        blank = _unit_configuration(
            {"input_type": "paper", "units": [{"unit_id": "old-unit"}]},
            "new-unit-3",
            unit_label=labels[0],
            unit_index=0,
            unit_total=2,
            unit_labels=labels,
            input_type="paper",
        )
        self.assertNotIn("paperName", blank)

    def test_unit_markers_without_structure_evidence_stay_as_one_candidate_unit(self) -> None:
        workflow = self.repository.create_workflow(
            "tts",
            {
                "source_filename": "marker-only.docx",
                "input_type": "paper",
            },
        )
        for sequence, unit_label in enumerate(("U1", "U2")):
            self.repository.create_item(
                workflow.workflow_id,
                item_type="imitation_reading",
                sequence=sequence,
                normalized_content=f"Marker-only content {sequence + 1}.",
                item_identity_key=f"marker-only:{sequence + 1}",
                metadata={"unit": unit_label},
                status="SUCCEEDED",
                source_locator=f"paragraph:{sequence + 1}",
            )

        projection = self.service.sync_projection(
            workflow.workflow_id,
            source_filename="marker-only.docx",
        )

        self.assertEqual(len(projection["units"]), 1)
        self.assertEqual(projection["unit_count_status"], "multiple_candidate")
        grouping = projection["units"][0]["evidence"]["unit_grouping"]
        self.assertEqual(grouping["strategy"], "candidate_requires_confirmation")
        self.assertEqual(len(grouping["candidate_boundaries"]), 2)
        self.assertFalse(grouping["structure_repeated"])

    def test_single_unit_override_merges_derived_ranges(self) -> None:
        workflow = self.repository.create_workflow(
            "tts",
            {
                "source_filename": "single-override.docx",
                "input_type": "paper",
                "unit_count_override": "single",
            },
        )
        for sequence, unit_label in enumerate(("U1", "U2")):
            self.repository.create_item(
                workflow.workflow_id,
                item_type="imitation_reading",
                sequence=sequence,
                normalized_content=f"Single override content {sequence + 1}.",
                item_identity_key=f"single-override:{sequence + 1}",
                metadata={
                    "unit": unit_label,
                    "type_path": ["模仿朗读", "教材"],
                },
                status="SUCCEEDED",
                source_locator=f"paragraph:{sequence + 1}",
            )

        projection = self.service.sync_projection(
            workflow.workflow_id,
            source_filename="single-override.docx",
        )

        self.assertEqual(len(projection["units"]), 1)
        self.assertEqual(projection["unit_count_status"], "single_default")
        self.assertEqual(len(projection["content_segments"]), 2)
        self.assertEqual(
            projection["units"][0]["evidence"]["unit_grouping"]["strategy"],
            "user_override_single",
        )

    def test_boundary_confirmation_survives_reparse_and_stale_items_require_reconfirmation(self) -> None:
        workflow = self.repository.create_workflow(
            "tts",
            {
                "source_filename": "boundary-review.docx",
                "input_type": "paper",
            },
        )
        for sequence, unit_label in enumerate(("U1", "U1", "U2", "U2")):
            self.repository.create_item(
                workflow.workflow_id,
                item_type="imitation_reading",
                sequence=sequence,
                normalized_content=f"Boundary content {sequence + 1}.",
                item_identity_key=f"boundary-review:{sequence + 1}",
                metadata={"unit": unit_label},
                status="SUCCEEDED",
                source_locator=f"paragraph:{sequence + 1}",
            )

        initial = self.service.sync_projection(workflow.workflow_id, source_filename="boundary-review.docx")
        self.assertEqual(initial["unit_count_status"], "multiple_candidate")
        grouping = initial["units"][0]["evidence"]["unit_grouping"]
        candidate_boundaries = grouping["candidate_boundaries"]
        self.assertEqual(len(candidate_boundaries), 2)
        state_version = self.repository.get_workflow(workflow.workflow_id).state_version
        confirmed = self.service.confirm_unit_boundaries(
            workflow.workflow_id,
            state_version,
            mode="multiple",
            boundaries=[
                {"item_ids": candidate["item_ids"], "label": f"用户单元 {index + 1}"}
                for index, candidate in enumerate(candidate_boundaries)
            ],
        )
        confirmed_ids = [unit["unit_id"] for unit in confirmed["projection"]["units"]]
        self.assertEqual(confirmed["projection"]["unit_count_status"], "multiple_confirmed")
        self.assertEqual(confirmed["decision"]["mode"], "multiple")
        self.assertEqual([unit["label"] for unit in confirmed["projection"]["units"]], ["用户单元 1", "用户单元 2"])

        with self.database.transaction() as con:
            metadata = json.loads(
                con.execute(
                    "SELECT metadata_json FROM work_items WHERE workflow_id=? ORDER BY sequence LIMIT 1",
                    (workflow.workflow_id,),
                ).fetchone()[0]
            )
            metadata["unit"] = "重新解析后的标记"
            con.execute(
                "UPDATE work_items SET metadata_json=? WHERE workflow_id=? AND sequence=0",
                (json.dumps(metadata, ensure_ascii=False), workflow.workflow_id),
            )
        refreshed = self.service.sync_projection(workflow.workflow_id, source_filename="boundary-review.docx")
        self.assertEqual(refreshed["unit_count_status"], "multiple_confirmed")
        self.assertEqual([unit["unit_id"] for unit in refreshed["units"]], confirmed_ids)
        self.assertEqual(refreshed["unit_boundary_decision"]["mode"], "multiple")

        self.repository.create_item(
            workflow.workflow_id,
            item_type="imitation_reading",
            sequence=4,
            normalized_content="A newly parsed item.",
            item_identity_key="boundary-review:5",
            metadata={"unit": "U3"},
            status="SUCCEEDED",
            source_locator="paragraph:5",
        )
        stale = self.service.sync_projection(workflow.workflow_id, source_filename="boundary-review.docx")
        self.assertEqual(stale["unit_count_status"], "multiple_candidate")
        self.assertEqual(stale["unit_boundary_decision"]["mode"], "multiple")
        self.assertEqual(stale["units"][0]["evidence"]["unit_grouping"]["strategy"], "user_override_stale")

        merged = self.service.confirm_unit_boundaries(
            workflow.workflow_id,
            self.repository.get_workflow(workflow.workflow_id).state_version,
            mode="single",
        )
        self.assertEqual(len(merged["projection"]["units"]), 1)
        self.assertEqual(merged["projection"]["unit_count_status"], "single_default")
        self.assertEqual(merged["decision"]["mode"], "single")

    def test_projection_removes_units_that_disappear_after_reparse(self) -> None:
        stale_unit_id = self.projection["units"][1]["unit_id"]
        with self.database.transaction() as con:
            metadata = json.loads(
                con.execute(
                    "SELECT metadata_json FROM work_items WHERE item_id=?",
                    (self.item_ids[1],),
                ).fetchone()[0]
            )
            metadata["unit"] = "第1套"
            con.execute(
                "UPDATE work_items SET metadata_json=? WHERE item_id=?",
                (json.dumps(metadata, ensure_ascii=False), self.item_ids[1]),
            )

        refreshed = self.service.sync_projection(
            self.workflow.workflow_id,
            source_filename="模仿朗读-9上-U6.docx",
        )

        self.assertEqual(len(refreshed["units"]), 1)
        self.assertNotIn(stale_unit_id, {unit["unit_id"] for unit in refreshed["units"]})
        self.assertEqual(
            len(refreshed["content_segments"]),
            2,
        )
        with self.database.read_transaction() as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM input_units WHERE workflow_id=?", (self.workflow.workflow_id,)).fetchone()[0],
                1,
            )

    def test_numeric_only_page_ids_do_not_make_configuration_executable(self) -> None:
        configuration = self._configuration()
        for unit in configuration["units"]:
            unit["provinceId"] = 440000
            unit["cityId"] = 440600
            unit["stageId"] = 2
            unit["gradeId"] = 9
            unit["platformTemplateName"] = None

        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            configuration,
        )

        self.assertEqual(saved["projection"]["input_status"], "pending_config")

    def test_fractional_required_numbers_do_not_get_truncated_into_executable_values(self) -> None:
        configuration = self._configuration()
        configuration["units"][0]["year"] = 2026.5
        configuration["units"][1]["answerTimeMinutes"] = 20.5

        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            configuration,
        )

        self.assertEqual(saved["projection"]["input_status"], "pending_config")

    def test_content_like_fields_are_rejected_from_unit_configuration(self) -> None:
        configuration = self._configuration()
        configuration["units"][0]["raw_text"] = "不得进入系统录入配置"

        with self.assertRaises(SystemInputError) as error:
            self.service.save_configuration(
                self.workflow.workflow_id,
                self.workflow.state_version,
                configuration,
            )

        self.assertEqual(error.exception.code, "SYSTEM_INPUT_CONFIG_FORBIDDEN_FIELD")

    def test_platform_template_name_cannot_be_numeric_only(self) -> None:
        with self.assertRaises(SystemInputError) as error:
            self.service.create_platform_template("paper", "123456")

        self.assertEqual(error.exception.code, "VALIDATION_ERROR")

    def test_platform_template_accepts_name_without_platform_owned_id(self) -> None:
        configuration = self._configuration()
        for unit in configuration["units"]:
            unit.pop("platformTemplateId", None)

        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            configuration,
        )

        self.assertEqual(saved["projection"]["input_status"], "pending_execute")

    def test_platform_template_accepts_name_without_version_snapshot(self) -> None:
        configuration = self._configuration()
        for unit in configuration["units"]:
            unit.pop("platformTemplateVersion", None)

        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            configuration,
        )

        self.assertEqual(saved["projection"]["input_status"], "pending_execute")

    def test_delivery_mode_patch_preserves_saved_unit_configuration(self) -> None:
        configured = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        audio_only = self.service.save_configuration(
            self.workflow.workflow_id,
            configured["snapshot"]["state_version"],
            {"delivery_mode": "audio_only"},
        )
        self.assertEqual(audio_only["projection"]["delivery_mode"], "audio_only")
        self.assertEqual(audio_only["projection"]["units"][0]["configuration"]["paperName"], "外研9上-U6-第1套")
        self.assertEqual(
            [entry["document_name"] for entry in audio_only["projection"]["entries"]],
            ["外研9上-U6-第1套", "外研9上-U6-第2套"],
        )

        restored = self.service.save_configuration(
            self.workflow.workflow_id,
            audio_only["snapshot"]["state_version"],
            {"delivery_mode": "audio_and_input"},
        )
        self.assertEqual(restored["projection"]["delivery_mode"], "audio_and_input")
        self.assertEqual(restored["projection"]["input_type"], "paper")
        self.assertEqual(restored["projection"]["input_status"], "pending_execute")

    def test_audio_only_target_save_remains_visible_and_can_enable_input(self) -> None:
        configuration = self._configuration()
        configuration["delivery_mode"] = "audio_only"

        audio_only = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            configuration,
        )

        self.assertEqual(audio_only["projection"]["delivery_mode"], "audio_only")
        self.assertEqual(
            [unit["configuration"]["paperName"] for unit in audio_only["projection"]["units"]],
            ["外研9上-U6-第1套", "外研9上-U6-第2套"],
        )

        enabled = self.service.save_configuration(
            self.workflow.workflow_id,
            audio_only["snapshot"]["state_version"],
            {"delivery_mode": "audio_and_input"},
        )

        self.assertEqual(enabled["projection"]["delivery_mode"], "audio_and_input")
        self.assertEqual(enabled["projection"]["input_status"], "pending_execute")

    def test_partial_unit_payload_does_not_erase_another_saved_target(self) -> None:
        configured = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        partial = self._configuration()
        partial["delivery_mode"] = "audio_only"
        partial["units"] = partial["units"][:1]

        audio_only = self.service.save_configuration(
            self.workflow.workflow_id,
            configured["snapshot"]["state_version"],
            partial,
        )

        self.assertEqual(
            [unit["configuration"]["paperName"] for unit in audio_only["projection"]["units"]],
            ["外研9上-U6-第1套", "外研9上-U6-第2套"],
        )

        restored = self.service.save_configuration(
            self.workflow.workflow_id,
            audio_only["snapshot"]["state_version"],
            {"delivery_mode": "audio_and_input"},
        )
        self.assertEqual(
            [unit["configuration"]["paperName"] for unit in restored["projection"]["units"]],
            ["外研9上-U6-第1套", "外研9上-U6-第2套"],
        )

    def test_workspace_start_action_accepts_name_without_platform_metadata(self) -> None:
        configuration = self._configuration()
        for unit in configuration["units"]:
            unit.pop("platformTemplateId", None)
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            configuration,
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        capabilities = {
            "system_input": {
                "available": True,
                "executor_available": True,
                "supported_external_input_types": ["paper"],
            },
        }

        workspace = self.repository.get_workspace(
            self.workflow.workflow_id,
            capabilities=capabilities,
        )
        start_action = next(
            action for action in workspace["available_actions"]
            if action["type"] == "START_INPUT"
        )
        self.assertTrue(start_action["enabled"])

    def test_workspace_start_action_matches_server_input_prerequisites(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        capabilities = {
            "system_input": {
                "available": True,
                "executor_available": True,
                "supported_external_input_types": ["paper"],
            },
        }

        ready = self.repository.get_workspace(
            self.workflow.workflow_id,
            capabilities=capabilities,
        )
        start_action = next(
            action for action in ready["available_actions"]
            if action["type"] == "START_INPUT"
        )
        self.assertTrue(start_action["enabled"])

        with self.database.transaction() as con:
            con.execute(
                "UPDATE input_units SET unit_count_status='multiple_candidate' WHERE workflow_id=?",
                (self.workflow.workflow_id,),
            )
        blocked = self.repository.get_workspace(
            self.workflow.workflow_id,
            capabilities=capabilities,
        )
        start_action = next(
            action for action in blocked["available_actions"]
            if action["type"] == "START_INPUT"
        )
        self.assertFalse(start_action["enabled"])
        self.assertEqual(start_action["reason"], "录入单元数量仍待确认")

    def test_incomplete_page_facts_disable_workspace_and_server_start_together(self) -> None:
        configuration = self._configuration()
        configuration["paper_category"] = "听说考试"
        for unit in configuration["units"]:
            unit["paperCategory"] = "听说考试"
            unit["paperType"] = {"id": "imitation-reading", "name": "模仿朗读"}
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            configuration,
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT metadata_json FROM work_items WHERE item_id=?",
                (self.item_ids[0],),
            ).fetchone()
            metadata = json.loads(row["metadata_json"])
            metadata["page_input"] = {
                "schema_version": "paper-page-input-v1",
                "input_type": "paper",
                "type": "模仿朗读",
                "questions": [{"listening_text": "Partial page fact"}],
            }
            con.execute(
                "UPDATE work_items SET metadata_json=? WHERE item_id=?",
                (json.dumps(metadata, ensure_ascii=False), self.item_ids[0]),
            )

        capabilities = {
            "system_input": {
                "available": True,
                "executor_available": True,
                "supported_external_input_types": ["paper"],
            },
        }
        workspace = self.repository.get_workspace(
            self.workflow.workflow_id,
            capabilities=capabilities,
        )
        page_status = workspace["system_input"]["page_content_status"]
        self.assertEqual(page_status["status"], "incomplete")
        projected_segment = next(
            segment
            for segment in workspace["system_input"]["content_segments"]
            if segment["item_id"] == self.item_ids[0]
        )
        self.assertEqual(
            projected_segment["page_input"]["questions"][0]["listening_text"],
            "Partial page fact",
        )
        start_action = next(
            action for action in workspace["available_actions"]
            if action["type"] == "START_INPUT"
        )
        self.assertFalse(start_action["enabled"])
        self.assertIn("页面内容不完整", start_action["reason"])

        with self.assertRaises(SystemInputError) as error:
            self.service.start_input(
                self.workflow.workflow_id,
                accepted["snapshot"]["state_version"],
                idempotency_key="incomplete-page-facts",
            )
        self.assertEqual(error.exception.code, "SYSTEM_INPUT_CONTENT_INCOMPLETE")
        with self.database.read_transaction() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM input_runs WHERE workflow_id=?",
                    (self.workflow.workflow_id,),
                ).fetchone()[0],
                0,
            )

    def test_special_topic_supported_document_also_requires_complete_page_facts(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT metadata_json FROM work_items WHERE item_id=?",
                (self.item_ids[0],),
            ).fetchone()
            metadata = json.loads(row["metadata_json"])
            metadata["page_input"] = {
                "schema_version": "paper-page-input-v1",
                "input_type": "paper",
                "type": "模仿朗读",
                "questions": [{"listening_text": "Partial special-topic fact"}],
            }
            con.execute(
                "UPDATE work_items SET metadata_json=? WHERE item_id=?",
                (json.dumps(metadata, ensure_ascii=False), self.item_ids[0]),
            )

        capabilities = {
            "system_input": {
                "available": True,
                "executor_available": True,
                "supported_external_input_types": ["paper"],
            },
        }
        workspace = self.repository.get_workspace(
            self.workflow.workflow_id,
            capabilities=capabilities,
        )
        self.assertEqual(
            workspace["system_input"]["page_content_status"]["status"],
            "incomplete",
        )
        start_action = next(
            action for action in workspace["available_actions"]
            if action["type"] == "START_INPUT"
        )
        self.assertFalse(start_action["enabled"])

        with self.assertRaises(SystemInputError) as error:
            self.service.start_input(
                self.workflow.workflow_id,
                accepted["snapshot"]["state_version"],
                idempotency_key="special-topic-page-facts",
            )
        self.assertEqual(error.exception.code, "SYSTEM_INPUT_CONTENT_INCOMPLETE")
        with self.database.read_transaction() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM input_runs WHERE workflow_id=?",
                    (self.workflow.workflow_id,),
                ).fetchone()[0],
                0,
            )

    def test_legacy_imitation_profile_closes_workspace_and_server_entry_gate(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        with self.database.transaction() as con:
            for item_id in self.item_ids:
                row = con.execute(
                    "SELECT metadata_json FROM work_items WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                metadata = json.loads(row["metadata_json"])
                metadata["major_section_profile"] = "imitation_legacy_unit_source"
                metadata["entry_profile"] = None
                metadata["capabilities"] = {
                    "parse": True,
                    "audio": True,
                    "normalize": True,
                    "external_input": False,
                }
                con.execute(
                    "UPDATE work_items SET metadata_json=? WHERE item_id=?",
                    (json.dumps(metadata, ensure_ascii=False), item_id),
                )

        capabilities = {
            "system_input": {
                "available": True,
                "executor_available": True,
                "supported_external_input_types": ["paper"],
            },
        }
        workspace = self.repository.get_workspace(
            self.workflow.workflow_id,
            capabilities=capabilities,
        )
        support = workspace["system_input"]["document_entry_support"]
        self.assertFalse(support["supported"])
        self.assertEqual(support["entry_profile_invalid_count"], 2)
        self.assertEqual(support["entry_capability_invalid_count"], 2)
        start_action = next(
            action for action in workspace["available_actions"]
            if action["type"] == "START_INPUT"
        )
        self.assertFalse(start_action["enabled"])
        self.assertIn("画像", start_action["reason"])

        with self.assertRaises(SystemInputError) as error:
            self.service.start_input(
                self.workflow.workflow_id,
                accepted["snapshot"]["state_version"],
                idempotency_key="legacy-imitation-entry-closed",
            )
        self.assertEqual(error.exception.code, "SYSTEM_INPUT_DOCUMENT_UNSUPPORTED")
        with self.database.read_transaction() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM input_runs WHERE workflow_id=?",
                    (self.workflow.workflow_id,),
                ).fetchone()[0],
                0,
            )

    def test_execute_input_run_rechecks_current_document_gate_before_external_side_effect(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="execute-document-gate-check",
        )

        with self.database.transaction() as con:
            for item_id in self.item_ids:
                row = con.execute(
                    "SELECT metadata_json FROM work_items WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                metadata = json.loads(row["metadata_json"])
                metadata["major_section_profile"] = "imitation_legacy_unit_source"
                metadata["entry_profile"] = None
                metadata["capabilities"] = {
                    "parse": True,
                    "audio": True,
                    "normalize": True,
                    "external_input": False,
                }
                con.execute(
                    "UPDATE work_items SET metadata_json=? WHERE item_id=?",
                    (json.dumps(metadata, ensure_ascii=False), item_id),
                )

        calls: list[dict[str, object]] = []
        self.service.executor = lambda payload: calls.append(payload) or {
            "external_record_id": "must-not-submit",
        }
        result = self.service.execute_input_run(started["input_run_id"])

        self.assertEqual(result["input_status"], "failed_retryable")
        self.assertEqual(calls, [])
        with self.database.read_transaction() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM external_operations WHERE workflow_id=?",
                    (self.workflow.workflow_id,),
                ).fetchone()[0],
                0,
            )
            attempts = con.execute(
                "SELECT status, side_effect_state, error_code FROM input_attempts WHERE input_run_id=? ORDER BY entry_id",
                (started["input_run_id"],),
            ).fetchall()
        self.assertEqual(
            [(row["status"], row["side_effect_state"], row["error_code"]) for row in attempts],
            [
                ("FAILED", "NOT_STARTED", "SYSTEM_INPUT_DOCUMENT_UNSUPPORTED"),
                ("FAILED", "NOT_STARTED", "SYSTEM_INPUT_DOCUMENT_UNSUPPORTED"),
            ],
        )

    def test_execute_input_run_rejects_pre_gate_run_snapshot(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="execute-snapshot-gate-check",
        )

        with self.database.transaction() as con:
            target = json.loads(
                con.execute(
                    "SELECT target_snapshot_json FROM input_runs WHERE input_run_id=?",
                    (started["input_run_id"],),
                ).fetchone()[0]
            )
            target.pop("document_entry_support", None)
            con.execute(
                "UPDATE input_runs SET target_snapshot_json=? WHERE input_run_id=?",
                (json.dumps(target, ensure_ascii=False), started["input_run_id"]),
            )

        calls: list[dict[str, object]] = []
        self.service.executor = lambda payload: calls.append(payload) or {
            "external_record_id": "must-not-submit",
        }
        result = self.service.execute_input_run(started["input_run_id"])

        self.assertEqual(result["input_status"], "failed_retryable")
        self.assertEqual(calls, [])
        with self.database.read_transaction() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM external_operations WHERE workflow_id=?",
                    (self.workflow.workflow_id,),
                ).fetchone()[0],
                0,
            )

    def test_configuration_rejects_content_and_template_facts(self) -> None:
        with self.assertRaises(SystemInputError) as config_error:
            self.service.save_configuration(
                self.workflow.workflow_id,
                self.workflow.state_version,
                {"input_type": "paper", "units": [{"unit_id": "x", "score": 7}]},
            )
        self.assertEqual(config_error.exception.code, "SYSTEM_INPUT_CONFIG_FORBIDDEN_FIELD")

        with self.assertRaises(SystemInputError) as template_error:
            self.service.create_template(
                "paper",
                "不应保存内容",
                {"paperCategory": "题型专项", "audioArtifactId": "audio-1"},
            )
        self.assertEqual(template_error.exception.code, "SYSTEM_INPUT_CONFIG_FORBIDDEN_FIELD")

    def test_application_template_keeps_only_reusable_configuration(self) -> None:
        template = self.service.create_template(
            "paper",
            "广东初中模仿朗读",
            {
                "paperCategory": "题型专项",
                "provinceId": {"id": 440000, "name": "广东省"},
                "cityId": {"id": 440600, "name": "佛山市"},
                "platformTemplateName": "模仿朗读",
            },
            platform_template_name="模仿朗读",
            platform_template_version="page-v1",
        )
        self.assertEqual(template["input_type"], "paper")
        self.assertEqual(template["platform_template_name"], "模仿朗读")
        self.assertEqual(template["configuration"]["paperCategory"], "题型专项")
        self.assertNotIn("units", template["configuration"])
        self.assertNotIn("paperName", template["configuration"])

    def test_platform_template_catalog_supports_local_crud_without_platform_side_effects(self) -> None:
        created = self.service.create_platform_template(
            "paper",
            "模仿朗读",
        )
        self.assertTrue(created["platform_template_key"].startswith("platform-template_"))
        self.assertIsNone(created["platform_template_id"])
        self.assertIsNone(created["platform_template_version"])
        self.assertEqual(self.service.list_platform_templates("paper"), [created])

        updated = self.service.update_platform_template(
            created["platform_template_key"],
            {
                "name": "模仿朗读专项",
                "platform_template_version": "v2",
            },
        )
        self.assertEqual(updated["name"], "模仿朗读专项")
        self.assertEqual(updated["platform_template_version"], "v2")

        archived = self.service.delete_platform_template(created["platform_template_key"])
        self.assertIsNotNone(archived["archived_at"])
        self.assertEqual(self.service.list_platform_templates("paper"), [])
        self.assertEqual(
            self.service.delete_platform_template(created["platform_template_key"]),
            archived,
        )

    def test_audio_acceptance_and_page_executor_run_are_fenced_and_idempotent(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self.assertEqual(saved["projection"]["input_status"], "pending_execute")
        self._finish_audio_workflow()

        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        accepted_version = accepted["snapshot"]["state_version"]
        self.assertEqual(accepted["projection"]["audio_acceptance"]["status"], "accepted")

        calls: list[dict[str, object]] = []

        def page_executor(payload: dict[str, object]) -> dict[str, object]:
            calls.append(payload)
            target = payload["target"]
            self.assertIsInstance(target, dict)
            self.assertNotIn("audio_path", json.dumps(payload, ensure_ascii=False))
            self.assertNotIn("original_audio_path", json.dumps(payload, ensure_ascii=False))
            unit = payload["unit"]
            self.assertIsInstance(unit, dict)
            return {
                "external_record_id": f"paper-{unit['unit_id']}",
                "feedback": {"status": "saved"},
            }

        self.service.executor = page_executor
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted_version,
            idempotency_key="system-input-run-key-0001",
        )
        run_id = started["input_run_id"]
        finished = self.service.execute_input_run(run_id)
        self.assertEqual(finished["input_status"], "succeeded")
        self.assertFalse(finished["configuration_editable"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(entry["external_record_id"] for entry in finished["entries"]))

        with self.database.read_transaction() as con:
            attempt_rows = con.execute(
                "SELECT status, side_effect_state, external_operation_id FROM input_attempts WHERE input_run_id=? ORDER BY entry_id",
                (run_id,),
            ).fetchall()
            operation_rows = con.execute(
                "SELECT side_effect_state FROM external_operations ORDER BY external_operation_id",
            ).fetchall()
        self.assertEqual([row["status"] for row in attempt_rows], ["SUCCEEDED", "SUCCEEDED"])
        self.assertEqual([row["side_effect_state"] for row in attempt_rows], ["CONFIRMED", "CONFIRMED"])
        self.assertEqual([row["side_effect_state"] for row in operation_rows], ["CONFIRMED", "CONFIRMED"])
        self.assertEqual(self.service.execute_input_run(run_id)["input_status"], "succeeded")
        self.assertEqual(len(calls), 2, "confirmed operations must replay without opening the page")

    def test_ambiguous_page_write_can_be_resolved_as_not_submitted_and_retried(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        first_unit_id = self.projection["units"][0]["unit_id"]

        def executor(payload: dict[str, object]) -> dict[str, object]:
            unit = payload["unit"]
            if isinstance(unit, dict) and unit.get("unit_id") == first_unit_id:
                raise SystemInputError("页面录入执行失败", code="INPUT_EXECUTOR_FAILED")
            return {"external_record_id": f"paper-{unit['unit_id']}"}

        self.service.executor = executor
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-ambiguous-not-submitted",
        )
        run_id = started["input_run_id"]
        pending = self.service.execute_input_run(run_id)
        self.assertEqual(pending["input_status"], "needs_reconcile")

        with self.database.read_transaction() as con:
            attempt = con.execute(
                "SELECT attempt_id, entry_id, external_operation_id FROM input_attempts WHERE input_run_id=? AND status='AMBIGUOUS'",
                (run_id,),
            ).fetchone()
        self.assertIsNotNone(attempt)
        resolved = self.service.resolve_ambiguous_external_operation(
            self.workflow.workflow_id,
            run_id,
            str(attempt["attempt_id"]),
            "NOT_SUBMITTED",
            evidence={"source": "test", "summary": "platform record was not created"},
            resolved_by="test",
        )
        entry = next(item for item in resolved["entries"] if item["entry_id"] == attempt["entry_id"])
        self.assertEqual(entry["input_status"], "failed_retryable")
        self.assertFalse(entry["requires_reconcile"])
        self.assertIsNone(entry["external_record_id"])

        with self.database.read_transaction() as con:
            operation = con.execute(
                "SELECT side_effect_state FROM external_operations WHERE external_operation_id=?",
                (attempt["external_operation_id"],),
            ).fetchone()
            attempt_state = con.execute(
                "SELECT status, side_effect_state, error_code FROM input_attempts WHERE attempt_id=?",
                (attempt["attempt_id"],),
            ).fetchone()
        self.assertEqual(operation["side_effect_state"], "REJECTED")
        self.assertEqual(
            (attempt_state["status"], attempt_state["side_effect_state"], attempt_state["error_code"]),
            ("FAILED", "REJECTED", "INPUT_EXTERNAL_NOT_SUBMITTED"),
        )

        # Replaying the same decision is idempotent and does not create a new
        # external operation before the user explicitly starts a retry.
        replay = self.service.resolve_ambiguous_external_operation(
            self.workflow.workflow_id,
            run_id,
            str(attempt["attempt_id"]),
            "NOT_SUBMITTED",
            evidence={"source": "test", "summary": "platform record was not created"},
            resolved_by="test",
        )
        self.assertEqual(replay["input_status"], "failed_retryable")

        self.service.executor = lambda payload: {
            "external_record_id": f"retry-{payload['unit']['unit_id']}",
        }
        retried = self.service.start_input(
            self.workflow.workflow_id,
            self.repository.get_workflow(self.workflow.workflow_id).state_version,
            idempotency_key="system-input-ambiguous-not-submitted-retry",
        )
        self.assertEqual(retried["input_run_id"], run_id)
        finished = self.service.execute_input_run(run_id)
        self.assertEqual(finished["input_status"], "succeeded")

    def test_ambiguous_page_write_can_bind_observed_record_without_reopening_page(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        first_unit_id = self.projection["units"][0]["unit_id"]

        def executor(payload: dict[str, object]) -> dict[str, object]:
            unit = payload["unit"]
            if isinstance(unit, dict) and unit.get("unit_id") == first_unit_id:
                raise SystemInputError("页面录入执行失败", code="INPUT_EXECUTOR_FAILED")
            return {"external_record_id": f"paper-{unit['unit_id']}"}

        self.service.executor = executor
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-ambiguous-confirmed",
        )
        run_id = started["input_run_id"]
        self.service.execute_input_run(run_id)
        with self.database.read_transaction() as con:
            attempt = con.execute(
                "SELECT attempt_id, entry_id, external_operation_id FROM input_attempts WHERE input_run_id=? AND status='AMBIGUOUS'",
                (run_id,),
            ).fetchone()
        self.assertIsNotNone(attempt)

        resolved = self.service.resolve_ambiguous_external_operation(
            self.workflow.workflow_id,
            run_id,
            str(attempt["attempt_id"]),
            "CONFIRMED",
            external_record_id="observed-paper-1",
            evidence={"source": "test", "summary": "platform record was found"},
            resolved_by="test",
        )
        entry = next(item for item in resolved["entries"] if item["entry_id"] == attempt["entry_id"])
        self.assertEqual(entry["input_status"], "failed_retryable")
        self.assertEqual(entry["external_record_id"], "observed-paper-1")
        self.assertFalse(entry["requires_reconcile"])

        calls = 0

        def must_not_reopen(_payload: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            raise AssertionError("confirmed external receipt must be replayed locally")

        self.service.executor = must_not_reopen
        retried = self.service.start_input(
            self.workflow.workflow_id,
            self.repository.get_workflow(self.workflow.workflow_id).state_version,
            idempotency_key="system-input-ambiguous-confirmed-retry",
        )
        self.assertEqual(retried["input_run_id"], run_id)
        finished = self.service.execute_input_run(run_id)
        self.assertEqual(finished["input_status"], "succeeded")
        self.assertEqual(calls, 0)

    def _run_to_ambiguous_state(self, idempotency_key: str) -> tuple[str, dict[str, object]]:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        first_unit_id = self.projection["units"][0]["unit_id"]

        def executor(payload: dict[str, object]) -> dict[str, object]:
            unit = payload["unit"]
            if isinstance(unit, dict) and unit.get("unit_id") == first_unit_id:
                raise SystemInputError("页面录入执行失败", code="INPUT_EXECUTOR_FAILED")
            return {"external_record_id": f"paper-{unit['unit_id']}"}

        self.service.executor = executor
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key=idempotency_key,
        )
        self.service.execute_input_run(started["input_run_id"])
        with self.database.read_transaction() as con:
            attempt = con.execute(
                "SELECT attempt_id, entry_id FROM input_attempts WHERE input_run_id=? AND status='AMBIGUOUS'",
                (started["input_run_id"],),
            ).fetchone()
        self.assertIsNotNone(attempt)
        return started["input_run_id"], dict(attempt)

    def test_readonly_verification_binds_single_platform_record(self) -> None:
        run_id, attempt = self._run_to_ambiguous_state("system-input-verify-found")

        class VerifyExecutor:
            def __init__(self) -> None:
                self.verify_payloads: list[dict[str, object]] = []

            def __call__(self, payload: dict[str, object]) -> dict[str, object]:
                raise AssertionError("verification must never execute a page write")

            def verify(self, payload: dict[str, object]) -> dict[str, object]:
                self.verify_payloads.append(payload)
                return {
                    "status": "FOUND",
                    "side_effect_policy": "PAGE_UI_READ_ONLY",
                    "matches": [{"external_record_id": "observed-paper-9", "status": "已发布"}],
                }

        executor = VerifyExecutor()
        self.service.executor = executor
        projection = self.service.verify_ambiguous_external_record(
            self.workflow.workflow_id,
            run_id,
            str(attempt["attempt_id"]),
            resolved_by="test",
        )
        self.assertEqual(projection["input_status"], "failed_retryable")
        entry = next(item for item in projection["entries"] if item["entry_id"] == attempt["entry_id"])
        self.assertEqual(entry["external_record_id"], "observed-paper-9")
        self.assertFalse(entry["requires_reconcile"])
        self.assertEqual(len(executor.verify_payloads), 1)
        self.assertEqual(executor.verify_payloads[0]["paper_title"], "外研9上-U6-第1套")
        self.assertEqual(executor.verify_payloads[0]["target"]["input_type"], "paper")

        with self.database.read_transaction() as con:
            operation_state = con.execute(
                """SELECT eo.side_effect_state FROM external_operations eo
                   JOIN input_attempts ia ON ia.external_operation_id=eo.external_operation_id
                   WHERE ia.attempt_id=?""",
                (attempt["attempt_id"],),
            ).fetchone()
        self.assertEqual(operation_state["side_effect_state"], "CONFIRMED")

    def test_readonly_verification_without_matches_opens_safe_retry(self) -> None:
        run_id, attempt = self._run_to_ambiguous_state("system-input-verify-missing")

        class VerifyExecutor:
            def __call__(self, payload: dict[str, object]) -> dict[str, object]:
                raise AssertionError("verification must never execute a page write")

            def verify(self, payload: dict[str, object]) -> dict[str, object]:
                return {"status": "NOT_FOUND", "matches": []}

        self.service.executor = VerifyExecutor()
        projection = self.service.verify_ambiguous_external_record(
            self.workflow.workflow_id,
            run_id,
            str(attempt["attempt_id"]),
            resolved_by="test",
        )
        self.assertEqual(projection["input_status"], "failed_retryable")
        entry = next(item for item in projection["entries"] if item["entry_id"] == attempt["entry_id"])
        self.assertIsNone(entry["external_record_id"])
        self.assertFalse(entry["requires_reconcile"])

    def test_readonly_verification_with_duplicate_titles_fails_closed(self) -> None:
        run_id, attempt = self._run_to_ambiguous_state("system-input-verify-duplicate")

        class VerifyExecutor:
            def __call__(self, payload: dict[str, object]) -> dict[str, object]:
                raise AssertionError("verification must never execute a page write")

            def verify(self, payload: dict[str, object]) -> dict[str, object]:
                return {
                    "status": "MULTIPLE_MATCHES",
                    "matches": [
                        {"external_record_id": "paper-a", "status": "已发布"},
                        {"external_record_id": "paper-b", "status": "已下架"},
                    ],
                }

        self.service.executor = VerifyExecutor()
        projection = self.service.verify_ambiguous_external_record(
            self.workflow.workflow_id,
            run_id,
            str(attempt["attempt_id"]),
            resolved_by="test",
        )
        # Nothing is guessed: the run stays protected and the concrete
        # problem is recorded on the attempt evidence for the renderer.
        self.assertEqual(projection["input_status"], "needs_reconcile")
        ambiguous = next(
            item for item in projection["input_run"]["attempts"]
            if item["status"] == "AMBIGUOUS"
        )
        outcome = ambiguous["evidence"]["external_record_verification"]
        self.assertEqual(outcome["status"], "needs_manual_resolution")
        self.assertEqual(len(outcome["matches"]), 2)

    def test_readonly_verification_records_executor_failure_for_retry(self) -> None:
        run_id, attempt = self._run_to_ambiguous_state("system-input-verify-failed")

        class VerifyExecutor:
            def __call__(self, payload: dict[str, object]) -> dict[str, object]:
                raise AssertionError("verification must never execute a page write")

            def verify(self, payload: dict[str, object]) -> dict[str, object]:
                raise SystemInputError("浏览器无法打开", code="INPUT_VERIFY_FAILED")

        self.service.executor = VerifyExecutor()
        with self.assertRaises(SystemInputError):
            self.service.verify_ambiguous_external_record(
                self.workflow.workflow_id,
                run_id,
                str(attempt["attempt_id"]),
                resolved_by="test",
            )
        projection = self.service.get_projection(self.workflow.workflow_id)
        ambiguous = next(
            item for item in projection["input_run"]["attempts"]
            if item["status"] == "AMBIGUOUS"
        )
        outcome = ambiguous["evidence"]["external_record_verification"]
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["error_code"], "INPUT_VERIFY_FAILED")
        self.assertEqual(projection["input_status"], "needs_reconcile")

    def test_readonly_verification_normalizes_invalid_executor_output(self) -> None:
        run_id, attempt = self._run_to_ambiguous_state("system-input-verify-invalid-output")

        class InvalidVerifyExecutor:
            def verify(self, payload: dict[str, object]) -> object:
                return ["not", "a", "mapping"]

        self.service.executor = InvalidVerifyExecutor()
        with self.assertRaises(SystemInputError) as error:
            self.service.verify_ambiguous_external_record(
                self.workflow.workflow_id,
                run_id,
                str(attempt["attempt_id"]),
                resolved_by="test",
            )
        self.assertEqual(error.exception.code, "INPUT_EXECUTOR_INVALID_RESULT")
        projection = self.service.get_projection(self.workflow.workflow_id)
        ambiguous = next(
            item for item in projection["input_run"]["attempts"]
            if item["status"] == "AMBIGUOUS"
        )
        outcome = ambiguous["evidence"]["external_record_verification"]
        self.assertEqual(outcome["error_code"], "INPUT_EXECUTOR_INVALID_RESULT")

    def test_readonly_verification_rejects_malformed_matches(self) -> None:
        run_id, attempt = self._run_to_ambiguous_state("system-input-verify-malformed-matches")

        class InvalidVerifyExecutor:
            def verify(self, payload: dict[str, object]) -> dict[str, object]:
                return {"status": "NOT_FOUND", "matches": ["not-a-record"]}

        self.service.executor = InvalidVerifyExecutor()
        with self.assertRaises(SystemInputError) as error:
            self.service.verify_ambiguous_external_record(
                self.workflow.workflow_id,
                run_id,
                str(attempt["attempt_id"]),
                resolved_by="test",
            )
        self.assertEqual(error.exception.code, "INPUT_EXECUTOR_INVALID_RESULT")
        projection = self.service.get_projection(self.workflow.workflow_id)
        ambiguous = next(
            item for item in projection["input_run"]["attempts"]
            if item["status"] == "AMBIGUOUS"
        )
        self.assertEqual(
            ambiguous["evidence"]["external_record_verification"]["error_code"],
            "INPUT_EXECUTOR_INVALID_RESULT",
        )

    def test_automatic_readonly_verification_does_not_reopen_after_stop(self) -> None:
        run_id, attempt = self._run_to_ambiguous_state("system-input-verify-stopped")
        self.service.finalize_stopped_input_run(
            run_id,
            reason="测试停止自动核验",
            requested_by="test",
        )

        calls = {"count": 0}

        class VerifyExecutor:
            def verify(self, payload: dict[str, object]) -> dict[str, object]:
                calls["count"] += 1
                return {"status": "NOT_FOUND", "matches": []}

        self.service.executor = VerifyExecutor()
        with self.assertRaises(SystemInputError) as error:
            self.service.verify_ambiguous_external_record(
                self.workflow.workflow_id,
                run_id,
                str(attempt["attempt_id"]),
                automatic=True,
                resolved_by="test",
            )
        self.assertEqual(error.exception.code, "INPUT_RUN_STOPPED")
        self.assertEqual(calls["count"], 0)

    def test_page_verification_checks_stop_after_profile_lock(self) -> None:
        executor = PlatformInputWorkflowPageExecutor(
            self.repository,
            self.artifacts,
            profile_dir=Path(self.temp.name) / "profile",
            admin_url="https://platform.example/admin",
            api_base="https://platform.example/api",
            login_timeout=1,
        )
        checks = iter((False, True))
        with patch("workflow.system_input_executor.page_input.verify_live") as verify_live:
            with self.assertRaises(SystemInputError) as error:
                executor.verify_paper({
                    "workflow_id": self.workflow.workflow_id,
                    "paper_title": "测试试卷",
                    "_abort_check": lambda: next(checks),
                })
        self.assertEqual(error.exception.code, "INPUT_RUN_STOPPED")
        verify_live.assert_not_called()

    def test_readonly_verification_resolves_textbook_title_and_reports_missing_id(self) -> None:
        run_id, attempt = self._run_to_ambiguous_state("system-input-verify-textbook")
        with self.database.transaction() as con:
            con.execute(
                "UPDATE input_units SET input_type='textbook' WHERE unit_id=?",
                (self.projection["units"][0]["unit_id"],),
            )
            con.execute(
                "UPDATE input_entries SET configuration_json=? WHERE entry_id=?",
                (json.dumps({"textbookNameZh": "Section A"}, ensure_ascii=False), attempt["entry_id"]),
            )

        class TextbookVerifyExecutor:
            def __init__(self) -> None:
                self.payloads: list[dict[str, object]] = []

            def __call__(self, payload: dict[str, object]) -> dict[str, object]:
                raise AssertionError("verification must never execute a page write")

            def verify(self, payload: dict[str, object]) -> dict[str, object]:
                self.payloads.append(payload)
                # The textbook list view reports existence only: no record id.
                return {"status": "FOUND", "matches": [{"external_record_id": None, "status": "FOUND"}]}

        executor = TextbookVerifyExecutor()
        self.service.executor = executor
        projection = self.service.verify_ambiguous_external_record(
            self.workflow.workflow_id,
            run_id,
            str(attempt["attempt_id"]),
            resolved_by="test",
        )
        self.assertEqual(executor.payloads[0]["paper_title"], "Section A")
        self.assertEqual(executor.payloads[0]["target"]["input_type"], "textbook")
        self.assertEqual(projection["input_status"], "needs_reconcile")
        ambiguous = next(
            item for item in projection["input_run"]["attempts"]
            if item["status"] == "AMBIGUOUS"
        )
        outcome = ambiguous["evidence"]["external_record_verification"]
        self.assertEqual(outcome["status"], "needs_manual_resolution")
        # The textbook hit must not be reported as duplicate titles.
        self.assertIn("没有读到记录 ID", outcome["message"])

    def test_readonly_verification_records_failed_outcome_for_nameless_unit(self) -> None:
        run_id, attempt = self._run_to_ambiguous_state("system-input-verify-nameless")
        with self.database.transaction() as con:
            con.execute(
                "UPDATE input_entries SET configuration_json='{}' WHERE entry_id=?",
                (attempt["entry_id"],),
            )
            con.execute(
                "UPDATE input_units SET configuration_json='{}' WHERE unit_id=?",
                (self.projection["units"][0]["unit_id"],),
            )
            # The run target snapshot is the last fallback for the lookup
            # title; a truly nameless unit blanks it as well.
            con.execute(
                "UPDATE input_runs SET target_snapshot_json=? WHERE input_run_id=?",
                (json.dumps({"input_type": "paper", "units": []}, ensure_ascii=False), run_id),
            )

        class VerifyExecutor:
            def __call__(self, payload: dict[str, object]) -> dict[str, object]:
                raise AssertionError("verification must never execute a page write")

            def verify(self, payload: dict[str, object]) -> dict[str, object]:
                raise AssertionError("a nameless unit must fail before the executor runs")

        self.service.executor = VerifyExecutor()
        with self.assertRaises(SystemInputError) as error:
            self.service.verify_ambiguous_external_record(
                self.workflow.workflow_id,
                run_id,
                str(attempt["attempt_id"]),
                resolved_by="test",
            )
        self.assertEqual(error.exception.code, "SYSTEM_INPUT_CONFIG_INCOMPLETE")
        projection = self.service.get_projection(self.workflow.workflow_id)
        ambiguous = next(
            item for item in projection["input_run"]["attempts"]
            if item["status"] == "AMBIGUOUS"
        )
        outcome = ambiguous["evidence"]["external_record_verification"]
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["error_code"], "SYSTEM_INPUT_CONFIG_INCOMPLETE")

    def test_browser_close_stops_run_and_marks_user_stopped(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        first_unit_id = self.projection["units"][0]["unit_id"]
        calls = {"count": 0}

        def executor(payload: dict[str, object]) -> dict[str, object]:
            calls["count"] += 1
            unit = payload.get("unit")
            if isinstance(unit, dict) and unit.get("unit_id") == first_unit_id:
                raise RuntimeError("Target page, context or browser has been closed")
            return {"external_record_id": f"paper-{unit['unit_id']}"}

        self.service.executor = executor
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-browser-close",
        )
        run_id = started["input_run_id"]
        self.service.execute_input_run(run_id)
        # Closing the browser is an explicit stop: the second unit must not
        # be launched in a fresh browser window.
        self.assertEqual(calls["count"], 1)
        projection = self.service.get_projection(self.workflow.workflow_id)
        self.assertEqual(projection["input_run"]["status"], "FAILED")
        self.assertEqual(projection["input_run"]["error_code"], "INPUT_RUN_STOPPED")
        self.assertEqual(projection["input_status"], "needs_reconcile")
        self.assertEqual(projection["input_run"]["control"], {"pause_requested": False, "stop_requested": False})
        # The stopped run is not auto-resumed by the recovery dispatcher.
        self.assertEqual(self.service.list_recoverable_run_ids(), [])

        # Replaying the original start command must remain idempotent even
        # after the stop marker is terminal.  It may return the old projection,
        # but it must not reopen the browser or process the next unit.
        replay = self.service.start_input(
            self.workflow.workflow_id,
            self.repository.get_workflow(self.workflow.workflow_id).state_version,
            idempotency_key="system-input-browser-close",
        )
        self.assertTrue(replay["replayed"])
        self.service.execute_input_run(run_id)
        self.assertEqual(calls["count"], 1)

    def test_wrapped_browser_close_error_is_detected_from_details_and_cause(self) -> None:
        wrapped = SystemInputError(
            "页面录入执行失败",
            code="INPUT_EXECUTOR_FAILED",
            details={"error_message": "Target page, context or browser has been closed"},
        )
        self.assertTrue(_is_browser_closed_error(wrapped))

        try:
            try:
                raise RuntimeError("browser disconnected")
            except RuntimeError as cause:
                raise SystemInputError("页面录入执行失败", code="INPUT_EXECUTOR_FAILED") from cause
        except SystemInputError as error:
            self.assertTrue(_is_browser_closed_error(error))

    def test_browser_close_during_readonly_verification_persists_stop_fence(self) -> None:
        run_id, attempt = self._run_to_ambiguous_state("system-input-verify-browser-close")

        class VerifyExecutor:
            def verify(self, payload: dict[str, object]) -> dict[str, object]:
                raise RuntimeError("Target page, context or browser has been closed")

        self.service.executor = VerifyExecutor()
        # Simulate a process exit in the small gap after the browser-close
        # error but before the normal terminal finalizer transaction.
        with patch.object(
            self.service,
            "finalize_stopped_input_run",
            side_effect=RuntimeError("finalizer interrupted"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.verify_ambiguous_external_record(
                    self.workflow.workflow_id,
                    run_id,
                    str(attempt["attempt_id"]),
                    automatic=True,
                    resolved_by="test",
                )

        with self.database.read_transaction() as con:
            run = con.execute(
                "SELECT status, error_code FROM input_runs WHERE input_run_id=?",
                (run_id,),
            ).fetchone()
        self.assertEqual(run["status"], "AMBIGUOUS")
        self.assertEqual(run["error_code"], "INPUT_BROWSER_CLOSED")
        self.assertEqual(self.service.list_browser_closed_run_ids(), [run_id])

        # A direct/replayed worker invocation drains a terminal close marker;
        # it must not invoke the executor or open another browser.
        projection = self.service.execute_input_run(run_id)
        self.assertEqual(projection["input_run"]["status"], "FAILED")
        self.assertEqual(projection["input_run"]["error_code"], "INPUT_RUN_STOPPED")

    def test_restart_recovery_ignores_and_finishes_close_marked_run(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-close-marker-restart",
        )
        with self.database.transaction() as con:
            con.execute(
                "UPDATE input_runs SET status='RUNNING', error_code='INPUT_BROWSER_CLOSED' WHERE input_run_id=?",
                (started["input_run_id"],),
            )

        self.assertEqual(self.service.list_recoverable_run_ids(), [])
        self.assertEqual(
            self.service.list_browser_closed_run_ids(),
            [started["input_run_id"]],
        )
        # A direct/replayed worker invocation must also drain the marker when
        # the scheduler is unavailable; it must not enter page execution.
        projection = self.service.execute_input_run(started["input_run_id"])
        self.assertEqual(projection["input_run"]["status"], "FAILED")
        self.assertEqual(projection["input_run"]["error_code"], "INPUT_RUN_STOPPED")
        self.assertEqual(self.service.list_browser_closed_run_ids(), [])

    def test_browser_close_during_preflight_marks_terminal_failure_as_user_stopped(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )

        class PreflightBrowserClosedExecutor:
            def preflight(self, payload: dict[str, object]) -> None:
                raise RuntimeError("INPUT_BROWSER_CLOSED")

            def __call__(self, payload: dict[str, object]) -> dict[str, object]:
                raise AssertionError("a browser-close preflight failure must not cross the page-write boundary")

        self.service.executor = PreflightBrowserClosedExecutor()
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-browser-close-preflight",
        )
        # Reduce the fixture to one runnable attempt so the failed preflight
        # reaches the terminal FAILED roll-up before the stop finalizer runs.
        second_unit_id = self.projection["units"][1]["unit_id"]
        with self.database.transaction() as con:
            con.execute(
                """DELETE FROM input_attempts
                   WHERE input_run_id=?
                     AND entry_id IN (SELECT entry_id FROM input_entries WHERE unit_id=?)""",
                (started["input_run_id"], second_unit_id),
            )
        projection = self.service.execute_input_run(started["input_run_id"])
        self.assertEqual(projection["input_run"]["status"], "FAILED")
        self.assertEqual(projection["input_run"]["error_code"], "INPUT_RUN_STOPPED")
        self.assertEqual(self.service.list_recoverable_run_ids(), [])

    def test_stop_requested_during_preflight_does_not_cross_page_write_boundary(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        calls = {"count": 0}

        class StopDuringPreflightExecutor:
            def preflight(inner_self, payload: dict[str, object]) -> None:
                self.service.request_input_run_control(
                    self.workflow.workflow_id,
                    str(payload["input_run_id"]),
                    action="stop",
                    reason="预检期间停止",
                    requested_by="test",
                )

            def __call__(inner_self, payload: dict[str, object]) -> dict[str, object]:
                calls["count"] += 1
                raise AssertionError("预检期间停止后不能进入页面写入")

        self.service.executor = StopDuringPreflightExecutor()
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-stop-during-preflight",
        )
        projection = self.service.execute_input_run(started["input_run_id"])
        self.assertEqual(calls["count"], 0)
        self.assertEqual(projection["input_run"]["status"], "FAILED")
        self.assertEqual(projection["input_run"]["error_code"], "INPUT_RUN_STOPPED")
        self.assertEqual(self.service.list_recoverable_run_ids(), [])

    def test_automatic_verification_discards_result_when_stop_arrives_after_page_read(self) -> None:
        run_id, attempt = self._run_to_ambiguous_state("system-input-verify-stop-after-read")

        class StopAfterReadExecutor:
            def verify(inner_self, payload: dict[str, object]) -> dict[str, object]:
                self.service.finalize_stopped_input_run(
                    run_id,
                    reason="核验返回后停止",
                    requested_by="test",
                )
                return {"status": "NOT_FOUND", "matches": []}

        self.service.executor = StopAfterReadExecutor()
        with self.assertRaises(SystemInputError) as error:
            self.service.verify_ambiguous_external_record(
                self.workflow.workflow_id,
                run_id,
                str(attempt["attempt_id"]),
                automatic=True,
                resolved_by="test",
            )
        self.assertEqual(error.exception.code, "INPUT_RUN_STOPPED")
        projection = self.service.get_projection(self.workflow.workflow_id)
        self.assertEqual(projection["input_run"]["error_code"], "INPUT_RUN_STOPPED")
        self.assertEqual(projection["input_run"]["status"], "FAILED")

    def test_stop_control_checkpoint_skips_unattempted_units(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        calls = {"count": 0}

        def executor(payload: dict[str, object]) -> dict[str, object]:
            calls["count"] += 1
            return {"external_record_id": f"paper-{payload['unit']['unit_id']}"}

        self.service.executor = executor
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-stop-checkpoint",
        )
        control = self.service.request_input_run_control(
            self.workflow.workflow_id,
            started["input_run_id"],
            action="stop",
            reason="测试停止",
            requested_by="test",
        )
        self.assertTrue(control["control"]["stop_requested"])
        # A restart before the worker gets scheduled must still discover the
        # durable stop request and finalize it instead of leaving PENDING stuck.
        self.assertEqual(self.service.list_browser_closed_run_ids(), [started["input_run_id"]])
        runtime = type(
            "InputDispatchRuntime",
            (),
            {"system_input": self.service, "input_tasks_by_run": {}},
        )()
        self.assertEqual(asyncio.run(_dispatch_input_runs_once(runtime)), 0)
        projection = self.service.execute_input_run(started["input_run_id"])
        self.assertEqual(calls["count"], 0)
        self.assertEqual(projection["input_run"]["status"], "FAILED")
        self.assertEqual(projection["input_run"]["error_code"], "INPUT_RUN_STOPPED")
        self.assertEqual(self.service.list_recoverable_run_ids(), [])

    def test_stop_request_after_final_page_does_not_leave_stale_control(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-stop-after-final-page",
        )
        second_unit_id = self.projection["units"][1]["unit_id"]
        with self.database.transaction() as con:
            con.execute(
                """DELETE FROM input_attempts
                   WHERE input_run_id=?
                     AND entry_id IN (SELECT entry_id FROM input_entries WHERE unit_id=?)""",
                (started["input_run_id"], second_unit_id),
            )

        def executor(payload: dict[str, object]) -> dict[str, object]:
            self.service.request_input_run_control(
                self.workflow.workflow_id,
                str(payload["input_run_id"]),
                action="stop",
                reason="最后一个页面完成前收到停止请求",
                requested_by="test",
            )
            return {"external_record_id": f"paper-{payload['unit']['unit_id']}"}

        self.service.executor = executor
        projection = self.service.execute_input_run(started["input_run_id"])
        # The final page action is allowed to finish, but a stop request that
        # arrives during it must not be erased by the success roll-up. The
        # run is intentionally finalized as an explicit user stop.
        self.assertEqual(projection["input_run"]["status"], "FAILED")
        self.assertEqual(projection["input_run"]["error_code"], "INPUT_RUN_STOPPED")
        self.assertEqual(
            projection["input_run"]["control"],
            {"pause_requested": False, "stop_requested": False},
        )
        self.assertEqual(self.service.list_browser_closed_run_ids(), [])

    def test_page_executor_pause_checkpoint_waits_for_resume(self) -> None:
        import threading

        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        calls = {"count": 0}
        paused = threading.Event()
        resumed = threading.Event()

        class ControlAwareExecutor:
            accepts_input_run_control = True

            def __call__(inner_self, payload: dict[str, object]) -> dict[str, object]:
                calls["count"] += 1
                check = payload.get("_control_check")
                self.assertTrue(callable(check))
                if calls["count"] == 1:
                    def request_pause() -> None:
                        self.service.request_input_run_control(
                            self.workflow.workflow_id,
                            str(payload["input_run_id"]),
                            action="pause",
                            reason="页面检查点测试暂停",
                            requested_by="test",
                        )
                        paused.set()

                    pause_thread = threading.Thread(target=request_pause)
                    pause_thread.start()
                    self.assertTrue(paused.wait(timeout=2))

                    def request_resume() -> None:
                        self.service.request_input_run_control(
                            self.workflow.workflow_id,
                            str(payload["input_run_id"]),
                            action="resume",
                            reason="页面检查点测试恢复",
                            requested_by="test",
                        )
                        resumed.set()

                    resume_timer = threading.Timer(0.1, request_resume)
                    resume_timer.start()
                    try:
                        check()
                    finally:
                        pause_thread.join(timeout=2)
                        resume_timer.join(timeout=2)
                unit = payload["unit"]
                return {"external_record_id": f"paper-{unit['unit_id']}"}

        self.service.executor = ControlAwareExecutor()
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-page-pause-checkpoint",
        )
        projection = self.service.execute_input_run(started["input_run_id"])

        self.assertEqual(projection["input_status"], "succeeded")
        self.assertEqual(calls["count"], 2)
        self.assertTrue(resumed.is_set())
        self.assertEqual(
            projection["input_run"]["control"],
            {"pause_requested": False, "stop_requested": False},
        )

    def test_partial_success_clears_pause_control_after_terminal_failure(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-partial-pause-cleanup",
        )
        second_unit_id = self.projection["units"][1]["unit_id"]

        class PartialFailureExecutor:
            def preflight(inner_self, payload: dict[str, object]) -> None:
                if payload["unit"]["unit_id"] != second_unit_id:
                    return
                self.service.request_input_run_control(
                    self.workflow.workflow_id,
                    str(payload["input_run_id"]),
                    action="pause",
                    reason="终态前收到暂停请求",
                    requested_by="test",
                )
                raise RuntimeError("second unit preflight failed")

            def __call__(inner_self, payload: dict[str, object]) -> dict[str, object]:
                return {"external_record_id": f"paper-{payload['unit']['unit_id']}"}

        self.service.executor = PartialFailureExecutor()
        projection = self.service.execute_input_run(started["input_run_id"])
        self.assertEqual(projection["input_run"]["status"], "PARTIAL_SUCCESS")
        self.assertEqual(
            projection["input_run"]["control"],
            {"pause_requested": False, "stop_requested": False},
        )

    def test_pause_and_resume_control_flags_park_and_release(self) -> None:
        import threading

        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-pause-resume",
        )
        run_id = started["input_run_id"]
        paused = self.service.request_input_run_control(
            self.workflow.workflow_id,
            run_id,
            action="pause",
            requested_by="test",
        )
        self.assertTrue(paused["control"]["pause_requested"])
        self.assertFalse(paused["control"]["stop_requested"])
        # A paused run must stay parked across dispatcher/restart scans.  It
        # becomes recoverable only after an explicit resume command.
        self.assertEqual(self.service.list_recoverable_run_ids(), [])

        # The parked worker releases as soon as the pause flag is cleared.
        timer = threading.Timer(
            1.5,
            lambda: self.service.request_input_run_control(
                self.workflow.workflow_id,
                run_id,
                action="resume",
                requested_by="test",
            ),
        )
        timer.start()
        try:
            self.assertEqual(self.service._park_for_input_run_control(run_id)[0], "resume")
        finally:
            timer.join()
        control = self.service._input_run_control_now(run_id)
        self.assertFalse(control["pause_requested"])
        self.assertFalse(control["stop_requested"])
        self.assertEqual(self.service.list_recoverable_run_ids(), [run_id])

        def executor(payload: dict[str, object]) -> dict[str, object]:
            return {"external_record_id": f"paper-{payload['unit']['unit_id']}"}

        self.service.executor = executor
        projection = self.service.execute_input_run(run_id)
        self.assertEqual(projection["input_status"], "succeeded")

    def test_pause_checkpoint_returns_renewed_external_lease(self) -> None:
        import threading
        from types import SimpleNamespace

        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-pause-lease-renewal",
        )
        run_id = started["input_run_id"]
        self.service.request_input_run_control(
            self.workflow.workflow_id,
            run_id,
            action="pause",
            requested_by="test",
        )
        old_lease = SimpleNamespace(lease_id="old-lease")
        renewed_lease = SimpleNamespace(lease_id="renewed-lease")
        renew_calls: list[object] = []
        sleep_calls = {"count": 0}

        def renew(lease: object, *, ttl_seconds: int) -> object:
            renew_calls.append((lease, ttl_seconds))
            return renewed_lease

        def fake_sleep(_seconds: float) -> None:
            sleep_calls["count"] += 1
            if sleep_calls["count"] == 2:
                self.service.request_input_run_control(
                    self.workflow.workflow_id,
                    run_id,
                    action="resume",
                    requested_by="test",
                )

        monotonic_values = iter((0.0, 21.0, 21.0, 21.0))
        with patch.object(self.service.external, "renew_record_lease", side_effect=renew), \
            patch("workflow.system_input.time.sleep", side_effect=fake_sleep), \
            patch("workflow.system_input.time.monotonic", side_effect=lambda: next(monotonic_values)):
            signal, current_lease = self.service._park_for_input_run_control(
                run_id,
                lease=old_lease,
            )

        self.assertEqual(signal, "resume")
        self.assertIs(current_lease, renewed_lease)
        self.assertEqual(renew_calls, [(old_lease, 300)])

    def test_run_control_rejects_stale_state_version(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-control-state-version",
        )
        run_id = started["input_run_id"]
        current_state_version = self.repository.get_workflow(self.workflow.workflow_id).state_version

        with self.assertRaises(Exception) as error:
            self.service.request_input_run_control(
                self.workflow.workflow_id,
                run_id,
                action="stop",
                reason="过期页面停止请求",
                requested_by="test",
                expected_state_version=current_state_version + 1,
            )
        self.assertEqual(getattr(error.exception, "code", None), "STATE_CONFLICT")
        self.assertEqual(
            self.service._input_run_control_now(run_id),
            {"pause_requested": False, "stop_requested": False},
        )

        accepted_control = self.service.request_input_run_control(
            self.workflow.workflow_id,
            run_id,
            action="stop",
            reason="当前页面停止请求",
            requested_by="test",
            expected_state_version=current_state_version,
        )
        self.assertTrue(accepted_control["control"]["stop_requested"])

        with self.assertRaises(Exception) as resume_error:
            self.service.request_input_run_control(
                self.workflow.workflow_id,
                run_id,
                action="resume",
                reason="停止后的旧恢复请求",
                requested_by="test",
            )
        self.assertEqual(
            getattr(resume_error.exception, "code", None),
            "INPUT_RUN_CONTROL_STATE_CONFLICT",
        )



    def test_cross_workflow_start_idempotency_key_is_not_reused(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        self.service.executor = lambda _payload: {"external_record_id": "paper-one"}
        self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="shared-system-input-key",
        )

        other = self.repository.create_workflow("tts", {"input_type": "paper"})
        with self.assertRaises(Exception) as conflict:
            self.service.start_input(
                other.workflow_id,
                other.state_version,
                idempotency_key="shared-system-input-key",
            )
        self.assertEqual(getattr(conflict.exception, "code", None), "IDEMPOTENCY_CONFLICT")

    def test_page_executor_builds_imitation_payload_without_stem_or_stem_audio(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        unit = dict(saved["projection"]["units"][0])
        unit["segments"] = [
            segment
            for segment in saved["projection"]["content_segments"]
            if segment["unit_id"] == unit["unit_id"]
        ]
        executor = PlatformInputWorkflowPageExecutor(
            self.repository,
            self.artifacts,
            profile_dir=Path(self.temp.name) / "chrome-profile",
            admin_url="https://admin.example.test/#/resource/exam",
            api_base="https://api.example.test",
            login_timeout=1,
        )
        with tempfile.TemporaryDirectory(prefix="wordtts-page-payload-") as directory:
            raw = executor._build_raw_spec(
                {"workflow_id": self.workflow.workflow_id, "unit": unit},
                Path(directory),
            )

        self.assertEqual(raw["paper"]["title"], "外研9上-U6-第1套")
        self.assertEqual(raw["paper"]["districts"], [{"id": 440605, "name": "南海区"}])
        self.assertEqual(raw["question_groups"][0]["type"], "模仿朗读")
        question = raw["question_groups"][0]["questions"][0]
        self.assertEqual(question["listening_text"], "Reading passage 1.")
        self.assertTrue(question["audio_path"].endswith("segment-1.mp3"))
        self.assertNotIn("text", question)
        self.assertNotIn("stem", question)
        self.assertNotIn("reference_answers", question)

        unit["configuration"] = {
            **unit["configuration"],
            "paperName": "人教版七上-Starter Unit1-1",
        }
        with tempfile.TemporaryDirectory(prefix="wordtts-page-payload-space-") as directory:
            raw_with_space = executor._build_raw_spec(
                {"workflow_id": self.workflow.workflow_id, "unit": unit},
                Path(directory),
            )
        self.assertEqual(raw_with_space["paper"]["title"], "人教版七上-Starter Unit1-1")

        unit["configuration"] = {
            **unit["configuration"],
            "districtIds": [],
        }
        with tempfile.TemporaryDirectory(prefix="wordtts-page-payload-") as directory:
            raw_without_district = executor._build_raw_spec(
                {"workflow_id": self.workflow.workflow_id, "unit": unit},
                Path(directory),
            )
        self.assertEqual(raw_without_district["paper"]["districts"], [])

    def test_page_executor_filters_mixed_source_before_unrelated_artifacts(self) -> None:
        configuration = dict(self._configuration()["units"][0])
        configuration["paperName"] = "mixed-source-imitation"
        imitation_segment = next(
            segment
            for segment in self.projection["content_segments"]
            if segment["unit_id"] == self.projection["units"][0]["unit_id"]
        )
        unrelated_record = {
            "segment_id": "segment-unrelated-record",
            "item_id": "unrelated-record",
            "raw_text": "A record section that must not enter an imitation paper.",
            "tts_text": "A record section that must not enter an imitation paper.",
            "score": 8,
            "page_input": {
                "schema_version": "paper-page-input-v1",
                "input_type": "paper",
                "type": "听后记录并转述信息",
                "recording": {
                    "table_image_required": True,
                    "image_artifact_id": "missing-image",
                    "questions": [],
                },
            },
        }
        unit = {
            "unit_id": "unit-mixed-source",
            "unit_label": "第1套",
            "input_type": "paper",
            "configuration": configuration,
            "segments": [imitation_segment, unrelated_record],
        }
        executor = PlatformInputWorkflowPageExecutor(
            self.repository,
            self.artifacts,
            profile_dir=Path(self.temp.name) / "chrome-profile",
            admin_url="https://admin.example.test/#/resource/exam",
            api_base="https://api.example.test",
            login_timeout=1,
        )
        with tempfile.TemporaryDirectory(prefix="wordtts-page-payload-") as directory:
            raw = executor._build_raw_spec(
                {"workflow_id": self.workflow.workflow_id, "unit": unit},
                Path(directory),
            )

        self.assertEqual(
            [group["type"] for group in raw["question_groups"]],
            ["模仿朗读"],
        )
        self.assertEqual(raw["question_groups"][0]["questions"][0]["score"], 7)

    def test_info_retelling_special_materializes_both_instruction_audio_channels(self) -> None:
        configuration = {
            "paperName": "信息转述专项",
            "paperCategory": "题型专项",
            "provinceId": {"id": 440000, "name": "广东省"},
            "cityId": {"id": 440600, "name": "佛山市"},
            "districtIds": [],
            "stageId": {"id": 2, "name": "初中"},
            "gradeId": {"id": 7, "name": "七年级"},
            "year": 2026,
            "answerTimeMinutes": 20,
            "platformTemplateName": "信息转述及询问",
            "platformTemplateId": "2085639857241194496",
        }

        def add_audio(artifact_id: str) -> None:
            staged = self.artifacts.stage_stream(io.BytesIO(artifact_id.encode()))
            blob = self.artifacts.promote(staged, format="mp3")
            self.repository.attach_imported_artifact(
                self.workflow.workflow_id,
                artifact_id=artifact_id,
                blob=blob,
                artifact_type="tts-segment",
                producer="test",
                producer_version="1",
                item_id=self.item_ids[0],
            )

        for artifact_id in (
            "retelling-original",
            "retelling-instruction",
            "retelling-prompt",
            "asking-instruction",
        ):
            add_audio(artifact_id)

        page_input = {
            "schema_version": "paper-page-input-v1",
            "input_type": "paper",
            "type": "信息转述及询问",
            "recording": {
                "listening_text": "Emma introduces her plan.",
                "audio_filename_stem": "信息转述-1",
                "instruction_text": "Please retell the introduction.",
                "instruction_occurrence": 0,
                "instruction_audio_filename_stem": "信息转述题目指导文字-1",
                "asking_instruction_text": "Ask Emma two questions.",
                "asking_instruction_occurrence": 0,
                "asking_instruction_audio_filename_stem": "询问信息题干-1",
            },
            "retelling": {
                "prompt": "Let me tell you about Emma.",
                "prompt_audio_filename_stem": "信息转述题干-1",
                "score": 6,
                "reference_answers": ["Let me tell you about Emma."],
            },
            "asking": [
                {"prompt": "What is the plan?", "score": 1.5, "reference_answers": ["The plan is useful."]},
            ],
        }
        unit = {
            "unit_id": "unit-info-retelling",
            "unit_label": "第1套",
            "input_type": "paper",
            "configuration": configuration,
            "segments": [
                {
                    "segment_id": "segment-retelling-original",
                    "item_id": self.item_ids[0],
                    "raw_text": "Emma introduces her plan.",
                    "tts_text": "Emma introduces her plan.",
                    "score": 6,
                    "audio_artifact_id": "retelling-original",
                    "audio_filename_stem": "信息转述-1",
                    "page_input": page_input,
                },
                {
                    "segment_id": "segment-retelling-instruction",
                    "item_id": self.item_ids[0],
                    "tts_text": "Please retell the introduction.",
                    "audio_artifact_id": "retelling-instruction",
                    "audio_filename_stem": "信息转述题目指导文字-1",
                    "category": "信息转述题目指导文字",
                    "audio_only_auxiliary": True,
                },
                {
                    "segment_id": "segment-retelling-prompt",
                    "item_id": self.item_ids[0],
                    "tts_text": "Let me tell you about Emma.",
                    "audio_artifact_id": "retelling-prompt",
                    "audio_filename_stem": "信息转述题干-1",
                    "category": "信息转述题干",
                    "audio_only_auxiliary": True,
                },
                {
                    "segment_id": "segment-asking-instruction",
                    "item_id": self.item_ids[0],
                    "tts_text": "Ask Emma two questions.",
                    "audio_artifact_id": "asking-instruction",
                    "audio_filename_stem": "询问信息题干-1",
                    "category": "询问信息题干",
                    "audio_only_auxiliary": True,
                },
            ],
        }
        executor = PlatformInputWorkflowPageExecutor(
            self.repository,
            self.artifacts,
            profile_dir=Path(self.temp.name) / "chrome-profile",
            admin_url="https://admin.example.test/#/resource/exam",
            api_base="https://api.example.test",
            login_timeout=1,
        )
        with tempfile.TemporaryDirectory(prefix="wordtts-page-info-retelling-") as directory:
            raw = executor._build_raw_spec(
                {"workflow_id": self.workflow.workflow_id, "unit": unit},
                Path(directory),
            )

        group = raw["question_groups"][0]
        self.assertEqual(group["type"], "信息转述及询问")
        self.assertTrue(group["recording"]["asking_instruction_audio_path"].endswith("segment-4.mp3"))
        self.assertTrue(group["recording"]["instruction_audio_path"].endswith("segment-2.mp3"))
        self.assertTrue(group["retelling"]["prompt_audio_path"].endswith("segment-3.mp3"))

    def test_page_executor_keeps_materialized_table_image_complete(self) -> None:
        staged = self.artifacts.stage_stream(io.BytesIO(b"fake-png"))
        image_blob = self.artifacts.promote(staged, format="png")
        self.repository.attach_imported_artifact(
            self.workflow.workflow_id,
            artifact_id="record-image",
            blob=image_blob,
            artifact_type="system-input-image",
            producer="test",
            producer_version="1",
            item_id=self.item_ids[0],
        )
        configuration = {
            "paperName": "table-image-paper",
            "paperCategory": "题型专项",
            "provinceId": {"id": 440000, "name": "广东省"},
            "cityId": {"id": 440600, "name": "佛山市"},
            "districtIds": [],
            "stageId": {"id": 2, "name": "初中"},
            "gradeId": {"id": 9, "name": "九年级"},
            "year": 2026,
            "answerTimeMinutes": 20,
            "platformTemplateName": "听后记录并转述信息",
        }
        page_input = {
            "schema_version": "paper-page-input-v1",
            "input_type": "paper",
            "type": "听后记录并转述信息",
            "recording": {
                "listening_text": "Cindy's room is tidy.",
                "image_artifact_id": "record-image",
                "table_image_required": True,
                "table_index": 1,
                "questions": [{"number": 1, "answers": ["tidy"], "score": 1}],
            },
            "retelling": {
                "prompt": "This is Cindy's room.",
                "reference_answers": ["This is Cindy's room."],
                "score": 5,
            },
        }
        executor = PlatformInputWorkflowPageExecutor(
            self.repository,
            self.artifacts,
            profile_dir=Path(self.temp.name) / "chrome-profile",
            admin_url="https://admin.example.test/#/resource/exam",
            api_base="https://api.example.test",
            login_timeout=1,
        )
        unit = {
            "unit_id": "unit-table-image",
            "unit_label": "第1套",
            "input_type": "paper",
            "configuration": configuration,
            "segments": [{
                "segment_id": "segment-table-image",
                "item_id": self.item_ids[0],
                "raw_text": "Cindy's room is tidy.",
                "tts_text": "Cindy's room is tidy.",
                "score": 8,
                "audio_artifact_id": "audio-1",
                "page_input": page_input,
            }],
        }
        with tempfile.TemporaryDirectory(prefix="wordtts-page-table-image-") as directory:
            raw = executor._build_raw_spec(
                {"workflow_id": self.workflow.workflow_id, "unit": unit},
                Path(directory),
            )
            recording = raw["question_groups"][0]["recording"]
            self.assertNotIn("image_artifact_id", recording)
            self.assertTrue(Path(recording["image_path"]).is_file())

    def test_page_executor_preflight_fills_base_form_before_template_lookup(self) -> None:
        class FakePage:
            url = "https://admin.example.test/#/resource/exam"

        class FakeContext:
            pages = [FakePage()]

            def close(self) -> None:
                return None

        class FakePlaywright:
            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        calls: list[str] = []

        class FakeObserver:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                return None

        class FakeAutomation:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                return None

            def wait_until_ready(self, *_args: object) -> None:
                calls.append("wait_until_ready")

            def start_new_paper(self) -> None:
                calls.append("start_new_paper")

            def fill_base_form(self) -> None:
                calls.append("fill_base_form")

            def select_template(self) -> None:
                calls.append("select_template")

        unit = dict(self.projection["units"][0])
        unit["configuration"] = self._configuration()["units"][0]
        unit["segments"] = [
            segment
            for segment in self.projection["content_segments"]
            if segment["unit_id"] == unit["unit_id"]
        ][:1]
        executor = PlatformInputWorkflowPageExecutor(
            self.repository,
            self.artifacts,
            profile_dir=Path(self.temp.name) / "chrome-profile",
            admin_url="https://admin.example.test/#/resource/exam",
            api_base="https://api.example.test",
            login_timeout=1,
        )
        with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywright()), \
             patch("platform_entry.paper_input._launch_browser", return_value=FakeContext()), \
             patch("platform_entry.paper_input.ReadOnlyFeedbackObserver", FakeObserver), \
             patch("platform_entry.paper_input.PlatformInputPageAutomation", FakeAutomation):
            executor.preflight({
                "workflow_id": self.workflow.workflow_id,
                "target": {"input_type": "paper"},
                "unit": unit,
            })

        self.assertEqual(
            calls,
            ["wait_until_ready", "start_new_paper", "fill_base_form", "select_template"],
        )

    def test_page_executor_rejects_malformed_artifact_metadata_without_leaking_exception(self) -> None:
        class BrokenRepository:
            def get_artifact_storage(self, _artifact_id: str, *, workflow_id: str) -> dict[str, object]:
                return {
                    "artifact_type": "tts-segment",
                    "format": "mp3",
                    "storage_key": "blobs/aa/not-a-real-file.mp3",
                    "size_bytes": "not-an-integer",
                    "sha256": "0" * 64,
                }

        executor = PlatformInputWorkflowPageExecutor(
            BrokenRepository(),
            self.artifacts,
            profile_dir=Path(self.temp.name) / "chrome-profile",
            admin_url="https://admin.example.test/#/resource/exam",
            api_base="https://api.example.test",
            login_timeout=1,
        )
        with tempfile.TemporaryDirectory(prefix="wordtts-page-payload-") as directory:
            with self.assertRaises(SystemInputError) as error:
                executor._materialize_audio(
                    "broken-audio",
                    self.workflow.workflow_id,
                    Path(directory) / "segment.mp3",
                )
        self.assertEqual(error.exception.code, "ARTIFACT_INVALID")

    def test_safe_retry_reuses_input_run_and_adds_attempt(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        self.service.external = None
        self.service.executor = lambda _payload: {"external_record_id": "unused"}
        first = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-retry-first",
        )
        run_id = first["input_run_id"]
        failed = self.service.execute_input_run(run_id)
        self.assertEqual(failed["input_status"], "failed_retryable")

        self.service.external = self.external
        self.service.executor = lambda payload: {
            "external_record_id": f"paper-{payload['unit']['unit_id']}",
            "feedback": {"status": "saved"},
        }
        current_version = self.repository.get_workflow(self.workflow.workflow_id).state_version
        retried = self.service.start_input(
            self.workflow.workflow_id,
            current_version,
            idempotency_key="system-input-retry-second",
        )
        self.assertEqual(retried["input_run_id"], run_id)
        self.assertTrue(retried["retry"])
        with self.database.read_transaction() as con:
            attempt_count = con.execute(
                "SELECT COUNT(*) FROM input_attempts WHERE input_run_id=?",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(attempt_count, 4)
        finished = self.service.execute_input_run(run_id)
        self.assertEqual(finished["input_status"], "succeeded")

        replay = self.service.start_input(
            self.workflow.workflow_id,
            self.repository.get_workflow(self.workflow.workflow_id).state_version,
            idempotency_key="system-input-retry-second",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["input_run_id"], run_id)

    def test_retry_does_not_execute_stale_pending_attempts(self) -> None:
        """A stopped run's old PENDING rows must not win over retry rows."""

        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-stale-pending-first",
        )
        run_id = started["input_run_id"]

        # Stopping before the worker claims a unit intentionally leaves the
        # original attempts PENDING.  The next explicit retry appends a new
        # attempt for each same entry to this run.
        stopped = self.service.finalize_stopped_input_run(run_id)
        self.assertEqual(stopped["input_run"]["status"], "FAILED")

        self.service.executor = lambda payload: {
            "external_record_id": f"paper-{payload['unit']['unit_id']}",
            "feedback": {"status": "saved"},
        }
        current_version = self.repository.get_workflow(self.workflow.workflow_id).state_version
        retried = self.service.start_input(
            self.workflow.workflow_id,
            current_version,
            idempotency_key="system-input-stale-pending-second",
        )
        self.assertTrue(retried["retry"])

        with self.database.read_transaction() as con:
            before = con.execute(
                """SELECT attempt_id FROM input_attempts
                   WHERE input_run_id=? ORDER BY created_at, rowid""",
                (run_id,),
            ).fetchall()
        old_attempt_ids = {str(row["attempt_id"]) for row in before[:2]}
        new_attempt_ids = {str(row["attempt_id"]) for row in before[2:]}

        finished = self.service.execute_input_run(run_id)
        self.assertEqual(finished["input_status"], "succeeded")
        with self.database.read_transaction() as con:
            rows = con.execute(
                """SELECT attempt_id, status FROM input_attempts
                   WHERE input_run_id=? ORDER BY created_at, rowid""",
                (run_id,),
            ).fetchall()
        statuses = {str(row["attempt_id"]): str(row["status"]) for row in rows}
        self.assertEqual({statuses[attempt_id] for attempt_id in old_attempt_ids}, {"PENDING"})
        self.assertEqual({statuses[attempt_id] for attempt_id in new_attempt_ids}, {"SUCCEEDED"})

    def test_executor_preflight_failure_is_retryable_before_side_effect_fence(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )

        class PreflightFailExecutor:
            def __init__(self) -> None:
                self.preflight_calls = 0
                self.execute_calls = 0

            def preflight(self, payload: dict[str, object]) -> None:
                self.preflight_calls += 1
                self.assert_page_safe_payload(payload)
                raise SystemInputError(
                    "平台登录会话无效",
                    code="INPUT_PLATFORM_SESSION_INVALID",
                    details={
                        "phase": "preflight",
                        "step": "wait_until_ready",
                        "error_type": "PlatformInputLoginError",
                        "error_message": "页面仍处于登录页",
                    },
                )

            def assert_page_safe_payload(self, payload: dict[str, object]) -> None:
                self_target = json.dumps(payload, ensure_ascii=False)
                if "must stay source-only" in self_target:
                    raise AssertionError("预检 payload 泄漏了答案事实")

            def __call__(self, _payload: dict[str, object]) -> dict[str, object]:
                self.execute_calls += 1
                raise AssertionError("页面执行不应在预检失败后启动")

        executor = PreflightFailExecutor()
        self.service.executor = executor
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-preflight-fail",
        )

        failed = self.service.execute_input_run(started["input_run_id"])

        self.assertEqual(failed["input_status"], "failed_retryable")
        self.assertEqual(executor.preflight_calls, 2)
        self.assertEqual(executor.execute_calls, 0)
        with self.database.read_transaction() as con:
            attempts = con.execute(
                "SELECT status, side_effect_state FROM input_attempts WHERE input_run_id=? ORDER BY entry_id",
                (started["input_run_id"],),
            ).fetchall()
            operations = con.execute(
                "SELECT side_effect_state FROM external_operations ORDER BY external_operation_id",
            ).fetchall()
        self.assertEqual(
            [(row["status"], row["side_effect_state"]) for row in attempts],
            [("FAILED", "INTENT_RECORDED"), ("FAILED", "INTENT_RECORDED")],
        )
        self.assertEqual(
            [row["side_effect_state"] for row in operations],
            ["REJECTED", "REJECTED"],
        )
        attempts_projection = failed["input_run"]["attempts"]
        self.assertEqual(len(attempts_projection), 2)
        self.assertEqual(
            attempts_projection[0]["evidence"]["details"]["step"],
            "wait_until_ready",
        )
        self.assertEqual(
            attempts_projection[0]["evidence"]["details"]["error_type"],
            "PlatformInputLoginError",
        )
        self.assertTrue(failed["configuration_editable"])

        replacement = self._configuration()
        for unit in replacement["units"]:
            unit["platformTemplateName"] = "模仿朗读-备用"
        updated = self.service.save_configuration(
            self.workflow.workflow_id,
            self.repository.get_workflow(self.workflow.workflow_id).state_version,
            replacement,
        )
        self.assertEqual(
            updated["projection"]["units"][0]["configuration"]["platformTemplateName"],
            "模仿朗读-备用",
        )
        self.assertTrue(updated["projection"]["configuration_editable"])

    def test_safe_retry_after_preflight_rejection_creates_fresh_operations(self) -> None:
        saved = self.service.save_configuration(
            self.workflow.workflow_id,
            self.workflow.state_version,
            self._configuration(),
        )
        self._finish_audio_workflow()
        accepted = self.service.accept_audio(
            self.workflow.workflow_id,
            saved["snapshot"]["state_version"],
        )

        class TogglePreflightExecutor:
            def __init__(self) -> None:
                self.fail_preflight = True
                self.preflight_calls = 0
                self.execute_calls = 0

            def preflight(self, _payload: dict[str, object]) -> None:
                self.preflight_calls += 1
                if self.fail_preflight:
                    raise SystemInputError(
                        "平台登录会话无效",
                        code="INPUT_PLATFORM_SESSION_INVALID",
                    )

            def __call__(self, payload: dict[str, object]) -> dict[str, object]:
                self.execute_calls += 1
                return {
                    "external_record_id": f"paper-{payload['unit']['unit_id']}",
                    "feedback": {"status": "saved"},
                }

        executor = TogglePreflightExecutor()
        self.service.executor = executor
        started = self.service.start_input(
            self.workflow.workflow_id,
            accepted["snapshot"]["state_version"],
            idempotency_key="system-input-retry-rejected-first",
        )
        failed = self.service.execute_input_run(started["input_run_id"])
        self.assertEqual(failed["input_status"], "failed_retryable")

        executor.fail_preflight = False
        current_version = self.repository.get_workflow(self.workflow.workflow_id).state_version
        retried = self.service.start_input(
            self.workflow.workflow_id,
            current_version,
            idempotency_key="system-input-retry-rejected-second",
        )
        finished = self.service.execute_input_run(retried["input_run_id"])

        self.assertEqual(finished["input_status"], "succeeded")
        self.assertFalse(finished["configuration_editable"])
        self.assertEqual(executor.preflight_calls, 4)
        self.assertEqual(executor.execute_calls, 2)
        with self.database.read_transaction() as con:
            operations = con.execute(
                "SELECT side_effect_state, external_operation_key FROM external_operations ORDER BY created_at, external_operation_id",
            ).fetchall()
        self.assertEqual([row["side_effect_state"] for row in operations].count("REJECTED"), 2)
        self.assertEqual([row["side_effect_state"] for row in operations].count("CONFIRMED"), 2)
        self.assertTrue(any(":attempt:" in row["external_operation_key"] for row in operations))


if __name__ == "__main__":
    unittest.main()

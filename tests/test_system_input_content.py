from __future__ import annotations

import unittest

from workflow.system_input_content import (
    PageInputFactsError,
    build_page_input_facts,
    document_entry_support,
    page_input_completeness,
)
from workflow.system_input_content import (
    register_page_input_sanitizer,
    sanitize_page_input,
)
from workflow.parser import normalize_item
from workflow.system_input_executor import PlatformInputWorkflowPageExecutor


class SystemInputContentTests(unittest.TestCase):
    def test_document_entry_preflight_recognizes_the_four_supported_shapes(self) -> None:
        full_paper = document_entry_support(
            [{"type": page_type} for page_type in (
                "听后选择",
                "听后应答",
                "模仿朗读",
                "听后记录并转述信息",
            )],
            input_type="paper",
            document_types=["听后选择", "听后应答", "模仿朗读", "听后记录并转述信息"],
            major_section_profiles=[None, None, "imitation_boxed_special", None],
            entry_profiles=[None, None, "imitation_reading_v1", None],
            capabilities=[None, None, {"external_input": True}, None],
        )
        self.assertTrue(full_paper["supported"])
        self.assertEqual(full_paper["format"], "listening_paper")
        self.assertEqual(full_paper["label"], "听说测试题")

        imitation = document_entry_support(
            [{"type": "模仿朗读"}, {"type": "模仿朗读"}],
            input_type="paper",
            document_types=["模仿朗读"],
            major_section_profiles=["imitation_boxed_special", "imitation_boxed_special"],
            entry_profiles=["imitation_reading_v1", "imitation_reading_v1"],
            capabilities=[
                {"external_input": True},
                {"external_input": True},
            ],
        )
        self.assertTrue(imitation["supported"])
        self.assertEqual(imitation["format"], "imitation_reading")
        self.assertEqual(imitation["label"], "模仿朗读")

        response = document_entry_support(
            [{"type": "听后应答"}],
            input_type="paper",
            document_types=["听后应答"],
            major_section_profiles=["response_colored_options_special"],
            entry_profiles=["listening_response_v1"],
            capabilities=[{"external_input": True}],
        )
        self.assertTrue(response["supported"])
        self.assertEqual(response["format"], "listening_response")
        self.assertEqual(response["label"], "听后应答")

        record_retelling = document_entry_support(
            [{"type": "听后记录并转述信息"}],
            input_type="paper",
            document_types=["听后记录并转述信息"],
            major_section_profiles=["record_retelling_table_special"],
            entry_profiles=["listening_record_retelling_v1"],
            capabilities=[{"external_input": True}],
        )
        self.assertTrue(record_retelling["supported"])
        self.assertEqual(record_retelling["format"], "listening_record_retelling")
        self.assertEqual(record_retelling["label"], "听后记录并转述信息")

    def test_record_retelling_entry_requires_confirmed_table_profile(self) -> None:
        missing_profile = document_entry_support(
            [{"type": "听后记录并转述信息"}],
            input_type="paper",
            document_types=["听后记录并转述信息"],
        )

        self.assertFalse(missing_profile["supported"])
        self.assertEqual(missing_profile["major_section_profile_invalid_count"], 1)
        self.assertEqual(missing_profile["entry_profile_invalid_count"], 1)
        self.assertEqual(missing_profile["entry_capability_invalid_count"], 1)
        self.assertEqual(missing_profile["expected_types"], ["听后记录并转述信息"])

    def test_response_entry_requires_confirmed_colored_options_profile(self) -> None:
        missing_profile = document_entry_support(
            [{"type": "听后应答"}],
            input_type="paper",
            document_types=["听后应答"],
        )

        self.assertFalse(missing_profile["supported"])
        self.assertEqual(missing_profile["major_section_profile_invalid_count"], 1)
        self.assertEqual(missing_profile["entry_profile_invalid_count"], 1)
        self.assertEqual(missing_profile["entry_capability_invalid_count"], 1)
        self.assertEqual(missing_profile["expected_types"], ["听后应答"])
        self.assertIn("听后应答", missing_profile["reason"])

    def test_imitation_entry_requires_registered_profile(self) -> None:
        missing_profile = document_entry_support(
            [{"type": "模仿朗读"}],
            input_type="paper",
            document_types=["模仿朗读"],
        )
        self.assertFalse(missing_profile["supported"])
        self.assertEqual(missing_profile["entry_profile_invalid_count"], 1)

        legacy_profile = document_entry_support(
            [None],
            input_type="paper",
            document_types=["模仿朗读"],
            major_section_profiles=["imitation_legacy_unit_source"],
            entry_profiles=["imitation_legacy_unit_source"],
        )
        self.assertFalse(legacy_profile["supported"])
        self.assertEqual(legacy_profile["entry_profile_invalid_count"], 1)

        mixed_profiles = document_entry_support(
            [{"type": "模仿朗读"}, {"type": "模仿朗读"}],
            input_type="paper",
            document_types=["模仿朗读", "模仿朗读"],
            major_section_profiles=["imitation_boxed_special", "imitation_boxed_special"],
            entry_profiles=["imitation_reading_v1", None],
        )
        self.assertFalse(mixed_profiles["supported"])
        self.assertEqual(mixed_profiles["entry_profile_invalid_count"], 1)

        legacy_alias = document_entry_support(
            [None, {"type": "模仿朗读"}],
            input_type="paper",
            # Older workspaces sometimes persisted the source suffix as the
            # document type instead of keeping it only in category.
            document_types=["模仿朗读-外网", "模仿朗读"],
            major_section_profiles=["imitation_legacy_unit_source", "imitation_boxed_special"],
            entry_profiles=[None, "imitation_reading_v1"],
            capabilities=[{"external_input": False}, {"external_input": True}],
        )
        self.assertFalse(legacy_alias["supported"])
        self.assertEqual(legacy_alias["entry_profile_invalid_count"], 1)

        stale_upgrade = document_entry_support(
            [{"type": "模仿朗读"}],
            input_type="paper",
            document_types=["模仿朗读-外网"],
            major_section_profiles=["imitation_legacy_unit_source"],
            entry_profiles=["imitation_reading_v1"],
            capabilities=[{"external_input": True}],
        )
        self.assertFalse(stale_upgrade["supported"])
        self.assertEqual(stale_upgrade["major_section_profile_invalid_count"], 1)

    def test_imitation_page_input_builder_requires_profile_and_capability(self) -> None:
        legacy = build_page_input_facts(
            "模仿朗读",
            {},
            {"text": "Legacy passage."},
            0,
        )
        self.assertIsNone(legacy)

        legacy_alias = build_page_input_facts(
            "模仿朗读-外网",
            {},
            {
                "text": "Legacy passage.",
                "page_input": {"type": "模仿朗读", "questions": []},
            },
            0,
        )
        self.assertIsNone(legacy_alias)

        incomplete = build_page_input_facts(
            "模仿朗读",
            {},
            {
                "text": "Recognized passage.",
                "entry_profile": "imitation_reading_v1",
                "major_section_profile": "imitation_boxed_special",
                "capabilities": {"external_input": False},
            },
            0,
        )
        self.assertIsNone(incomplete)

        eligible = build_page_input_facts(
            "模仿朗读",
            {},
            {
                "text": "Eligible passage.",
                "score": 7,
                "entry_profile": "imitation_reading_v1",
                "major_section_profile": "imitation_boxed_special",
                "capabilities": {"external_input": True},
            },
            0,
        )
        self.assertEqual(eligible["type"], "模仿朗读")
        self.assertEqual(eligible["questions"][0]["score"], 7)
        self.assertNotIn("reference_answers", eligible["questions"][0])
        self.assertEqual(page_input_completeness(eligible)["status"], "complete")

        stale_upgrade = build_page_input_facts(
            "模仿朗读-外网",
            {},
            {
                "text": "Legacy passage with stale entry facts.",
                "major_section_profile": "imitation_legacy_unit_source",
                "entry_profile": "imitation_reading_v1",
                "capabilities": {"external_input": True},
                "page_input": {"type": "模仿朗读", "questions": []},
            },
            0,
        )
        self.assertIsNone(stale_upgrade)

    def test_document_entry_preflight_hides_partial_and_missing_parser_shapes(self) -> None:
        partial = document_entry_support(
            [{"type": "听后选择"}, {"type": "听后应答"}],
            input_type="paper",
            document_types=["听后选择", "听后应答"],
        )
        self.assertFalse(partial["supported"])
        self.assertEqual(partial["status"], "unsupported")

        missing = document_entry_support([], input_type="paper", document_types=[])
        self.assertFalse(missing["supported"])
        self.assertEqual(missing["status"], "unsupported")

        reserved = document_entry_support(
            [{"type": "模仿朗读"}],
            input_type="textbook",
            document_types=["模仿朗读"],
        )
        self.assertFalse(reserved["supported"])
        self.assertEqual(reserved["status"], "unsupported")

    def test_record_image_keeps_internal_artifact_reference_and_drops_path(self) -> None:
        facts = sanitize_page_input(
            {
                "type": "听后记录并转述信息",
                "recording": {
                    "image_artifact_id": "artifact-image-1",
                    "image_path": "/Users/should-never-persist/table.png",
                    "listening_text": "Cindy's room is small.",
                },
            }
        )

        self.assertEqual(facts["recording"]["image_artifact_id"], "artifact-image-1")
        self.assertNotIn("image_path", facts["recording"])

    def test_required_record_table_without_artifact_is_incomplete(self) -> None:
        facts = sanitize_page_input(
            {
                "type": "听后记录并转述信息",
                "recording": {
                    "table_image_required": True,
                    "table_index": 1,
                    "listening_text": "Cindy's room is small.",
                    "questions": [{"score": 1, "answers": ["tidy"]}],
                },
                "retelling": {
                    "prompt": "This is Cindy's room.",
                    "score": 5,
                    "reference_answers": ["It is tidy."],
                },
            }
        )
        self.assertEqual(
            page_input_completeness(facts)["reason"],
            "听后记录缺少文档块图片产物，不能开始录入",
        )
        facts["recording"]["image_artifact_id"] = "artifact-image-1"
        self.assertEqual(page_input_completeness(facts)["status"], "complete")

    def test_positioned_drawing_selector_survives_sanitization(self) -> None:
        facts = sanitize_page_input(
            {
                "type": "听后记录并转述信息",
                "recording": {
                    "block_image_required": True,
                    "block_kind": "drawing_group",
                    "block_index": 0,
                    "listening_text": "Emma describes her plan card.",
                    "questions": [],
                },
            }
        )

        self.assertTrue(facts["recording"]["block_image_required"])
        self.assertEqual(facts["recording"]["block_kind"], "drawing_group")
        self.assertEqual(facts["recording"]["block_index"], 0)

    def test_duplicate_option_ids_fail_closed(self) -> None:
        with self.assertRaises(PageInputFactsError):
            sanitize_page_input(
                {
                    "type": "听后应答",
                    "questions": [{
                        "prompt": "Question",
                        "options": [
                            {"option_id": "A", "text": "One"},
                            {"option_id": "a", "text": "Two"},
                        ],
                        "answer": "A",
                        "score": 1,
                    }],
                }
            )

    def test_shared_selection_material_retains_one_audio_for_multiple_questions(self) -> None:
        facts = sanitize_page_input(
            {
                "type": "听后选择",
                "materials": [
                    {
                        "listening_text": "A shared listening script.",
                        "questions": [
                            {
                                "prompt": "Question one",
                                "options": {"A": "One", "B": "Two"},
                                "answer": "A",
                                "score": 1,
                            },
                            {
                                "prompt": "Question two",
                                "options": {"A": "Three", "B": "Four"},
                                "answer": "B",
                                "score": 1,
                            },
                        ],
                    }
                ],
            }
        )
        groups = PlatformInputWorkflowPageExecutor._build_grouped_page_content(
            [(facts, "/private/segment-1.mp3", {"raw_text": "fallback"})]
        )

        self.assertEqual(len(groups), 1)
        material = groups[0]["materials"][0]
        self.assertEqual(material["audio_path"], "/private/segment-1.mp3")
        self.assertEqual(len(material["questions"]), 2)

    def test_info_acquisition_maps_question_audio_stems_to_prompt_audio(self) -> None:
        facts = sanitize_page_input(
            {
                "type": "信息获取",
                "materials": [{
                    "listening_text": "A shared listening script.",
                    "questions": [{
                        "prompt": "How many subjects does Mary have at school?",
                        "options": [
                            {"option_id": "A", "text": "Four."},
                            {"option_id": "B", "text": "Five."},
                            {"option_id": "C", "text": "Six."},
                        ],
                        "prompt_audio_filename_stem": "信息获取题目-1",
                        "reference_answers": ["Five.", "Five subjects."],
                        "reference_answers_source": "document",
                        "score": 1.5,
                    }],
                }],
            }
        )

        self.assertEqual(
            facts["materials"][0]["questions"][0]["reference_answers_source"],
            "document",
        )

        groups = PlatformInputWorkflowPageExecutor._build_grouped_page_content(
            [
                (
                    facts,
                    "/private/segment-1.mp3",
                    {"raw_text": "fallback"},
                )
            ],
            {"信息获取题目-1": "/private/question-1.mp3"},
        )

        question = groups[0]["materials"][0]["questions"][0]
        self.assertEqual(question["prompt_audio_path"], "/private/question-1.mp3")

    def test_legacy_page_group_boundary_strips_speaker_markers_from_listening_text(self) -> None:
        # Older saved page facts can still carry the TTS source instead of the
        # page-display copy.  The final grouping boundary must keep those
        # speaker labels out of the external editor while leaving TTS storage
        # untouched elsewhere.
        retelling_facts = {
            "type": "信息转述及询问",
            "recording": {
                "listening_text": "(W)Emma introduces her plan.\n(M)A second line.",
            },
            "retelling": {"prompt": "Let me tell you about Emma.", "score": 6},
            "asking": [],
        }
        retelling_groups = PlatformInputWorkflowPageExecutor._build_grouped_page_content(
            [(retelling_facts, "/private/retelling.mp3", {"tts_text": retelling_facts["recording"]["listening_text"]})]
        )
        self.assertEqual(
            retelling_groups[0]["recording"]["listening_text"],
            "Emma introduces her plan.\nA second line.",
        )

        acquisition_facts = {
            "type": "信息获取",
            "materials": [{
                "section": "回答问题",
                "listening_text": "(M)Tom answers the question.",
                "questions": [{
                    "prompt": "What is the answer?",
                    "score": 1,
                    "reference_answers": ["The answer."],
                }],
            }],
        }
        acquisition_groups = PlatformInputWorkflowPageExecutor._build_grouped_page_content(
            [(acquisition_facts, "/private/acquisition.mp3", {"tts_text": "(M)fallback"})]
        )
        self.assertEqual(
            acquisition_groups[0]["materials"][0]["listening_text"],
            "Tom answers the question.",
        )

    def test_info_retelling_page_boundary_splits_legacy_slash_answers(self) -> None:
        facts = build_page_input_facts(
            "信息转述及询问",
            {
                "recording": {"listening_text": "A listening script."},
                "retelling": {"prompt": "Start here.", "score": 6},
                "asking": [{
                    "number": 11,
                    "prompt": "这个计划可能有什么困难？",
                    "reference_answer": "One answer. / Another answer.",
                }],
            },
            {"category": "信息转述录音稿", "text": "A listening script."},
            0,
        )

        self.assertEqual(
            facts["asking"][0]["reference_answers"],
            ["One answer.", "Another answer."],
        )

    def test_response_can_use_listening_text_as_question_prompt(self) -> None:
        facts = sanitize_page_input(
            {
                "type": "听后应答",
                "questions": [
                    {
                        "listening_text": "How are you today?",
                        "options": {"A": "Fine.", "B": "Thanks."},
                        "answer": "A",
                        "score": 1,
                    }
                ],
            }
        )

        self.assertEqual(
            facts["questions"][0]["prompt"],
            "How are you today?",
        )
        self.assertEqual(
            facts["questions"][0]["listening_text"],
            "How are you today?",
        )

    def test_parser_does_not_apply_generic_metadata_cap_to_page_facts(self) -> None:
        references = [f"Reference answer {index}" for index in range(40)]
        item = normalize_item(
            {
                "text": "Read this passage.",
                "page_input": {
                    "type": "模仿朗读",
                    "questions": [{
                        "listening_text": "Read this passage.",
                        "score": 1,
                        "reference_answers": references,
                    }],
                },
            },
            sequence=0,
            source_basis="test-source",
            document_type="模仿朗读",
        )

        self.assertEqual(
            len(item.metadata["page_input"]["questions"][0]["reference_answers"]),
            40,
        )

    def test_future_content_type_can_register_its_own_bounded_sanitizer(self) -> None:
        input_type = "test-textbook-content"
        register_page_input_sanitizer(
            input_type,
            lambda value: {
                "schema_version": "test-textbook-v1",
                "input_type": input_type,
                "unit": str(value.get("unit") or "")[:64],
            },
        )

        facts = sanitize_page_input({"input_type": input_type, "unit": "Unit 1"})
        self.assertEqual(facts["schema_version"], "test-textbook-v1")
        self.assertEqual(facts["unit"], "Unit 1")


if __name__ == "__main__":
    unittest.main()

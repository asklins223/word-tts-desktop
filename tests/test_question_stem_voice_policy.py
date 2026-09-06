"""信息转述题干的角色音色默认值与可编辑覆盖回归测试。"""

from pathlib import Path

from question_types.segmenter import parse_document_once
from workflow.engine import WorkflowEngine
from workflow.parser import DocumentParser
from workflow.providers import XunfeiTTSAdapter
from wordtts import QUESTION_STEM_VOICE, build_synthesis_segments, normalize_tts_config
from wordtts.composite_plan import build_composite_work_plan
from xunfei.voice_catalog import get_voice_info
from xunfei_voice_catalog import normalize_catalog


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/documents/信息转述及询问信息 7上- U1.docx"


def test_info_retelling_prompt_is_an_editable_question_stem_role():
    results, _ = parse_document_once(FIXTURE)
    audio_items = {
        item["category"]: item
        for item in results[0]["audio_items"]
    }
    prompt = audio_items["信息转述题干"]
    instruction = audio_items["信息转述题目指导文字"]
    asking_instruction = audio_items["询问信息题干"]

    assert instruction["role"] == "题干音色"
    assert prompt["role"] == "题干音色"
    assert asking_instruction["role"] == "题干音色"
    assert "提两个问题" in asking_instruction["text"]
    assert "15 秒钟的准备时间" in asking_instruction["text"]
    assert "8 秒钟的提问时间" in asking_instruction["text"]
    # Do not pin voice_key on either parser item: the workflow role mapping is
    # the user's later manual override point.
    assert "voice_key" not in instruction
    assert "voice_key" not in prompt
    assert "voice_key" not in asking_instruction

    parsed = DocumentParser().parse(FIXTURE, include_auxiliary_audio=True)
    assert [
        (item.item_type, item.role)
        for item in parsed.items
    ] == [
        ("信息转述录音稿", None),
        ("信息转述题目指导文字", "题干音色"),
        ("信息转述题干", "题干音色"),
        ("询问信息题干", "题干音色"),
    ]
    prompt_item = next(item for item in parsed.items if item.item_type == "信息转述题干")
    assert prompt_item.role == "题干音色"
    assert prompt_item.voice_key is None
    asking_item = next(item for item in parsed.items if item.item_type == "询问信息题干")
    assert asking_item.role == "题干音色"
    assert asking_item.voice_key is None


def test_question_stem_item_type_is_recovered_for_older_work_items():
    instruction_plan = WorkflowEngine._effective_plan_item(
        {
            "item_id": "instruction-1",
            "item_type": "信息转述题目指导文字",
            "content": "你将听到一段介绍。",
            "role": None,
            "voice_key": None,
        },
        {
            "default_female_voice": "amanda",
            "default_male_voice": "george",
        },
    )
    asking_instruction_plan = WorkflowEngine._effective_plan_item(
        {
            "item_id": "asking-instruction-1",
            "item_type": "询问信息题干",
            "content": "请根据以下提示向 Li Ling 提两个问题。",
            "role": None,
            "voice_key": None,
        },
        {
            "default_female_voice": "amanda",
            "default_male_voice": "george",
        },
    )
    ordinary_plan = WorkflowEngine._effective_plan_item(
        {
            "item_id": "recording-1",
            "item_type": "信息转述录音稿",
            "content": "普通录音稿",
            "role": None,
            "voice_key": None,
        },
        {
            "default_female_voice": "amanda",
            "default_male_voice": "george",
        },
    )
    assert (
        instruction_plan["role"],
        instruction_plan["voice_key"],
        instruction_plan["speed"],
        instruction_plan["pitch"],
        instruction_plan["volume"],
    ) == ("题干音色", QUESTION_STEM_VOICE, 40, 50, 50)
    assert (
        asking_instruction_plan["role"],
        asking_instruction_plan["voice_key"],
        asking_instruction_plan["speed"],
        asking_instruction_plan["pitch"],
        asking_instruction_plan["volume"],
    ) == ("题干音色", QUESTION_STEM_VOICE, 40, 50, 50)
    assert (
        ordinary_plan["role"],
        ordinary_plan["voice_key"],
        ordinary_plan["speed"],
    ) == (None, "amanda", 50)


def test_question_stem_uses_xiaoyan_40_50_50_and_keeps_manual_overrides():
    default_segments = build_synthesis_segments(
        "你的介绍可以这样开始：Let me tell you about Emma.",
        50,
        50,
        50,
        default_role="题干音色",
    )
    assert [
        (item["voice_key"], item["speed"], item["pitch"], item["volume"])
        for item in default_segments
    ] == [(QUESTION_STEM_VOICE, 40, 50, 50)]

    overridden = build_synthesis_segments(
        "Let me tell you about Emma.",
        50,
        50,
        50,
        default_role="题干音色",
        role_voices={"题干音色": "common:manual"},
        role_configs={
            "role:题干音色": {"rate": 62, "pitch": 47, "volume": 81},
        },
    )
    assert [
        (item["voice_key"], item["speed"], item["pitch"], item["volume"])
        for item in overridden
    ] == [("common:manual", 62, 47, 81)]

    plan = WorkflowEngine._effective_plan_item(
        {"item_id": "stem-1", "content": "Let me tell you.", "role": "题干音色", "voice_key": None},
        {
            "default_female_voice": "amanda",
            "default_male_voice": "george",
            "role_configs": {"role:题干音色": {"rate": 62, "pitch": 47, "volume": 81}},
            "role_voices": {"题干音色": "common:manual"},
        },
    )
    assert (plan["voice_key"], plan["speed"], plan["pitch"], plan["volume"]) == (
        "common:manual", 62, 47, 81
    )


def test_question_stem_role_cannot_leak_from_profile_to_default_female_items():
    payload = {
        "plan": [
            {
                "item_id": "ordinary",
                "content": "普通默认女声段落",
                "role": None,
                "voice_key": "amanda",
                "speed": 50,
                "pitch": 50,
                "volume": 50,
            },
            {
                "item_id": "stem",
                "item_type": "信息转述题目指导文字",
                "content": "你的介绍可以这样开始：Let me tell you about Emma.",
                "role": None,
                "voice_key": None,
                "speed": 50,
                "pitch": 50,
                "volume": 50,
            },
        ],
        "profile": {
            # This is a stale/legacy global value. It must not override the
            # item-level role of ordinary content.
            "default_role": "题干音色",
            "default_female_voice": "amanda",
            "default_male_voice": "george",
            "role_voices": {"题干音色": QUESTION_STEM_VOICE},
            "role_configs": {
                "role:题干音色": {"rate": 40, "pitch": 50, "volume": 50},
            },
        },
    }

    specs = XunfeiTTSAdapter._legacy_item_specs(payload)
    assert [spec["default_role"] for spec in specs] == [None, "题干音色"]
    works = build_composite_work_plan(specs)
    assert [
        [
            (segment["role"], segment["voice_key"], segment["speed"])
            for segment in item["segments"]
        ]
        for item in works[0]["items"]
    ] == [
        [(None, "amanda", 50)],
        [("题干音色", QUESTION_STEM_VOICE, 40)],
    ]


def test_question_stem_policy_is_present_in_config_and_offline_catalog():
    normalized = normalize_tts_config({"role_configs": {"role:题干音色": {}}})
    assert normalized["role_configs"]["role:题干音色"] == {
        "rate": 40,
        "pitch": 50,
        "volume": 50,
    }
    assert get_voice_info(QUESTION_STEM_VOICE)["name"] == "晓燕"
    builtin = {item["key"]: item for item in normalize_catalog([], source="builtin")["voices"]}
    assert builtin[QUESTION_STEM_VOICE]["name"] == "晓燕"
    assert builtin[QUESTION_STEM_VOICE]["speaker_no"] == 130165

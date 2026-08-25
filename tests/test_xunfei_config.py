from __future__ import annotations

import unittest

import word_tts_app as core


class XunfeiConfigTests(unittest.TestCase):
    def test_three_platform_parameters_are_integer_values_between_zero_and_hundred(self):
        config = core.normalize_tts_config({
            "rate": -20,
            "volume": 101.4,
            "pitch": "not-a-number",
        })

        self.assertEqual(config["rate"], 0)
        self.assertEqual(config["volume"], 100)
        self.assertEqual(config["pitch"], 50)
        self.assertEqual(
            {key for key in config if key in {"proxy", "female_voice", "male_voice", "word_voice"}},
            set(),
        )
        self.assertNotIn("pause", config)

        invalid_numeric_config = core.normalize_tts_config({"rate": float("inf")})
        self.assertEqual(invalid_numeric_config["rate"], 50)

    def test_default_configuration_uses_fifty_for_all_platform_parameters(self):
        config = core.normalize_tts_config()

        self.assertEqual(
            (config["rate"], config["pitch"], config["volume"]),
            (50, 50, 50),
        )
        self.assertEqual(config["format"], "mp3")
        self.assertEqual(config["quality"], "128 kbps（标准）")

    def test_words_and_sentences_always_use_default_female_voice(self):
        self.assertEqual(
            core.default_voice_for_item({"category": "单词", "voice": "male"}),
            core.FEMALE_VOICE,
        )
        self.assertEqual(
            core.default_voice_for_item({"category": "例句"}),
            core.FEMALE_VOICE,
        )
        self.assertEqual(
            core.default_voice_for_item({"category": "语篇跟读", "voice": "male"}),
            core.MALE_VOICE,
        )

    def test_speaker_markers_map_to_amanda_and_george(self):
        self.assertEqual(
            core.parse_speakers("W: hello\nM: goodbye"),
            [(core.FEMALE_VOICE, "hello"), (core.MALE_VOICE, "goodbye")],
        )

    def test_named_roles_can_be_mapped_to_independent_voice_keys(self):
        segments = core.parse_speakers_with_roles(
            "Reporter: opening\nMr Yan: answer\nMs Wu: follow-up",
            role_voices={"Reporter": "speaker:reporter", "Mr Yan": "speaker:yan"},
        )
        self.assertEqual(
            segments,
            [
                ("Reporter", "speaker:reporter", "opening"),
                ("Mr Yan", "speaker:yan", "answer"),
                ("Ms Wu", core.FEMALE_VOICE, "follow-up"),
            ],
        )

    def test_unmapped_colon_text_stays_in_the_default_voice_segment(self):
        segments = core.parse_speakers_with_roles(
            "This is text: keep the colon\nThe next line remains ordinary.",
            role_voices={},
        )
        self.assertEqual(
            segments,
            [(None, core.FEMALE_VOICE, "This is text: keep the colon\nThe next line remains ordinary.")],
        )

    def test_each_voice_keeps_its_own_three_parameters(self):
        config = core.normalize_tts_config({
            "rate": 10,
            "volume": 20,
            "pitch": 30,
            "default_female_voice": "speaker:female",
            "default_male_voice": "speaker:male",
            "voice_configs": {
                "speaker:female": {"rate": 11, "volume": 12, "pitch": 13},
                "speaker:male": {"rate": 71, "volume": 72, "pitch": 73},
            },
            "role_voices": {"Reporter": "speaker:male"},
        })
        self.assertEqual(config["voice_configs"]["speaker:female"], {"rate": 11, "volume": 12, "pitch": 13})
        self.assertEqual(config["voice_configs"]["speaker:male"], {"rate": 71, "volume": 72, "pitch": 73})
        self.assertEqual(config["role_voices"], {"reporter": "speaker:male"})

    def test_same_voice_keeps_default_and_role_parameter_slots_independent(self):
        config = core.normalize_tts_config({
            "default_female_voice": "speaker:shared",
            "default_male_voice": "speaker:shared",
            "role_voices": {"Reporter": "speaker:shared"},
            "role_configs": {
                core.DEFAULT_FEMALE_ROLE_KEY: {"rate": 10, "volume": 20, "pitch": 30},
                core.DEFAULT_MALE_ROLE_KEY: {"rate": 40, "volume": 50, "pitch": 60},
                "role:Reporter": {"rate": 70, "volume": 80, "pitch": 90},
            },
        })

        self.assertEqual(
            config["role_configs"][core.DEFAULT_FEMALE_ROLE_KEY],
            {"rate": 10, "volume": 20, "pitch": 30},
        )
        self.assertEqual(
            config["role_configs"][core.DEFAULT_MALE_ROLE_KEY],
            {"rate": 40, "volume": 50, "pitch": 60},
        )
        self.assertEqual(
            config["role_configs"]["role:reporter"],
            {"rate": 70, "volume": 80, "pitch": 90},
        )


if __name__ == "__main__":
    unittest.main()

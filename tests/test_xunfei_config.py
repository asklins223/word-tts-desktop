from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import wordtts as core
import xunfei.config as xunfei_config


class XunfeiConfigTests(unittest.TestCase):
    def test_initialized_legacy_profile_wins_over_update_created_canonical_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir) / "WordTTS"
            legacy_dir = Path(temp_dir) / "legacy-profile"
            canonical_dir = base_dir / "xunfei_chrome_profile"

            legacy_state = legacy_dir / "Default" / "Cookies"
            legacy_state.parent.mkdir(parents=True)
            legacy_state.touch()
            canonical_state = canonical_dir / "Default" / "Cookies"
            canonical_state.parent.mkdir(parents=True)
            canonical_state.touch()

            self.assertEqual(
                xunfei_config._resolve_profile_dir(base_dir, legacy_dir),
                str(legacy_dir.absolute()),
            )

    def test_new_canonical_profile_is_used_when_legacy_profile_is_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir) / "WordTTS"
            legacy_dir = Path(temp_dir) / "legacy-profile"
            canonical_dir = base_dir / "xunfei_chrome_profile"
            (legacy_dir / "Default").mkdir(parents=True)
            (canonical_dir / "Default" / "Network").mkdir(parents=True)
            (canonical_dir / "Default" / "Network" / "Cookies").touch()

            self.assertEqual(
                xunfei_config._resolve_profile_dir(base_dir, legacy_dir),
                str(canonical_dir),
            )

    def test_new_install_without_either_profile_uses_canonical_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir) / "WordTTS"
            legacy_dir = Path(temp_dir) / "legacy-profile"

            self.assertEqual(
                xunfei_config._resolve_profile_dir(base_dir, legacy_dir),
                str(base_dir / "xunfei_chrome_profile"),
            )

    def test_windows_chrome_install_locations_are_checked_without_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            chrome = Path(temp_dir) / "Google" / "Chrome" / "Application" / "chrome.exe"
            chrome.parent.mkdir(parents=True)
            chrome.write_bytes(b"fixture")
            with mock.patch.object(xunfei_config.sys, "platform", "win32"), \
                    mock.patch.object(xunfei_config, "_CHROME_CANDIDATES", []), \
                    mock.patch.dict(
                        xunfei_config.os.environ,
                        {
                            "PROGRAMFILES": temp_dir,
                            "PROGRAMFILES(X86)": "",
                            "LOCALAPPDATA": "",
                            "USERPROFILE": "",
                        },
                        clear=False,
                    ), mock.patch.object(xunfei_config.shutil, "which", return_value=None):
                self.assertEqual(xunfei_config._find_chrome(), str(chrome))

    def test_staged_chromium_is_resolved_for_windows_packages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = (
                Path(temp_dir)
                / "chromium-1194"
                / "chrome-win"
                / "chrome.exe"
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture")
            with mock.patch.object(xunfei_config.sys, "platform", "win32"), \
                    mock.patch.dict(
                        xunfei_config.os.environ,
                        {"PLAYWRIGHT_BROWSERS_PATH": temp_dir},
                        clear=False,
                    ):
                self.assertEqual(
                    xunfei_config._find_bundled_chromium(),
                    str(executable),
                )

    def test_fallback_user_agent_matches_the_runtime_platform(self):
        with mock.patch.object(xunfei_config.sys, "platform", "win32"):
            self.assertIn("Windows NT 10.0", xunfei_config._platform_user_agent())

    def test_former_word_tts_facade_exports_remain_available_from_package(self):
        for name in (
            "_audio_dbfs",
            "_composite_item_from_spec",
            "_find_composite_silence_runs",
            "_normalize_voice_key",
            "_normalize_voice_params",
            "_stable_composite_work_id",
            "_synth_segment",
            "_trim_composite_edge_silence",
            "COMPOSITE_BOUNDARY_MS",
            "COMPOSITE_MAX_TEXT_LENGTH",
            "GENERATION_MODES",
            "export_audio",
            "now_str",
        ):
            with self.subTest(name=name):
                self.assertIn(name, core.__all__)
                self.assertTrue(hasattr(core, name))

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
        self.assertEqual(
            config["generation_mode"],
            core.GENERATION_MODE_COMPOSITE,
        )
        self.assertEqual(
            config["role_configs"][core.DEFAULT_FEMALE_ROLE_KEY],
            {"rate": 50, "pitch": 50, "volume": 50},
        )
        self.assertEqual(
            config["role_configs"][core.DEFAULT_MALE_ROLE_KEY],
            {"rate": 35, "pitch": 50, "volume": 50},
        )

    def test_generation_mode_accepts_legacy_single_segment_and_defaults_safely(self):
        self.assertEqual(
            core.normalize_tts_config({"generation_mode": core.GENERATION_MODE_SINGLE})[
                "generation_mode"
            ],
            core.GENERATION_MODE_SINGLE,
        )
        self.assertEqual(
            core.normalize_tts_config({"generation_mode": "unknown"})[
                "generation_mode"
            ],
            core.GENERATION_MODE_COMPOSITE,
        )

    def test_output_format_is_always_mp3_and_quality_does_not_switch_it(self):
        config = core.normalize_tts_config({
            "format": "wav",
            "quality": "无损（仅 wav 生效）",
        })

        self.assertEqual(config["format"], "mp3")
        self.assertEqual(config["quality"], "128 kbps（标准）")
        self.assertEqual(list(core.FORMAT_MAP), ["mp3"])
        self.assertNotIn("无损（仅 wav 生效）", core.QUALITY_BITRATE)

    def test_mp3_export_validation_uses_a_lightweight_header_check(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            valid = Path(temp_dir) / "valid.mp3"
            invalid = Path(temp_dir) / "invalid.mp3"
            valid.write_bytes(b"ID3" + b"\x00" * 32)
            invalid.write_bytes(b"not an audio file")

            self.assertTrue(core._looks_like_mp3_file(valid))
            self.assertFalse(core._looks_like_mp3_file(invalid))

    def test_multi_role_audio_segments_are_concatenated_without_repeated_pydub_copy(self):
        from pydub import AudioSegment

        first = AudioSegment.silent(duration=80)
        second = AudioSegment.silent(duration=120)
        combined = core._concat_audio_segments([first, second])

        self.assertEqual(len(combined), 200)
        self.assertEqual(combined.frame_rate, first.frame_rate)
        self.assertEqual(combined.channels, first.channels)

    def test_renderer_rebuilds_format_control_instead_of_falling_back_to_first_option(self):
        renderer = Path(__file__).resolve().parents[1] / "electron" / "renderer" / "app.js"
        source = renderer.read_text(encoding="utf-8")

        self.assertIn("format.replaceChildren(option)", source)
        self.assertIn("format: 'mp3'", source)
        self.assertNotIn("format: $('format').value", source)
        self.assertIn("composite_cut", source)
        self.assertIn("single_segment", source)

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

    def test_same_voice_migration_keeps_gender_specific_defaults(self):
        config = core.normalize_tts_config({
            "default_female_voice": "speaker:shared",
            "default_male_voice": "speaker:shared",
            "voice_configs": {"speaker:shared": {"volume": 60}},
        })

        self.assertEqual(
            config["role_configs"][core.DEFAULT_FEMALE_ROLE_KEY],
            {"rate": 50, "volume": 60, "pitch": 50},
        )
        self.assertEqual(
            config["role_configs"][core.DEFAULT_MALE_ROLE_KEY],
            {"rate": 35, "volume": 60, "pitch": 50},
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import math
import shutil
import tempfile
import unittest
import zipfile
from array import array
from collections import Counter
from unittest import mock
# 确保能找到 voice_training/ 目录下的模块
import sys as _sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(ROOT / "voice_training"))

from prepare_788_corpus import build_corpus, write_corpus
from validate_788_corpus import (
    AudioMetrics,
    CorpusValidationError,
    LOCKED_CORPUS_SHA256,
    _requirement_messages,
    analyze_pcm,
    inspect_rights_record,
    sha256_file,
    validate_corpus,
)


TARGET_AUDIO = ROOT / "788.mp3"
PROMPTS = ROOT / "voice_training" / "datasets" / "788" / "prompts" / "788_corpus.tsv"
META = ROOT / "voice_training" / "datasets" / "788" / "prompts" / "788_corpus.meta.json"


class CorpusPromptTests(unittest.TestCase):
    def test_locked_corpus_has_expected_unique_splits(self):
        corpus = build_corpus()
        split_counts = Counter(prompt.split for prompt in corpus)
        self.assertEqual(
            split_counts,
            {"train": 400, "validation": 40, "test": 40},
        )
        self.assertEqual(len({prompt.prompt_id for prompt in corpus}), 480)
        self.assertEqual(len({prompt.text.casefold() for prompt in corpus}), 480)
        self.assertFalse(
            any(
                phrase in prompt.text
                for prompt in corpus
                for phrase in ("At across ", "At behind ", "At beside ")
            )
        )
        self.assertEqual(sha256_file(PROMPTS), LOCKED_CORPUS_SHA256)

    def test_corpus_writer_is_reproducible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first_tsv, first_meta = write_corpus(Path(temp_dir))
            first_bytes = first_tsv.read_bytes()
            metadata = first_meta.read_text(encoding="utf-8")
            first_tsv.unlink()
            first_meta.unlink()
            second_tsv, second_meta = write_corpus(Path(temp_dir))
            self.assertEqual(first_bytes, second_tsv.read_bytes())
            self.assertEqual(metadata, second_meta.read_text(encoding="utf-8"))

    def test_validator_rejects_a_modified_locked_prompt_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prompts, metadata = write_corpus(root / "prompts")
            prompts.write_text(
                prompts.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(CorpusValidationError):
                validate_corpus(
                    prompts,
                    root / "audio",
                    meta_path=metadata,
                    rights_path=root / "SOURCE_AND_RIGHTS.md",
                )

    def test_rights_template_cannot_authorize_training(self):
        template = (
            ROOT
            / "voice_training"
            / "datasets"
            / "788"
            / "SOURCE_AND_RIGHTS_TEMPLATE.json"
        )
        record = inspect_rights_record(template)
        self.assertTrue(record["present"])
        self.assertFalse(record["complete"])
        self.assertTrue(
            any("training_authorized" in issue for issue in record["issues"])
        )

    def test_recommended_inherits_minimum_duration_and_seals_test(self):
        expected = Counter({"train": 400, "validation": 40, "test": 40})
        delivered = Counter({"train": 400, "validation": 40, "test": 1})
        active_seconds = Counter(
            {"train": 10 * 60, "validation": 1 * 60, "test": 10}
        )
        errors, _warnings = _requirement_messages(
            "recommended",
            expected,
            delivered,
            active_seconds,
            {"train": [], "validation": [], "test": ["te_0002"]},
        )
        self.assertTrue(any("30 分钟" in error for error in errors))
        self.assertTrue(any("test" in error for error in errors))

    def test_all_advanced_levels_inherit_each_minimum_duration_gate(self):
        expected = Counter({"train": 400, "validation": 40, "test": 40})
        for level, delivered, active_seconds in (
            (
                "recommended",
                Counter({"train": 400, "validation": 40}),
                Counter({"train": 29 * 60, "validation": 1 * 60}),
            ),
            (
                "complete",
                Counter({"train": 400, "validation": 40, "test": 40}),
                Counter(
                    {
                        "train": 29 * 60,
                        "validation": 1 * 60,
                        "test": 5 * 60,
                    }
                ),
            ),
        ):
            with self.subTest(level=level):
                errors, _warnings = _requirement_messages(
                    level,
                    expected,
                    delivered,
                    active_seconds,
                    {"train": [], "validation": [], "test": []},
                )
                self.assertTrue(
                    any("validation" in error and "3 分钟" in error for error in errors)
                )


class CorpusAudioTests(unittest.TestCase):
    def test_pcm_metrics_detect_leading_and_trailing_silence(self):
        sample_rate = 16000
        samples = array("h")
        samples.extend([0] * round(sample_rate * 0.2))
        samples.extend(
            round(8192 * math.sin(2 * math.pi * 440 * index / sample_rate))
            for index in range(round(sample_rate * 1.6))
        )
        samples.extend([0] * round(sample_rate * 0.2))
        metrics = analyze_pcm(samples.tobytes(), sample_rate)
        self.assertAlmostEqual(metrics["leading_silence_seconds"], 0.2, delta=0.03)
        self.assertAlmostEqual(metrics["trailing_silence_seconds"], 0.2, delta=0.03)
        self.assertAlmostEqual(metrics["peak_dbfs"], -12.04, delta=0.1)
        self.assertEqual(metrics["clipping_ratio"], 0)

    @unittest.skipUnless(TARGET_AUDIO.exists(), "788 target audio is required")
    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe is required")
    def test_reference_audio_passes_partial_signal_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_dir = Path(temp_dir)
            shutil.copyfile(TARGET_AUDIO, audio_dir / "tr_0001.mp3")
            report = validate_corpus(PROMPTS, audio_dir, level="partial")
            self.assertTrue(report["signal_preflight_passed"])
            self.assertFalse(report["passed"])
            self.assertFalse(report["training_admission"]["passed"])
            self.assertEqual(report["summary"]["delivered_count"]["train"], 1)
            self.assertEqual(report["summary"]["files_with_errors"], 0)
            metrics = report["entries"][0]["metrics"]
            self.assertEqual(metrics["sample_rate_hz"], 16000)
            self.assertEqual(metrics["channels"], 1)
            self.assertGreater(metrics["active_seconds"], 4)

    def test_test_audio_is_not_decoded_without_a_valid_reveal_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_dir = root / "audio"
            audio_dir.mkdir()
            (audio_dir / "te_0001.wav").write_bytes(b"not-a-real-wave")
            with (
                mock.patch("validate_788_corpus.inspect_audio") as inspect,
                mock.patch(
                    "validate_788_corpus.inspect_archived_audio"
                ) as inspect_archived,
            ):
                report = validate_corpus(
                    PROMPTS,
                    audio_dir,
                    meta_path=META,
                    rights_path=root / "missing-rights.json",
                    frozen_run_path=root / "missing-frozen-run.json",
                    level="complete",
                    reveal_test=False,
                )
            inspect.assert_not_called()
            inspect_archived.assert_not_called()
            self.assertFalse(
                report["sealed_test_package"]["scan_authorized"]
            )
            self.assertEqual(report["summary"]["delivered_count"]["test"], 0)

    def test_complete_scans_the_frozen_zip_and_verifies_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_dir = root / "audio"
            audio_dir.mkdir()
            rights_path = root / "SOURCE_AND_RIGHTS.json"
            rights_path.write_text(
                json.dumps(
                    {
                        "provided_by": "test",
                        "provided_at": "2026-07-28T09:00:00+08:00",
                        "source_provider": "authorized test fixture",
                        "voice_name": "Alfie",
                        "voice_id": "788",
                        "training_authorized": True,
                        "authorization_basis": "test fixture",
                        "native_audio": {
                            "format": "wav",
                            "sample_rate_hz": 24000,
                        },
                        "synthesis_settings": {
                            "rate": "default",
                            "pitch": "default",
                            "volume": "default",
                            "style": "default",
                        },
                        "postprocessed": False,
                        "signer": "test signer",
                        "signed_at": "2026-07-28",
                    }
                ),
                encoding="utf-8",
            )
            package_path = root / "788_test_v1.zip"
            with zipfile.ZipFile(
                package_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for index in range(1, 41):
                    archive.writestr(
                        f"788_test_v1/audio/te_{index:04d}.wav",
                        f"locked-test-audio-{index}".encode(),
                    )

            artifact_fields = {
                "training_manifest": "approved_train_manifest.jsonl",
                "model": "model.bin",
                "index": "retrieval.index",
                "config": "config.json",
                "thresholds": "thresholds.json",
                "evaluator_bundle": "evaluators.zip",
            }
            frozen_payload = {
                "frozen_at": "2026-07-28T10:00:00+08:00",
                "corpus_sha256": LOCKED_CORPUS_SHA256,
                "sealed_test_package_sha256": sha256_file(package_path),
            }
            for label, filename in artifact_fields.items():
                artifact_path = root / filename
                artifact_path.write_bytes(f"frozen-{label}".encode())
                frozen_payload[f"{label}_path"] = filename
                frozen_payload[f"{label}_sha256"] = sha256_file(artifact_path)
            frozen_run_path = root / "frozen_run.json"
            frozen_run_path.write_text(
                json.dumps(frozen_payload),
                encoding="utf-8",
            )

            call_index = 0

            def fake_metrics(_package: Path, _member: str) -> AudioMetrics:
                nonlocal call_index
                call_index += 1
                return AudioMetrics(
                    duration_seconds=5.0,
                    probe_duration_seconds=5.0,
                    active_seconds=4.0,
                    sample_rate_hz=24000,
                    channels=1,
                    codec="pcm_s16le",
                    bitrate_bps=384000,
                    peak_dbfs=-3.0,
                    rms_dbfs=-18.0,
                    clipping_ratio=0.0,
                    dc_offset_ratio=0.0,
                    leading_silence_seconds=0.1,
                    trailing_silence_seconds=0.1,
                    active_frame_ratio=0.8,
                    sha256=f"{call_index:064x}",
                    pcm_sha256=f"{call_index + 1000:064x}",
                )

            with mock.patch(
                "validate_788_corpus.inspect_archived_audio",
                side_effect=fake_metrics,
            ) as inspect_archived:
                report = validate_corpus(
                    PROMPTS,
                    audio_dir,
                    meta_path=META,
                    rights_path=rights_path,
                    frozen_run_path=frozen_run_path,
                    sealed_test_package_path=package_path,
                    level="complete",
                    reveal_test=True,
                )
            self.assertEqual(inspect_archived.call_count, 40)
            self.assertTrue(
                report["sealed_test_package"]["scan_authorized"]
            )
            self.assertEqual(
                report["summary"]["delivered_count"]["test"],
                40,
            )
            self.assertTrue(
                all(
                    record["verified"]
                    for record in report["frozen_run_record"][
                        "artifacts"
                    ].values()
                )
            )

            (root / "model.bin").write_bytes(b"changed-after-freeze")
            with mock.patch(
                "validate_788_corpus.inspect_archived_audio"
            ) as blocked_scan:
                blocked_report = validate_corpus(
                    PROMPTS,
                    audio_dir,
                    meta_path=META,
                    rights_path=rights_path,
                    frozen_run_path=frozen_run_path,
                    sealed_test_package_path=package_path,
                    level="complete",
                    reveal_test=True,
                )
            blocked_scan.assert_not_called()
            self.assertFalse(
                blocked_report["sealed_test_package"]["scan_authorized"]
            )
            self.assertTrue(
                any(
                    "冻结产物哈希不一致" in error
                    for error in blocked_report["global_errors"]
                )
            )


if __name__ == "__main__":
    unittest.main()

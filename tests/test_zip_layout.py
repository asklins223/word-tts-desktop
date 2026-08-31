"""ZIP exports keep every deliverable below one top-level folder."""

import tempfile
import zipfile
from pathlib import Path

from wordtts.progress import create_zip


def test_legacy_zip_wraps_audio_and_manifests_in_one_folder():
    with tempfile.TemporaryDirectory(prefix="wordtts-zip-layout-") as temp_dir:
        session_dir = Path(temp_dir)
        audio_path = session_dir / "source-audio.mp3"
        audio_path.write_bytes(b"audio")
        (session_dir / "parsed.json").write_text("{}", encoding="utf-8")
        progress = {
            "source_file": "test-paper.docx",
            "created_at": "2026-01-01T00:00:00",
            "completed": 1,
            "failed": 0,
            "total_items": 1,
            "config": {},
            "items": [{
                "filename": "听后选择-1.mp3",
                "status": "done",
                "output_path": str(audio_path),
                "doc_type": "听后选择",
                "category": "听后选择录音稿",
            }],
        }

        zip_path = create_zip(str(session_dir), progress)

        with zipfile.ZipFile(zip_path) as archive:
            assert archive.namelist() == [
                "audio/",
                "audio/听后选择-1.mp3",
                "audio/parsed.json",
                "audio/manifest.json",
            ]

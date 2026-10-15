"""Full-fidelity repro of the Xunfei download-page Chrome crash.

Runs the app's *real* composite pipeline — editor page, submission, then the
download page wait — against a throw-away copy of the automation profile.
That is the only way to reproduce, because the works-list API is
authenticated with a token harvested from the editor page's network traffic.

Submits one real (very short) work.  Read-only afterwards.

    python tools/repro_xunfei_full.py [system|chromium]
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SOURCE_PROFILE = Path.home() / ".xunfei_chrome_profile"
# Deliberately tiny: one work, one segment, a few characters.
WORKS = [{
    "work_id": "repro:1",
    "items": [{"segments": [{"voice_key": "amanda", "text": "测试。"}]}],
}]


def prepare_profile():
    target = Path(tempfile.mkdtemp(prefix="xunfei-full-profile-"))
    profile = target / "profile"
    shutil.copytree(SOURCE_PROFILE, profile, symlinks=True)
    for name in ("SingletonCookie", "SingletonLock", "SingletonSocket"):
        link = profile / name
        if link.is_symlink() or link.exists():
            link.unlink()
    return profile


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "system"

    import xunfei.config as config
    import xunfei.runtime as runtime
    import xunfei.session as session

    profile = prepare_profile()
    session.PROFILE_DIR = str(profile)
    session.mark_system_chrome_suspect = lambda *_a, **_k: None

    chrome = config._find_chrome()
    bundled = config._find_bundled_chromium() or _bundled_in_app()
    target = chrome if which == "system" else bundled
    print(f"[full] 目标浏览器: {target}", flush=True)
    if not target:
        print("[full] 目标浏览器不可用", flush=True)
        return 1
    session._find_chrome = lambda: target
    session._find_bundled_chromium = (lambda: None) if which == "system" else (lambda: target)

    async def run():
        started = time.monotonic()
        try:
            result = await runtime.synth_xunfei_composite(WORKS)
            elapsed = time.monotonic() - started
            print(f"[full] {elapsed:.0f}s 后正常返回: {result}", flush=True)
            print("[full] 结论: 全流程没有崩溃", flush=True)
            return 0
        except Exception as error:
            elapsed = time.monotonic() - started
            print(f"[full] {elapsed:.0f}s 后异常: {type(error).__name__}: {error}", flush=True)
            return 1
        finally:
            try:
                await runtime.close_session()
            except Exception:
                pass
            shutil.rmtree(profile.parent, ignore_errors=True)

    return asyncio.run(run())


def _bundled_in_app():
    import glob

    tail = ("/小猪wordTTS.app/Contents/Resources/server_backend/_internal"
            "/playwright_browsers/chromium-*/chrome-mac"
            "/Chromium.app/Contents/MacOS/Chromium")
    found = []
    for pattern in ("/Volumes/*/*" + tail, "/Volumes/*" + tail):
        found.extend(sorted(glob.glob(pattern)))
    for path in reversed(found):
        probe = os.path.join(os.path.dirname(path), f".w-{os.getpid()}")
        try:
            open(probe, "w").close()
            os.remove(probe)
        except OSError:
            continue
        return path
    return None


if __name__ == "__main__":
    raise SystemExit(main())

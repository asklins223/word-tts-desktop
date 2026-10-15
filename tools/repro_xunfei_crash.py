"""Reproduce the Xunfei download-page Chrome crash and bisect the browser.

Drives the *real* download code path (no submission, read-only) against a
throw-away copy of the automation profile, once with the system Chrome and
once with the bundled Chromium, so the two can be compared on the same page.

Usage:
    python tools/repro_xunfei_crash.py system      # system Chrome
    python tools/repro_xunfei_crash.py chromium    # bundled Chromium
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SOURCE_PROFILE = Path.home() / ".xunfei_chrome_profile"
DOWNLOAD_PAGE = "https://peiyin.xunfei.cn/user"
# The work that is sitting on the download page right now.  Read-only: the
# download path never submits anything.
# A real, already-submitted work from the 09-28 07:17 run that succeeded.
# Using a real worksId matters: with a fake one the download path finds no
# target row and returns early, so it never reaches row selection or the
# "下载" click — which is exactly the code path the crash lives in.
PROBE_WORKS = {
    "job_id": "repro",
    "works_id": os.environ.get("WORDTTS_REPRO_WORKS_ID", "0928a84418d5fdd44b39bb7e6c2a8abb335b"),
    "works_name": os.environ.get("WORDTTS_REPRO_WORKS_NAME", "wordtts_composite_0001"),
    "output_path": str(Path(tempfile.gettempdir()) / "repro-probe.mp3"),
}


def prepare_profile():
    """Copy the automation profile so the live one is never touched."""
    target = Path(tempfile.mkdtemp(prefix="xunfei-repro-profile-"))
    profile = target / "profile"
    shutil.copytree(SOURCE_PROFILE, profile, symlinks=True)
    # A copied profile carries the source's singleton/lock symlinks, which
    # would point at the original browser and make Chrome refuse to start.
    for name in ("SingletonCookie", "SingletonLock", "SingletonSocket"):
        link = profile / name
        if link.is_symlink() or link.exists():
            link.unlink()
    return profile


def _bundled_chromium_in_app():
    """Locate the Chromium staged inside the packaged app.

    ``_find_bundled_chromium()`` only resolves from a packaged (frozen) run;
    from a source checkout it returns None, so point at the built app
    directly when reproducing from the repo.
    """
    import glob

    tail = ("/小猪wordTTS.app/Contents/Resources/server_backend/_internal"
            "/playwright_browsers/chromium-*/chrome-mac"
            "/Chromium.app/Contents/MacOS/Chromium")
    patterns = (
        # The installed copy, e.g. /Volumes/<disk>/app/小猪wordTTS.app.
        "/Volumes/*/*" + tail,
        str(Path.home() / ("*" + tail)),
        "/Volumes/*" + tail,   # the read-only installer image
    )
    found = []
    for pattern in patterns:
        found.extend(sorted(glob.glob(pattern)))
    for path in reversed(found):
        if _on_writable_volume(path):
            return path
    # Never fall back to the read-only image: Chrome cannot run from it.
    return None


def _on_writable_volume(executable):
    """Reject executables that live on the read-only installer image.

    ``os.access(dir, os.W_OK)`` only inspects permission bits, so it happily
    reports True on a read-only mount.  Actually create and remove a file.
    """
    directory = os.path.dirname(executable)
    probe = os.path.join(directory, f".repro-write-{os.getpid()}")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.remove(probe)
    except OSError:
        return False
    return os.access(executable, os.R_OK)


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "system"
    if which not in {"system", "chromium"}:
        print(__doc__)
        return 2

    import xunfei.config as config
    import xunfei.session as session

    profile = prepare_profile()
    session.PROFILE_DIR = str(profile)
    # Never mark the real system Chrome suspect from a throw-away probe.
    session.mark_system_chrome_suspect = lambda *_a, **_k: None

    chrome_path = config._find_chrome()
    bundled = config._find_bundled_chromium() or _bundled_chromium_in_app()
    target = chrome_path if which == "system" else bundled
    print(f"[repro] 目标浏览器: {target}", flush=True)
    if not target:
        print(f"[repro] {which} 不可用，无法复现", flush=True)
        return 1

    real_find = config._find_chrome
    config._find_chrome = lambda: target
    session._find_chrome = lambda: target
    session._find_bundled_chromium = lambda: target
    if which == "system":
        # Keep the bundled one out of the candidate list so the run really
        # exercises the system Chrome and cannot silently fall back.
        session._find_bundled_chromium = lambda: None

    sess = session.XunFeiSession()
    print("[repro] 启动浏览器并复用 profile 里的登录态…", flush=True)
    try:
        sess.login(login_timeout=120)
    except Exception as error:
        print(f"[repro] 登录/启动阶段失败: {error}", flush=True)
        return 1
    print(f"[repro] 已登录: {sess._logged_in}", flush=True)

    print("[repro] 进入下载页并按 App 的节奏轮询作品列表（只读，不提交）…", flush=True)
    started = time.monotonic()
    try:
        result = sess._download_pending_batch([PROBE_WORKS])
        print(f"[repro] 下载路径正常返回（{time.monotonic() - started:.0f}s）: {result}", flush=True)
        print("[repro] 结论: 没有崩溃", flush=True)
        return 0
    except Exception as error:
        elapsed = time.monotonic() - started
        print(f"[repro] {elapsed:.0f}s 后异常: {type(error).__name__}: {error}", flush=True)
        return 1
    finally:
        try:
            sess.close()
        except Exception:
            pass
        config._find_chrome = real_find
        shutil.rmtree(profile.parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the reproducible local 2A workflow gate.

This gate is intentionally deterministic and has no real-provider side effect.
It records command output and the two explicit schema profiles so a failed
release can be diagnosed without opening the user's runtime database.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
ELECTRON = ROOT / "electron"


def _command(label: str, args: Sequence[str], *, cwd: Path = ROOT) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = subprocess.run(
            list(args),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=900,
            check=False,
        )
        output = result.stdout or ""
        return {
            "label": label,
            "command": list(args),
            "cwd": str(cwd),
            "status": "PASS" if result.returncode == 0 else "FAIL",
            "returncode": result.returncode,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "output_tail": output[-6000:],
        }
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return {
            "label": label,
            "command": list(args),
            "cwd": str(cwd),
            "status": "FAIL",
            "returncode": 124,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "output_tail": (output + "\ncommand timed out")[-6000:],
        }
    except OSError as exc:
        return {
            "label": label,
            "command": list(args),
            "cwd": str(cwd),
            "status": "FAIL",
            "returncode": 127,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "output_tail": str(exc),
        }


def run_gate(*, include_electron: bool = True) -> dict[str, Any]:
    python = sys.executable
    checks: list[dict[str, Any]] = [
        _command("migration-check-2a", [python, "db/migration_runner.py", "--check", "--profile", "2a"]),
        _command("migration-check-full", [python, "db/migration_runner.py", "--check", "--profile", "full"]),
        _command("schema-check-2a", [python, "db/schema_checks.py", "--profile", "2a"]),
        _command("schema-check-full", [python, "db/schema_checks.py", "--profile", "full"]),
        _command("python-tests", [python, "-m", "unittest", "discover", "-s", "tests", "-q"]),
        _command("process-kill-recovery", [python, "tools/process_recovery_probe.py"]),
        _command(
            "logical-xunfei-smoke",
            [
                python,
                "tools/xunfei_smoke.py",
                "--logical-only",
                "--source",
                "examples/documents/信息转述及询问信息 7上- U1.docx",
                "--max-items",
                "100",
            ],
        ),
        _command("python-compile", [
            python, "-m", "py_compile",
            *[str(path.relative_to(ROOT)) for parent in ("api", "application", "workflow")
              for path in sorted((ROOT / parent).glob("*.py"))],
        ]),
    ]
    if include_electron:
        checks.extend([
            _command("electron-tests", ["npm", "test"], cwd=ELECTRON),
            _command("contract-check", ["npm", "run", "check:contracts"], cwd=ELECTRON),
            _command("electron-main-syntax", ["node", "--check", "main.js"], cwd=ELECTRON),
            _command("electron-preload-syntax", ["node", "--check", "preload.js"], cwd=ELECTRON),
            _command("electron-renderer-syntax", ["node", "--check", "renderer/app.js"], cwd=ELECTRON),
        ])
    passed = all(item["status"] == "PASS" for item in checks)
    node_version = _version("node", ["node", "--version"])
    open_items = [
        "Real Xunfei smoke is intentionally not part of this local gate.",
        "Platform-specific fsync and low-end-device thresholds require target hardware.",
    ]
    if not node_version.startswith("v24."):
        open_items.insert(0, f"Node.js 24 is required; this gate detected {node_version}.")
    return {
        "gate": "2A_LOCAL",
        "status": "PASS" if passed else "FAIL",
        "side_effect_policy": "REAL_PROVIDER_DISABLED",
        "runtime": {
            "python": platform.python_version(),
            "node": node_version,
            "platform": platform.platform(),
        },
        "checks": checks,
        "open_items": open_items,
    }


def _version(label: str, args: Sequence[str]) -> str:
    try:
        return subprocess.check_output(list(args), cwd=ROOT, text=True, stderr=subprocess.STDOUT).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"{label}: unavailable ({exc})"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-electron", action="store_true", help="skip Node/Electron checks")
    parser.add_argument("--output", type=Path, help="write the JSON gate report to this path")
    args = parser.parse_args(argv)
    report = run_gate(include_electron=not args.no_electron)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.output:
        destination = args.output.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

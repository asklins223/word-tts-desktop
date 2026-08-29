#!/usr/bin/env python3
"""Check release-boundary invariants without touching user data.

The default report is deliberately strict about evidence that cannot be
created safely on this development host: Node 24, a real Xunfei smoke, and
target-device durability/performance.  A small, explicit waiver list marks
the items the product owner has decided not to block on; each waiver keeps
its reason in the report for auditability.  ``--no-waivers`` restores the
strict gate, and ``--allow-open`` is useful for the local code gate but must
not be used as a support claim.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# 产品负责人豁免的发布开放项：不阻塞发布门，但原因必须留痕。
# 恢复额度/拿到目标设备后应删除对应豁免并补做实测。
CHECK_WAIVERS = {
    "real-xunfei-smoke-evidence": (
        "real account smoke waived by product owner: the current Xunfei "
        "account has no quota, so a controlled real-account smoke cannot be "
        "scheduled; informal field usage is not accepted as support evidence. "
        "Re-run the smoke when quota is available."
    ),
    "target-device-performance": (
        "target-device field evidence waived by product owner: only the "
        "development-host baseline exists. Re-measure on target hardware "
        "before making a minimum-spec support claim."
    ),
}


def _check(label: str, passed: bool, detail: str, *, status: str | None = None) -> dict[str, Any]:
    return {
        "label": label,
        "status": status or ("PASS" if passed else "FAIL"),
        "passed": bool(passed),
        "detail": detail,
    }


def _node_version() -> tuple[int, str]:
    try:
        value = subprocess.check_output(["node", "--version"], cwd=ROOT, text=True, stderr=subprocess.STDOUT).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        return 0, f"unavailable ({exc})"
    match = re.match(r"v(\d+)(?:\.|$)", value)
    return (int(match.group(1)) if match else 0), value


def _forbidden_scan(path: Path, needles: Sequence[str]) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read {path}: {exc}"]
    return [needle for needle in needles if needle in text]


def run_release_gate(*, apply_waivers: bool = True) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    package_path = ROOT / "electron" / "package.json"
    lock_path = ROOT / "electron" / "package-lock.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        package_version = str(package.get("version") or "")
        lock_version = str(lock.get("version") or "")
        checks.append(_check("package-version-source", bool(package_version and package_version == lock_version),
                             f"package.json={package_version!r}, package-lock.json={lock_version!r}"))
    except (OSError, ValueError) as exc:
        checks.append(_check("package-version-source", False, str(exc)))

    data_format_path = ROOT / "DATA_FORMAT_VERSION"
    try:
        file_version = data_format_path.read_text(encoding="utf-8").strip()
        from workflow.version import DATA_FORMAT_VERSION

        checks.append(_check("data-format-version", file_version == DATA_FORMAT_VERSION and bool(file_version),
                             f"file={file_version!r}, module={DATA_FORMAT_VERSION!r}"))
    except (OSError, ImportError) as exc:
        checks.append(_check("data-format-version", False, str(exc)))

    forbidden = {
        "renderer": (ROOT / "electron" / "renderer" / "app.js", (
            "?token=", "EventSource", "history_id", "zip_path", "file_path",
            "saveFileByPath", "apiUrl", "backend.url", "backend.token", "/api/",
        )),
        "preload": (ROOT / "electron" / "preload.js", (
            "X-WordTTS-Token", "?token=", "filePath", "backend.url", "backend.token",
        )),
        "proxy": (ROOT / "electron" / "workflow-proxy.js", (
            "?token=", "X-WordTTS-Token", "EventSource",
        )),
    }
    for label, (path, needles) in forbidden.items():
        hits = _forbidden_scan(path, needles)
        checks.append(_check(f"forbidden-{label}", not hits, "clean" if not hits else f"found: {hits}"))

    main_text = (ROOT / "electron" / "main.js").read_text(encoding="utf-8")
    checks.append(_check("single-instance-lock", "requestSingleInstanceLock()" in main_text,
                         "Electron requests the process-wide single-instance lock"))
    checks.append(_check("versioned-startup-probe", "/api/v1/health" in main_text and "X-Desktop-Capability" in main_text,
                         "startup probe uses versioned path and capability header"))
    real_provider_gate = (
        "const realProviderEnabled = !isSmokeTest;" in main_text
        and "WORDTTS_ENABLE_REAL_PROVIDER: realProviderEnabled ? '1' : '0'" in main_text
    )
    checks.append(_check(
        "real-provider-default-on",
        real_provider_gate,
        "formal Electron enables the real provider by default; --smoke-test remains offline"
        if real_provider_gate else "formal Electron real provider default is not enabled",
    ))

    server_text = (ROOT / "server.py").read_text(encoding="utf-8")
    checks.append(_check("legacy-api-retirement-code", "API_VERSION_RETIRED" in server_text and "_legacy_api_enabled" in server_text,
                         "legacy API has a production 410 path"))
    backend_real_provider_gate = (
        'os.environ.get("WORDTTS_ENABLE_REAL_PROVIDER", "1") != "0"' in server_text
        and '"--disable-real-provider"' in server_text
    )
    checks.append(_check(
        "backend-real-provider-default-on",
        backend_real_provider_gate,
        "backend defaults to real provider on; --disable-real-provider is explicit offline opt-out"
        if backend_real_provider_gate
        else "backend real provider default is not enabled",
    ))
    previous = os.environ.get("WORDTTS_LEGACY_API")
    try:
        os.environ["WORDTTS_LEGACY_API"] = "0"
        from fastapi.testclient import TestClient
        import server

        response = TestClient(server.app).get("/api/config")
        checks.append(_check("legacy-api-410", response.status_code == 410 and response.json().get("error_code") == "API_VERSION_RETIRED",
                             f"GET /api/config -> HTTP {response.status_code}"))
    except Exception as exc:  # pragma: no cover - environment-specific import failure is reported.
        checks.append(_check("legacy-api-410", False, f"runtime probe failed: {exc}"))
    finally:
        if previous is None:
            os.environ.pop("WORDTTS_LEGACY_API", None)
        else:
            os.environ["WORDTTS_LEGACY_API"] = previous

    node_major, node_value = _node_version()
    checks.append(_check(
        "node-24",
        node_major >= 24,
        f"detected {node_value}; Node 24 is required for support",
        status="PASS" if node_major >= 24 else "OPEN",
    ))

    smoke_report = ROOT / "docs" / "real-xunfei-smoke-report.md"
    smoke_text = smoke_report.read_text(encoding="utf-8") if smoke_report.exists() else ""
    smoke_done = "STATUS: PASS" in smoke_text
    checks.append(_check("real-xunfei-smoke-evidence", smoke_done,
                         "real smoke evidence is present" if smoke_done else "real account smoke has not been executed",
                         status="PASS" if smoke_done else "OPEN"))

    logical_report = ROOT / "docs" / "logical-xunfei-smoke-report.json"
    try:
        logical = json.loads(logical_report.read_text(encoding="utf-8"))
        logical_safe = (
            logical.get("status") == "PASS"
            and logical.get("side_effect_policy") == "LOGICAL_ONLY_NO_NETWORK"
            and logical.get("network_calls") == 0
            and logical.get("page_calls") == 0
        )
        logical_detail = "page-free logical smoke passed with network_calls=0 and page_calls=0"
    except (OSError, ValueError) as exc:
        logical_safe = False
        logical_detail = f"logical smoke evidence unavailable: {exc}"
    checks.append(_check("logical-xunfei-smoke", logical_safe, logical_detail,
                         status="PASS" if logical_safe else "FAIL"))

    performance_report = ROOT / "docs" / "performance-baseline.md"
    performance_text = performance_report.read_text(encoding="utf-8") if performance_report.exists() else ""
    target_model_done = (
        "STATUS: PASS" in performance_text
        and "TARGET_DEVICE_STATUS: SIMULATED" in performance_text
        and '"hardware_claim": false' in performance_text
    )
    checks.append(_check("target-device-model", target_model_done,
                         "target-device model simulation is evidenced without a hardware claim"
                         if target_model_done else "target-device model simulation evidence is unavailable",
                         status="PASS" if target_model_done else "FAIL"))
    target_hardware_done = "TARGET_DEVICE_STATUS: PASS" in performance_text
    checks.append(_check("target-device-performance", target_hardware_done,
                         "target-device thresholds are evidenced" if target_hardware_done else "development-host baseline only",
                         status="PASS" if target_hardware_done else "OPEN"))

    hard_failures = [item for item in checks if item["status"] == "FAIL"]
    open_items = [item for item in checks if item["status"] == "OPEN"]
    waived_items: list[dict[str, Any]] = []
    if apply_waivers:
        for item in checks:
            waiver = CHECK_WAIVERS.get(item["label"])
            if waiver and item["status"] == "OPEN":
                item["status"] = "WAIVED"
                item["waiver"] = waiver
                waived_items.append({
                    "label": item["label"],
                    "detail": item["detail"],
                    "waiver": waiver,
                })
        open_items = [item for item in checks if item["status"] == "OPEN"]
    return {
        "gate": "RELEASE",
        "status": "PASS" if not hard_failures and not open_items else "OPEN",
        "release_ready": not hard_failures and not open_items,
        "checks": checks,
        "open_items": [item["detail"] for item in open_items],
        "waived_items": waived_items,
        "hard_failures": [item["detail"] for item in hard_failures],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-open", action="store_true", help="return success for code-clean but externally open checks")
    parser.add_argument("--no-waivers", action="store_true", help="keep waived checks OPEN so the strict gate is restored")
    parser.add_argument("--output", type=Path, help="write the JSON report to this path")
    args = parser.parse_args(argv)
    report = run_release_gate(apply_waivers=not args.no_waivers)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.output:
        destination = args.output.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    if report["hard_failures"]:
        return 1
    return 0 if args.allow_open or report["release_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Measure a bounded local FakeProvider workflow baseline.

This is a development-host baseline, not a low-end-device support claim.  It
keeps the workload small and deterministic, and never contacts a Provider.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import sys
import tempfile
import time
from pathlib import Path
from statistics import mean, quantiles
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_baseline(*, items: int = 10, runs: int = 3) -> dict[str, Any]:
    if items < 1 or runs < 1:
        raise ValueError("items and runs must be positive")
    from workflow.artifact_store import ArtifactStore
    from workflow.database import WorkflowDatabase
    from workflow.engine import WorkflowEngine
    from workflow.fake_provider import FakeProvider
    from workflow.repositories import WorkflowRepository

    timings: list[float] = []
    artifact_counts: list[int] = []
    peak_rss: list[int] = []
    with tempfile.TemporaryDirectory(prefix="wordtts-performance-") as temporary:
        root = Path(temporary)
        database = WorkflowDatabase(root / "workflow.db", profile="2a")
        database.initialize()
        try:
            store = ArtifactStore(root / "artifacts", max_bytes=64 * 1024 * 1024)
            repository = WorkflowRepository(database)
            engine = WorkflowEngine(repository, store)
            for run in range(runs):
                workflow = repository.create_workflow(
                    "tts",
                    {"generation_mode": "composite_cut", "benchmark_run": run},
                )
                for index in range(items):
                    repository.create_item(
                        workflow.workflow_id,
                        item_type="sentence",
                        sequence=index,
                        normalized_content=f"benchmark item {run}-{index}",
                        item_identity_key=f"benchmark:{run}:{index}",
                        role="default",
                        voice_key="fake",
                    )
                started = time.perf_counter()
                result = engine.run_tts(workflow.workflow_id, FakeProvider())
                elapsed = time.perf_counter() - started
                if result.status != "SUCCEEDED":
                    raise RuntimeError(f"benchmark workflow did not succeed: {result.status}")
                timings.append(elapsed)
                artifact_counts.append(len(result.artifact_ids))
                peak_rss.append(int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
        finally:
            database.close()
    sorted_timings = sorted(timings)
    p95 = quantiles(sorted_timings, n=20, method="inclusive")[18] if len(sorted_timings) >= 2 else sorted_timings[0]
    return {
        "status": "PASS",
        "classification": "DEVELOPMENT_HOST_BASELINE_ONLY",
        "target_device_status": "OPEN",
        "side_effect_policy": "FAKE_PROVIDER_ONLY",
        "workload": {"items_per_run": items, "runs": runs},
        "latency_seconds": {
            "mean": round(mean(timings), 6),
            "min": round(min(timings), 6),
            "max": round(max(timings), 6),
            "p95": round(p95, 6),
        },
        "artifacts_per_run": sorted(set(artifact_counts)),
        "peak_rss_raw": sorted(set(peak_rss)),
        "resource_policy": {"max_active": 1, "max_queue": 4, "audio": "on-demand artifact ticket"},
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "pid": os.getpid(),
        },
    }


def run_simulated_target(
    *,
    items: int = 10,
    runs: int = 3,
    cpu_slowdown_factor: float = 2.0,
    io_latency_ms: float = 8.0,
    memory_budget_mb: int = 512,
) -> dict[str, Any]:
    """Estimate a conservative target profile without claiming hardware evidence."""

    if cpu_slowdown_factor < 1 or io_latency_ms < 0 or memory_budget_mb < 64:
        raise ValueError("simulation factors and memory budget are invalid")
    base = run_baseline(items=items, runs=runs)
    io_penalty = (max(0, items) * io_latency_ms) / 1000.0
    base_latency = base["latency_seconds"]
    simulated_latency = {
        key: round(float(value) * cpu_slowdown_factor + io_penalty, 6)
        for key, value in base_latency.items()
    }
    simulated_peak_rss = max(base.get("peak_rss_raw") or [0])
    memory_budget_bytes = memory_budget_mb * 1024 * 1024
    memory_budget_fit = simulated_peak_rss <= memory_budget_bytes
    return {
        **base,
        "status": "PASS" if base["status"] == "PASS" and memory_budget_fit else "FAIL",
        "classification": "SIMULATED_TARGET_MODEL",
        "target_device_status": "SIMULATED",
        "latency_seconds": simulated_latency,
        "simulation": {
            "cpu_slowdown_factor": cpu_slowdown_factor,
            "io_latency_ms_per_item": io_latency_ms,
            "memory_budget_mb": memory_budget_mb,
            "memory_budget_fit": memory_budget_fit,
            "memory_budget_bytes": memory_budget_bytes,
            "peak_rss_observed_on_development_host": simulated_peak_rss,
            "hardware_claim": False,
        },
    }


def _markdown(report: dict[str, Any]) -> str:
    title = "性能基线（目标设备模型模拟）" if report.get("target_device_status") == "SIMULATED" else "性能基线（开发机）"
    caveat = (
        "本报告是基于开发机实测结果的保守模型模拟，不是目标设备实测，不得用于宣称最低设备支持；仍需现场测量。"
        if report.get("target_device_status") == "SIMULATED"
        else
        "本报告只测量临时 SQLite + FakeProvider 的确定性本地链路，不代表最低支持设备、Node 24、Electron 打包或真实讯飞性能。目标设备实测完成后，应将 `TARGET_DEVICE_STATUS` 改为 `PASS`，并附上设备、系统、构建和重复次数。"
    )
    return """# {title}

STATUS: {status}
TARGET_DEVICE_STATUS: {target}

{caveat}

```json
{payload}
```
""".format(
        title=title,
        status=report["status"],
        target=report.get("target_device_status", "OPEN"),
        caveat=caveat,
        payload=json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=10)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--simulate-target", action="store_true", help="run a documented target-device model simulation; never turns into hardware evidence")
    parser.add_argument("--cpu-slowdown-factor", type=float, default=2.0)
    parser.add_argument("--io-latency-ms", type=float, default=8.0)
    parser.add_argument("--memory-budget-mb", type=int, default=512)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "performance-baseline.md")
    args = parser.parse_args(argv)
    try:
        if args.simulate_target:
            report = run_simulated_target(
                items=args.items,
                runs=args.runs,
                cpu_slowdown_factor=args.cpu_slowdown_factor,
                io_latency_ms=args.io_latency_ms,
                memory_budget_mb=args.memory_budget_mb,
            )
        else:
            report = run_baseline(items=args.items, runs=args.runs)
    except Exception as exc:
        report = {
            "status": "FAIL",
            "classification": "DEVELOPMENT_HOST_BASELINE_ONLY",
            "target_device_status": "OPEN",
            "side_effect_policy": "FAKE_PROVIDER_ONLY",
            "reason": str(exc)[:2000],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    destination = args.output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_markdown(report), encoding="utf-8")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

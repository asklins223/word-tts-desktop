#!/usr/bin/env python3
"""Exercise a real child-process kill at the durable TTS submit boundary.

The child stops after the local ``IN_FLIGHT`` commit and before a Provider
response.  The parent kills only that child, starts a fresh database owner,
and verifies that recovery converts the local submission to a retryable
``REJECTED`` state without submitting anything or querying a Provider.  The
probe uses only the deterministic local repository and never enables a real
Provider.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _child(root: Path) -> int:
    from workflow.database import WorkflowDatabase
    from workflow.domain import content_hash
    from workflow.repositories import WorkflowRepository

    database = WorkflowDatabase(root / "workflow.db", profile="2a")
    database.initialize()
    repository = WorkflowRepository(database)
    workflow = repository.create_workflow("tts", {"generation_mode": "composite_cut"})
    item = repository.create_item(
        workflow.workflow_id,
        item_type="sentence",
        sequence=0,
        normalized_content="process recovery probe",
        item_identity_key="probe:0",
        role="default",
        voice_key="fake",
    )
    plan_item = {
        "ordinal": 0,
        "item_id": item,
        "identity_key": "probe:0",
        "content": "process recovery probe",
        "content_hash": content_hash("process recovery probe"),
        "role": "default",
        "voice_key": "fake",
    }
    _lease_id, fencing_token, _ = repository.acquire_lease(
        workflow.workflow_id,
        "provider",
        "fake:fake-account",
        "process-recovery-probe",
        ttl_seconds=300,
    )
    plan = repository.prepare_tts_plan(
        workflow.workflow_id,
        provider="fake",
        provider_account_scope="fake-account",
        unit_type="composite",
        tts_submission_key="process-recovery-probe",
        ordered_plan=[plan_item],
        input_hash=content_hash({"mode": "composite_cut", "items": [plan_item]}),
        submission_profile_hash=content_hash({"profile": "process-recovery-probe"}),
        capability_snapshot={"provider": "fake", "account_scope": "fake-account"},
        lease_fencing_token=fencing_token,
    )
    repository.begin_tts_submission(plan)
    print(json.dumps({"workflow_id": workflow.workflow_id, "submission_id": plan["submission_id"]}), flush=True)
    # Keep the process alive so the parent can kill the exact uncertain window.
    sys.stdin.buffer.read(1)
    return 0


def run_probe() -> dict[str, Any]:
    from workflow.database import WorkflowDatabase
    from workflow.recovery import RecoveryService

    with tempfile.TemporaryDirectory(prefix="wordtts-process-recovery-") as temporary:
        root = Path(temporary)
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--child", str(root)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ready: dict[str, Any] | None = None
        try:
            assert child.stdout is not None
            for _ in range(200):
                line = child.stdout.readline()
                if not line:
                    break
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and candidate.get("submission_id"):
                    ready = candidate
                    break
            if ready is None:
                stderr = child.stderr.read() if child.stderr is not None else ""
                raise RuntimeError(f"child did not reach IN_FLIGHT boundary: {stderr[-2000:]}")
            os.kill(child.pid, signal.SIGKILL)
            exit_code = child.wait(timeout=10)
            database = WorkflowDatabase(root / "workflow.db", profile="2a")
            database.initialize()
            try:
                recovery = RecoveryService(database)
                before = recovery.scan()
                applied = recovery.apply_safe_recovery()
                with database.read_transaction() as con:
                    submission_state = con.execute(
                        "SELECT side_effect_state FROM provider_submissions WHERE provider_submission_id=?",
                        (ready["submission_id"],),
                    ).fetchone()[0]
                    intent_state = con.execute(
                        "SELECT state FROM side_effect_intents WHERE workflow_id=? AND operation_namespace='tts'",
                        (ready["workflow_id"],),
                    ).fetchone()[0]
                if exit_code == 0:
                    raise RuntimeError("child was not killed at the recovery boundary")
                if not any(item.resource_id == ready["submission_id"] for item in before):
                    raise RuntimeError("recovery scanner did not observe the in-flight submission")
                if not any(item.resource_id == ready["submission_id"] for item in applied):
                    raise RuntimeError("recovery did not process the in-flight submission")
                if submission_state != "REJECTED" or intent_state != "ARCHIVED":
                    raise RuntimeError(
                        f"unexpected recovery state: submission={submission_state}, intent={intent_state}"
                    )
                return {
                    "status": "PASS",
                    "side_effect_policy": "FAKE_PROVIDER_ONLY",
                    "child_exit_code": exit_code,
                    "workflow_id": ready["workflow_id"],
                    "submission_id": ready["submission_id"],
                    "submission_state": submission_state,
                    "intent_state": intent_state,
                    "provider_submit_calls": 0,
                }
            finally:
                database.close()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child is not None:
        return _child(args.child.expanduser().resolve())
    try:
        report = run_probe()
    except Exception as exc:
        report = {"status": "FAIL", "side_effect_policy": "FAKE_PROVIDER_ONLY", "reason": str(exc)[:2000]}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

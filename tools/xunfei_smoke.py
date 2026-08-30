#!/usr/bin/env python3
"""Run logical or explicitly authorised real-Xunfei composite_cut smoke tests.

No credentials are read from this repository and no real call is made unless
both the environment feature flag and the command-line confirmation are set.
The default invocation is therefore a safe, machine-readable BLOCKED result;
``--logical-only`` exercises the complete workflow against an in-memory,
page-free backend with no network side effect.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _blocked(reason: str) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "side_effect_policy": "NO_REAL_CALL",
        "reason": reason,
        "required_for_execution": [
            "--confirm-real-side-effects",
            "WORDTTS_ENABLE_REAL_PROVIDER=1",
            "WORDTTS_XUNFEI_ACCOUNT_SCOPE",
            "--source pointing to one approved .docx or .xlsx file",
        ],
    }


class _LogicalXunfeiBackend:
    """In-memory Xunfei-shaped backend for page-free logic smoke tests."""

    # Keep the logical fixture recognizable as an MPEG-1 Layer III frame.
    # The workflow deliberately rejects arbitrary provider bytes before they
    # become a deliverable artifact, so a text-only placeholder is not a
    # valid success fixture.
    _MP3_FRAME_HEADER = b"\xff\xfb\x90\x64"

    def __init__(self, account_scope: str) -> None:
        self.account_scope = account_scope
        self.submit_calls = 0
        self.query_calls = 0
        self._receipts: dict[str, dict[str, Any]] = {}

    def submit(self, submission_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self._receipts.get(submission_key)
        if existing is not None:
            return dict(existing)
        self.submit_calls += 1
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        receipt = {
            "provider": "xunfei",
            "account_scope": self.account_scope,
            "provider_job_id": f"logical-job-{digest[:24]}",
            "canonical_key": f"logical-canonical-{digest[:24]}",
            "temporary_works_id": f"logical-temp-{digest[:16]}",
            "formal_works_id": f"logical-formal-{digest[:16]}",
            "output": self._MP3_FRAME_HEADER + ("logical-xunfei-mp3:" + digest).encode("ascii"),
            "summary": {"mode": "logical-only", "network": False, "page": False},
        }
        self._receipts[submission_key] = receipt
        return dict(receipt)

    def query(self, submission_key: str) -> dict[str, Any] | None:
        self.query_calls += 1
        receipt = self._receipts.get(submission_key)
        return dict(receipt) if receipt is not None else None


def _validate_source(source: Path, *, max_source_bytes: int) -> tuple[int, str | None]:
    if source.is_symlink() or not source.is_file():
        return 0, "source is not a regular file"
    if source.suffix.lower() not in {".docx", ".xlsx"}:
        return 0, "source must have a .docx or .xlsx suffix"
    size = source.stat().st_size
    if size > max_source_bytes:
        return size, f"source exceeds the {max_source_bytes} byte smoke budget"
    return size, None


def run_logical_smoke(
    source: Path,
    *,
    max_items: int = 20,
    max_source_bytes: int = 16 * 1024 * 1024,
) -> dict[str, Any]:
    """Run the full workflow with an in-memory Xunfei-shaped backend only."""

    size, source_error = _validate_source(source, max_source_bytes=max_source_bytes)
    if source_error:
        return {"status": "FAIL", "side_effect_policy": "LOGICAL_ONLY_NO_NETWORK", "reason": source_error}

    from application.workflow_service import WorkflowApplicationService
    from workflow.artifact_store import ArtifactStore
    from workflow.database import WorkflowDatabase
    from workflow.parser import LegacyWordParser
    from workflow.providers import ProviderRegistry, XunfeiTTSAdapter
    from workflow.repositories import WorkflowRepository
    from workflow.source_imports import SourceImportService

    account_scope = "logical-smoke"
    with tempfile.TemporaryDirectory(prefix="wordtts-logical-xunfei-smoke-") as temporary:
        root = Path(temporary)
        database = WorkflowDatabase(root / "workflow.db", profile="2a")
        database.initialize()
        backend = _LogicalXunfeiBackend(account_scope)
        provider = XunfeiTTSAdapter(account_scope=account_scope, backend=backend, allow_real=False)
        try:
            artifacts = ArtifactStore(root / "artifacts", max_bytes=max_source_bytes)
            repository = WorkflowRepository(database)
            imports = SourceImportService(database, artifacts)
            registry = ProviderRegistry()
            registry.register(provider)
            service = WorkflowApplicationService(
                repository,
                imports,
                artifacts,
                parser=LegacyWordParser(),
                providers=registry,
            )
            draft = service.create_draft(
                "tts",
                {
                    "generation_mode": "composite_cut",
                    "provider": "xunfei",
                    "account_scope": account_scope,
                    "source_filename": source.name,
                    "smoke_test": "logical-only",
                },
            )
            with source.open("rb") as source_handle:
                imported = service.import_source(
                    draft.workflow_id,
                    source_handle,
                    filename=source.name,
                    request_key=f"logical-smoke:{source.name}:{size}",
                    content_type="application/octet-stream",
                )
            # Completing the managed source binding advances the workflow
            # snapshot version.  Read the authoritative version before parse;
            # the original draft object is intentionally stale after import.
            current = service.repository.get_workflow(draft.workflow_id)
            parsed = service.parse(
                draft.workflow_id,
                expected_state_version=current.state_version,
                source_artifact_id=imported["source_artifact_id"],
            )
            item_count = int(parsed["workflow"].item_count)
            if item_count < 1 or item_count > max_items:
                return {
                    "status": "FAIL",
                    "side_effect_policy": "LOGICAL_ONLY_NO_NETWORK",
                    "reason": f"parsed item count {item_count} is outside the smoke budget 1..{max_items}",
                    "workflow_id": draft.workflow_id,
                    "item_count": item_count,
                }
            _accepted, result = service.start_generation(
                draft.workflow_id,
                expected_state_version=parsed["workflow"].state_version,
                generation_mode="composite_cut",
                provider="xunfei",
                account_scope=account_scope,
            )
            return {
                "status": "PASS" if result.status == "SUCCEEDED" else "FAIL",
                "side_effect_policy": "LOGICAL_ONLY_NO_NETWORK",
                "network_calls": 0,
                "page_calls": 0,
                "workflow_id": result.workflow_id,
                "item_count": item_count,
                "result": result.as_dict(),
                "provider": provider.capability_snapshot(),
                "backend_submit_calls": backend.submit_calls,
                "backend_query_calls": backend.query_calls,
                "source": {"filename": source.name, "size_bytes": size},
                "runtime": {"python": platform.python_version(), "platform": platform.platform()},
            }
        finally:
            provider.close()
            database.close()


def run_smoke(source: Path, *, max_items: int = 20, max_source_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    size, source_error = _validate_source(source, max_source_bytes=max_source_bytes)
    if source_error:
        return {"status": "FAIL", "reason": source_error}
    account_scope = os.environ.get("WORDTTS_XUNFEI_ACCOUNT_SCOPE", "").strip()
    if not account_scope:
        return {"status": "BLOCKED", "reason": "WORDTTS_XUNFEI_ACCOUNT_SCOPE is not set"}

    from application.workflow_service import WorkflowApplicationService
    from workflow.artifact_store import ArtifactStore
    from workflow.database import WorkflowDatabase
    from workflow.parser import LegacyWordParser
    from workflow.providers import ProviderRegistry, XunfeiTTSAdapter
    from workflow.repositories import WorkflowRepository
    from workflow.source_imports import SourceImportService

    with tempfile.TemporaryDirectory(prefix="wordtts-xunfei-smoke-") as temporary:
        root = Path(temporary)
        database = WorkflowDatabase(root / "workflow.db", profile="2a")
        database.initialize()
        provider = None
        try:
            artifacts = ArtifactStore(root / "artifacts", max_bytes=max_source_bytes)
            repository = WorkflowRepository(database)
            imports = SourceImportService(database, artifacts)
            registry = ProviderRegistry()
            provider = XunfeiTTSAdapter(account_scope=account_scope, allow_real=True)
            registry.register(provider)
            service = WorkflowApplicationService(
                repository,
                imports,
                artifacts,
                parser=LegacyWordParser(),
                providers=registry,
            )
            draft = service.create_draft(
                "tts",
                {
                    "generation_mode": "composite_cut",
                    "provider": "xunfei",
                    "account_scope": account_scope,
                    "source_filename": source.name,
                    "smoke_test": True,
                },
            )
            with source.open("rb") as source_handle:
                imported = service.import_source(
                    draft.workflow_id,
                    source_handle,
                    filename=source.name,
                    request_key=f"real-smoke:{source.name}:{size}",
                    content_type="application/octet-stream",
                )
            current = service.repository.get_workflow(draft.workflow_id)
            parsed = service.parse(
                draft.workflow_id,
                expected_state_version=current.state_version,
                source_artifact_id=imported["source_artifact_id"],
            )
            item_count = int(parsed["workflow"].item_count)
            if item_count < 1 or item_count > max_items:
                return {
                    "status": "BLOCKED",
                    "reason": f"parsed item count {item_count} is outside the smoke budget 1..{max_items}",
                    "workflow_id": draft.workflow_id,
                    "item_count": item_count,
                }
            _accepted, result = service.start_generation(
                draft.workflow_id,
                expected_state_version=parsed["workflow"].state_version,
                generation_mode="composite_cut",
                provider="xunfei",
                account_scope=account_scope,
            )
            return {
                "status": "PASS" if result.status == "SUCCEEDED" else "FAIL",
                "side_effect_policy": "REAL_XUNFEI_COMPOSITE_CUT",
                "workflow_id": result.workflow_id,
                "item_count": item_count,
                "result": result.as_dict(),
                "provider": provider.capability_snapshot(),
                "source": {"filename": source.name, "size_bytes": size},
                "runtime": {"python": platform.python_version(), "platform": platform.platform()},
            }
        finally:
            if provider is not None:
                provider.close()
            database.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="one approved .docx/.xlsx smoke input")
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument("--max-source-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--logical-only", action="store_true", help="run the complete workflow with an in-memory, page-free Xunfei-shaped backend")
    parser.add_argument("--confirm-real-side-effects", action="store_true")
    parser.add_argument("--report", type=Path, help="optional JSON report path")
    args = parser.parse_args(argv)

    if args.logical_only and args.source is None:
        report = {"status": "FAIL", "side_effect_policy": "LOGICAL_ONLY_NO_NETWORK", "reason": "--source is required for a logical smoke"}
    elif args.logical_only and (args.max_items < 1 or args.max_source_bytes < 1):
        report = {"status": "FAIL", "side_effect_policy": "LOGICAL_ONLY_NO_NETWORK", "reason": "smoke budgets must be positive"}
    elif args.logical_only:
        try:
            report = run_logical_smoke(
                args.source.expanduser().resolve(),
                max_items=args.max_items,
                max_source_bytes=args.max_source_bytes,
            )
        except Exception as exc:
            report = {"status": "FAIL", "side_effect_policy": "LOGICAL_ONLY_NO_NETWORK", "reason": str(exc)[:2000]}
    elif not args.confirm_real_side_effects:
        report = _blocked("explicit confirmation flag is missing")
    elif os.environ.get("WORDTTS_ENABLE_REAL_PROVIDER") != "1":
        report = _blocked("WORDTTS_ENABLE_REAL_PROVIDER is not exactly 1")
    elif args.source is None:
        report = _blocked("--source is required for a real smoke")
    elif args.max_items < 1 or args.max_source_bytes < 1:
        report = {"status": "FAIL", "reason": "smoke budgets must be positive"}
    else:
        try:
            report = run_smoke(
                args.source.expanduser().resolve(),
                max_items=args.max_items,
                max_source_bytes=args.max_source_bytes,
            )
        except Exception as exc:  # preserve the external failure as evidence; never retry here.
            report = {"status": "FAIL", "reason": str(exc)[:2000], "retry_policy": "manual_reconcile_only"}

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.report:
        destination = args.report.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    return 0 if report["status"] == "PASS" else 2 if report["status"] == "BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())

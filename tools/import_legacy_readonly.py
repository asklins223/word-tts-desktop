#!/usr/bin/env python3
"""Dry-run/apply one-time legacy JSON session import.

The command is deliberately dry-run by default.  ``--apply`` is the only
switch that opens the target database and writes managed Blobs/rows; source
JSON, source documents, and legacy audio are never modified.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from workflow.artifact_store import ArtifactStore
from workflow.database import WorkflowDatabase
from workflow.legacy_import import LegacyImporter
from workflow.repositories import WorkflowRepository
from workflow.security import OneTimeTicketManager
from workflow.source_imports import SourceImportService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", required=True, help="directory containing legacy session directories")
    parser.add_argument("--source-root", help="optional allow-listed root for original source documents")
    parser.add_argument("--database", required=True, help="target workflow SQLite database")
    parser.add_argument("--artifact-root", required=True, help="target managed Artifact directory")
    parser.add_argument("--profile", choices=("2a", "full"), default="full")
    parser.add_argument("--apply", action="store_true", help="write the target database and managed Artifacts")
    parser.add_argument("--report", help="optional JSON report path")
    args = parser.parse_args(argv)

    database = None
    try:
        repository = None
        artifacts = None
        imports = None
        if args.apply:
            database = WorkflowDatabase(args.database, profile=args.profile)
            database.initialize()
            repository = WorkflowRepository(database)
            artifacts = ArtifactStore(args.artifact_root)
            imports = SourceImportService(database, artifacts, ticket_manager=OneTimeTicketManager(max_ttl_seconds=3600))
        importer = LegacyImporter(
            args.legacy_root,
            source_root=args.source_root,
            repository=repository,
            artifact_store=artifacts,
            source_imports=imports,
        )
        reports = [report.as_dict() for report in importer.run(apply=args.apply)]
        output = {"ok": all(not report.get("error") for report in reports), "apply": args.apply, "reports": reports}
        rendered = json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2)
        print(rendered)
        if args.report:
            report_path = Path(args.report).expanduser().resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(rendered + "\n", encoding="utf-8")
        return 0 if output["ok"] else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "apply": args.apply, "errors": [str(exc)[:2000]]}, ensure_ascii=False, indent=2))
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())

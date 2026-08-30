"""Bounded, diagnostic-first cleanup for staging and unreferenced Blobs."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .artifact_store import ArtifactStore


@dataclass(frozen=True)
class GarbageFinding:
    kind: str
    key: str
    action: str


class ArtifactGarbageCollector:
    def __init__(self, database, artifact_store: ArtifactStore, *, staging_ttl_seconds: int = 3600) -> None:
        self.database = database
        self.artifact_store = artifact_store
        self.staging_ttl_seconds = max(60, int(staging_ttl_seconds))

    def scan(self) -> list[GarbageFinding]:
        referenced: set[str] = set()
        with self.database.read_transaction() as con:
            referenced.update(str(row["storage_key"]) for row in con.execute(
                "SELECT storage_key FROM artifact_blobs WHERE lifecycle_state='READY'"
            ).fetchall())
        findings = [GarbageFinding("blob", key, "ORPHAN") for key in self.artifact_store.scan_orphans(referenced)]
        for key in sorted(referenced):
            exists = self.artifact_store.has_regular_file(key)
            if not exists:
                # A DB READY row whose Blob is missing is a consistency fault,
                # never a deletion candidate.  Keep it visible to startup
                # diagnostics instead of silently treating it as cleaned up.
                findings.append(GarbageFinding("blob-missing", key, "MISSING"))
        cutoff = time.time() - self.staging_ttl_seconds
        if self.artifact_store.staging_root.exists():
            for path in self.artifact_store.staging_root.rglob("*"):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    findings.append(GarbageFinding("staging", path.relative_to(self.artifact_store.root).as_posix(), "EXPIRE"))
        return sorted(findings, key=lambda item: (item.kind, item.key))

    def collect(self, *, limit: int = 32) -> list[GarbageFinding]:
        """Delete only paths identified by a fresh scan; DB-referenced blobs stay."""

        limit = max(1, min(int(limit), 256))
        findings = self.scan()[:limit]
        # The scan and deletion are separate operations. Re-check the DB so a
        # Blob referenced after the scan cannot be removed by a stale finding.
        with self.database.read_transaction() as con:
            referenced_now = {
                str(row["storage_key"]) for row in con.execute(
                    "SELECT storage_key FROM artifact_blobs WHERE lifecycle_state='READY'"
                ).fetchall()
            }
        removed: list[GarbageFinding] = []
        for finding in findings:
            if finding.action not in {"ORPHAN", "EXPIRE"}:
                continue
            if finding.kind == "blob":
                if finding.key in referenced_now or not self.artifact_store.delete_blob(finding.key):
                    continue
            elif finding.kind == "staging":
                if not self.artifact_store.delete_staging(finding.key):
                    continue
            else:
                continue
            removed.append(GarbageFinding(finding.kind, finding.key, "REMOVED"))
        return removed

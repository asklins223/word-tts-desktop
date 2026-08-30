"""Deterministic external-system double for reconciliation tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .domain import content_hash
from .external import ExternalLookup, ExternalSubmission, ExternalVerification


@dataclass(frozen=True)
class FakeExternalRecord:
    business_record_key: str
    external_record_id: str
    operation_key: str
    payload_hash: str
    summary: Mapping[str, Any]


class FakeExternalAdapter:
    system = "fake-external"

    def __init__(self, *, account_scope: str = "fake-scope") -> None:
        self.account_scope = account_scope
        self.fail_mode: str | None = None
        self.submit_calls = 0
        self.query_calls = 0
        self._records_by_operation: dict[str, FakeExternalRecord] = {}
        self._records_by_business_key: dict[str, FakeExternalRecord] = {}

    def seed(self, business_record_key: str, payload: Mapping[str, Any], *, external_record_id: str = "seeded-1") -> None:
        record = FakeExternalRecord(
            business_record_key=business_record_key,
            external_record_id=external_record_id,
            operation_key="seeded",
            payload_hash=content_hash(payload),
            summary={"seeded": True},
        )
        self._records_by_business_key[business_record_key] = record

    def bind_business_key(self, operation_key: str, business_record_key: str) -> None:
        record = self._records_by_operation.get(operation_key)
        if record is None:
            return
        self._records_by_business_key[business_record_key] = FakeExternalRecord(
            business_record_key, record.external_record_id, record.operation_key, record.payload_hash, record.summary
        )

    def lookup(self, business_record_key: str) -> ExternalLookup:
        record = self._records_by_business_key.get(business_record_key)
        if record is None:
            return ExternalLookup(found=False, business_record_key=business_record_key)
        return self._lookup(record)

    def submit(
        self,
        operation_key: str,
        payload: Mapping[str, Any],
        existing: ExternalLookup | None = None,
    ) -> ExternalSubmission:
        record = self._records_by_operation.get(operation_key)
        if record is not None:
            return ExternalSubmission(record.external_record_id, operation_key, record.summary)
        self.submit_calls += 1
        if self.fail_mode == "before":
            raise RuntimeError("fake external failure before submission")
        payload_hash = content_hash(payload)
        record = FakeExternalRecord(
            business_record_key=str((payload.get("business_record_key") if isinstance(payload, Mapping) else "") or ""),
            external_record_id=f"fake-external-{self.submit_calls}",
            operation_key=operation_key,
            payload_hash=payload_hash,
            summary={"operation_key": operation_key},
        )
        self._records_by_operation[operation_key] = record
        if record.business_record_key:
            self._records_by_business_key[record.business_record_key] = record
        if self.fail_mode == "after":
            self.fail_mode = None
            raise RuntimeError("fake external response lost after submission")
        return ExternalSubmission(record.external_record_id, operation_key, record.summary)

    def query(self, operation_key: str, external_record_id: str | None = None) -> ExternalLookup:
        self.query_calls += 1
        record = self._records_by_operation.get(operation_key)
        if record is None and external_record_id:
            record = next((candidate for candidate in self._records_by_operation.values() if candidate.external_record_id == external_record_id), None)
        if record is None:
            return ExternalLookup(found=False)
        return self._lookup(record)

    def verify(self, operation_key: str, payload: Mapping[str, Any], external_record_id: str | None = None) -> ExternalVerification:
        record = self._records_by_operation.get(operation_key)
        if record is None and external_record_id:
            record = next((candidate for candidate in self._records_by_operation.values() if candidate.external_record_id == external_record_id), None)
        if record is None:
            return ExternalVerification(verified=False)
        return ExternalVerification(
            verified=record.payload_hash == content_hash(payload),
            external_record_id=record.external_record_id,
            payload_hash=record.payload_hash,
            summary=record.summary,
        )

    @staticmethod
    def _lookup(record: FakeExternalRecord) -> ExternalLookup:
        return ExternalLookup(
            found=True,
            external_record_id=record.external_record_id,
            business_record_key=record.business_record_key,
            payload_hash=record.payload_hash,
            status="EXISTS",
            summary=record.summary,
        )

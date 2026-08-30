"""Deterministic MP3 Provider port used before a real external account is enabled."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping


class FakeProviderError(RuntimeError):
    code = "TRANSIENT_PROVIDER_ERROR"


class AmbiguousProviderError(FakeProviderError):
    code = "SUBMISSION_AMBIGUOUS"


@dataclass(frozen=True)
class FakeReceipt:
    provider: str
    account_scope: str
    submission_key: str
    provider_job_id: str
    payload_hash: str
    output: bytes
    segments: Mapping[str, bytes] | None = None
    output_format: str = "mp3"


class FakeProvider:
    def __init__(self, *, account_scope: str = "fake-account", output_format: str = "mp3") -> None:
        self.provider = "fake"
        self.account_scope = account_scope
        self.output_format = str(output_format or "mp3").lower().lstrip(".")
        self.fail_mode: str | None = None
        self.submit_calls = 0
        self._receipts: dict[str, FakeReceipt] = {}

    def capability_snapshot(self) -> dict[str, Any]:
        """Expose the same output-format fact that a real Provider exposes."""

        return {
            "provider": self.provider,
            "account_scope": self.account_scope,
            "capability_version": "fake-provider-1",
            "generation_modes": ["composite_cut", "single_segment"],
            "formats": [self.output_format],
            "supports_query": True,
            "supports_resume": True,
            "status": "READY",
            "ready": True,
            "can_generate": True,
            "can_start_generation": True,
            "reason": "测试 Provider 已就绪",
        }

    def submit(self, submission_key: str, payload: Mapping[str, Any]) -> FakeReceipt:
        existing = self._receipts.get(submission_key)
        if existing is not None:
            return existing
        self.submit_calls += 1
        payload_hash = hashlib.sha256(str(sorted(payload.items())).encode("utf-8")).hexdigest()
        if self.fail_mode == "before":
            raise FakeProviderError("simulated failure before submission")
        plan = payload.get("plan") if isinstance(payload.get("plan"), list) else []
        # Use a recognizable MPEG Layer III frame header rather than an
        # arbitrary ``ID3`` prefix.  The latter is only a metadata marker and
        # made fake payloads bypass the engine's MP3 publication gate.
        prefix = b"\xff\xfb\x90\x64" if self.output_format == "mp3" else b""
        segments = {
            str(item.get("item_id")): prefix + (
                "fake-audio:" + payload_hash + ":item:" + str(item.get("item_id"))
            ).encode("utf-8")
            for item in plan
            if isinstance(item, Mapping) and item.get("item_id")
        }
        receipt = FakeReceipt(
            self.provider,
            self.account_scope,
            submission_key,
            # Provider job identifiers are account-scoped, not adapter-instance
            # scoped.  A new adapter object must not recycle fake-job-1 while
            # the same fake account still has durable receipts in the workflow
            # database.
            f"fake-job-{hashlib.sha256(submission_key.encode('utf-8')).hexdigest()[:24]}",
            payload_hash,
            prefix + ("fake-audio:" + payload_hash).encode("ascii"),
            segments,
            self.output_format,
        )
        self._receipts[submission_key] = receipt
        if self.fail_mode == "after":
            self.fail_mode = None
            raise AmbiguousProviderError("simulated response loss after submission")
        return receipt

    def query(self, submission_key: str) -> FakeReceipt | None:
        return self._receipts.get(submission_key)

    def download(self, receipt: FakeReceipt) -> bytes:
        if not receipt or receipt.provider != self.provider or receipt.account_scope != self.account_scope:
            raise FakeProviderError("receipt belongs to another fake provider scope")
        return receipt.output

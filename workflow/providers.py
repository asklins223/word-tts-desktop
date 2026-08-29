"""Provider ports and the Xunfei adapter boundary.

The workflow engine depends on the small ``TTSProviderPort`` only.  Browser
automation, temporary/formal works identifiers, and byte download details are
kept behind this module so a second provider does not require engine changes.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .domain import DomainError, content_hash


class ProviderError(RuntimeError):
    code = "TRANSIENT_PROVIDER_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
        ambiguous: bool | None = None,
    ) -> None:
        super().__init__(_safe_provider_message(message, "provider call failed"))
        if code:
            self.code = code
        self.details = _safe_provider_details(details)
        self.ambiguous = ambiguous


class ProviderCapabilityError(ProviderError):
    code = "EXTERNAL_CAPABILITY_REQUIRED"


_SENSITIVE_DETAIL_KEYS = {
    "authorization", "cookie", "password", "secret", "token", "api_key", "apikey",
}


def _safe_provider_message(message: Any, fallback: str = "provider call failed") -> str:
    """Keep provider diagnostics useful without leaking browser credentials."""

    text = " ".join(str(message or "").split())
    for label in _SENSITIVE_DETAIL_KEYS:
        text = re.sub(
            rf"(?i)({re.escape(label)}\s*[:=]\s*)\S+",
            r"\1[REDACTED]",
            text,
        )
    return (text or fallback)[:2000]


def _safe_provider_details(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}

    def clean(item: Any, *, key: str = "") -> Any:
        if key.lower() in _SENSITIVE_DETAIL_KEYS:
            return "[REDACTED]"
        if isinstance(item, Mapping):
            return {str(k)[:128]: clean(v, key=str(k)) for k, v in list(item.items())[:32]}
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in list(item)[:32]]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return _safe_provider_message(item)

    return clean(value)


def _normalize_legacy_error(error: Exception, *, works_name: str | None = None) -> ProviderError:
    """Map the legacy module's exception classes into stable workflow errors."""

    if isinstance(error, ProviderError):
        if works_name and "works_name" not in error.details:
            error.details["works_name"] = _safe_provider_message(works_name)
        # A legacy result may report a quota/login error as a plain mapping
        # instead of raising its typed exception.  Let those default-coded
        # messages go through the same normalization below; explicit codes
        # must remain authoritative.
        if error.code != ProviderError.code or error.ambiguous is not None:
            return error

    class_name = type(error).__name__
    raw_message = _safe_provider_message(error)
    lowered = raw_message.lower()
    details: dict[str, Any] = {}
    error_works_name = getattr(error, "works_name", None) or works_name
    if error_works_name:
        details["works_name"] = _safe_provider_message(error_works_name)

    if class_name == "XunfeiQuotaExceeded" or any(token in lowered for token in ("额度不足", "quota exceeded", "quota")):
        return ProviderError(
            "讯飞配音额度不足，请检查账号额度后重试",
            code="PROVIDER_QUOTA_EXCEEDED",
            details=details,
            ambiguous=False,
        )
    if class_name == "XunfeiLoginRequired" or any(token in lowered for token in ("尚未登录", "请先登录", "login required", "登录失效")):
        return ProviderError(
            "讯飞配音登录已失效，请在浏览器中重新登录后重试",
            code="PROVIDER_LOGIN_REQUIRED",
            details=details,
            ambiguous=False,
        )
    if class_name == "XunfeiRateLimited" or any(token in lowered for token in ("rate limit", "频控", "请求过于频繁")):
        return ProviderError(
            "讯飞配音请求触发频控，请稍后重试",
            code="PROVIDER_RATE_LIMITED",
            details=details,
            ambiguous=False,
        )
    if class_name == "XunfeiSubmissionAmbiguous":
        return ProviderError(
            raw_message,
            code="SUBMISSION_AMBIGUOUS",
            details=details,
            ambiguous=True,
        )
    if class_name == "XunfeiCancelled":
        return ProviderError(
            "讯飞浏览器任务已取消；为避免重复扣费，提交结果需要核验",
            code="SUBMISSION_AMBIGUOUS",
            details=details,
            ambiguous=True,
        )
    # The legacy flow raises its plain XunfeiError while preparing the page or
    # editor, before the visible "确认合成" action.  A completed/uncertain
    # click is promoted to XunfeiSubmissionAmbiguous by the legacy flow above;
    # keeping this pre-boundary error non-ambiguous lets the durable workflow
    # release the failed attempt and safely retry with a changed voice/config.
    # Unknown legacy/provider failures are also non-ambiguous by default: the
    # engine only needs the conservative ambiguity path after an explicit
    # submission-boundary exception above.
    return ProviderError(raw_message, details=details, ambiguous=False)


class TTSProviderPort(Protocol):
    provider: str
    account_scope: str

    def submit(self, submission_key: str, payload: Mapping[str, Any]) -> Any:
        ...

    def query(self, submission_key: str) -> Any | None:
        ...

    def query_with_context(
        self,
        submission_key: str,
        context: Mapping[str, Any] | None = None,
        *,
        cancel_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> Any | None:
        ...

    def download(self, receipt: Any) -> bytes:
        ...


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    account_scope: str
    capability_version: str
    generation_modes: tuple[str, ...]
    formats: tuple[str, ...]
    supports_query: bool
    supports_resume: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account_scope": self.account_scope,
            "capability_version": self.capability_version,
            "generation_modes": list(self.generation_modes),
            "formats": list(self.formats),
            "supports_query": self.supports_query,
            "supports_resume": self.supports_resume,
        }


@dataclass(frozen=True)
class ProviderReceipt:
    provider: str
    account_scope: str
    submission_key: str
    provider_job_id: str
    canonical_key: str
    output: bytes
    temporary_works_id: str | None = None
    formal_works_id: str | None = None
    summary: Mapping[str, Any] | None = None
    # A composite provider must return independently verifiable item bytes;
    # the engine must never manufacture child artifacts by copying the parent.
    segments: Mapping[str, bytes] | None = None
    output_format: str = "mp3"


class BrowserRuntimePort(Protocol):
    def ensure_session(self, voice_key: str) -> Any:
        ...

    def close(self) -> None:
        ...


class BrowserRuntime:
    """Small injectable seam for Playwright/session lifecycle tests."""

    def __init__(self, session_factory: Callable[[str], Any] | None = None, close_callback: Callable[[], None] | None = None) -> None:
        self._session_factory = session_factory
        self._close_callback = close_callback
        self._session: Any | None = None

    def ensure_session(self, voice_key: str) -> Any:
        if self._session is None:
            if self._session_factory is None:
                raise ProviderCapabilityError("Xunfei BrowserRuntime is not configured for this process")
            self._session = self._session_factory(voice_key)
        return self._session

    def close(self) -> None:
        if self._close_callback is not None:
            self._close_callback()
        elif self._session is not None and hasattr(self._session, "close"):
            self._session.close()
        self._session = None


class SubmissionTracker:
    """Map temporary/formal provider identifiers to one canonical receipt."""

    def __init__(self) -> None:
        self._by_submission: dict[str, dict[str, str | None]] = {}

    def observe(
        self,
        submission_key: str,
        *,
        temporary_works_id: str | None = None,
        formal_works_id: str | None = None,
        canonical_key: str | None = None,
    ) -> str:
        if not submission_key:
            raise DomainError("VALIDATION_ERROR", "submission_key is required")
        existing = self._by_submission.get(submission_key)
        if existing is None:
            canonical = canonical_key or formal_works_id or temporary_works_id or content_hash(submission_key)[:24]
            existing = {"canonical_key": canonical, "temporary_works_id": temporary_works_id, "formal_works_id": formal_works_id}
            self._by_submission[submission_key] = existing
        else:
            if canonical_key and existing["canonical_key"] not in (None, canonical_key):
                raise ProviderError("provider identifiers resolve to different canonical receipts")
            if temporary_works_id and existing["temporary_works_id"] not in (None, temporary_works_id):
                raise ProviderError("temporary works identifier changed for a submission")
            if formal_works_id and existing["formal_works_id"] not in (None, formal_works_id):
                raise ProviderError("formal works identifier changed for a submission")
        existing["temporary_works_id"] = existing["temporary_works_id"] or temporary_works_id
        existing["formal_works_id"] = existing["formal_works_id"] or formal_works_id
        return str(existing["canonical_key"])

    def get(self, submission_key: str) -> dict[str, str | None] | None:
        value = self._by_submission.get(submission_key)
        return dict(value) if value else None


class ArtifactDownloader:
    """Verify provider output before handing bytes to ArtifactStore."""

    def __init__(self, download_callback: Callable[[Any], bytes] | None = None) -> None:
        self._download_callback = download_callback

    def download(self, receipt: Any) -> bytes:
        if self._download_callback is not None:
            value = self._download_callback(receipt)
        elif isinstance(receipt, ProviderReceipt):
            value = receipt.output
        else:
            value = getattr(receipt, "output", None)
        if not isinstance(value, (bytes, bytearray)) or not value:
            raise ProviderError("provider returned an empty or non-byte artifact", code="ARTIFACT_INVALID")
        return bytes(value)

    @staticmethod
    def verify(data: bytes, *, expected_sha256: str | None = None, min_bytes: int = 1) -> str:
        if not isinstance(data, (bytes, bytearray)) or len(data) < max(1, min_bytes):
            raise ProviderError("provider artifact is empty or below the minimum size", code="ARTIFACT_INVALID")
        digest = content_hash(bytes(data))
        if expected_sha256 and digest != expected_sha256:
            raise ProviderError("provider artifact hash does not match its receipt", code="ARTIFACT_INVALID")
        return digest


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[tuple[str, str], TTSProviderPort] = {}

    def register(self, provider: TTSProviderPort) -> None:
        key = (str(provider.provider), str(provider.account_scope))
        if not all(key):
            raise DomainError("VALIDATION_ERROR", "provider and account_scope are required")
        if key in self._providers:
            raise DomainError("CONFLICT", f"provider is already registered: {key[0]}:{key[1]}")
        self._providers[key] = provider

    def get(self, provider: str, account_scope: str) -> TTSProviderPort:
        try:
            return self._providers[(provider, account_scope)]
        except KeyError as exc:
            raise DomainError("NOT_FOUND", f"provider is not registered: {provider}:{account_scope}") from exc


class XunfeiTTSAdapter:
    """Controlled adapter over the existing Xunfei implementation.

    ``backend`` is the preferred production/test seam.  When omitted, real
    Playwright calls are enabled for the formal runtime by default.  Logical
    smoke/tests pass ``allow_real=False`` explicitly to stay offline.
    """

    provider = "xunfei"

    def __init__(
        self,
        *,
        account_scope: str = "xunfei-default",
        backend: Any | None = None,
        browser_runtime: BrowserRuntime | None = None,
        tracker: SubmissionTracker | None = None,
        downloader: ArtifactDownloader | None = None,
        allow_real: bool = True,
    ) -> None:
        self.account_scope = account_scope
        self.backend = backend
        self.browser_runtime = browser_runtime or BrowserRuntime()
        self.tracker = tracker or SubmissionTracker()
        self.downloader = downloader or ArtifactDownloader()
        self.allow_real = bool(allow_real)
        self._receipts: dict[str, ProviderReceipt] = {}
        self._submission_payloads: dict[str, dict[str, Any]] = {}
        self.capabilities = ProviderCapabilities(
            self.provider,
            account_scope,
            "xunfei-adapter-1",
            ("composite_cut", "single_segment"),
            ("mp3",),
            True,
            True,
        )

    def capability_snapshot(self) -> dict[str, Any]:
        snapshot = self.capabilities.as_dict()
        snapshot["real_calls_enabled"] = self.allow_real
        snapshot["backend"] = type(self.backend).__name__ if self.backend is not None else "legacy-xunfei"
        return snapshot

    def submit(self, submission_key: str, payload: Mapping[str, Any]) -> ProviderReceipt:
        existing = self._receipts.get(submission_key)
        if existing is not None:
            return existing
        cancel_check = payload.get("_cancel_check") if isinstance(payload, Mapping) else None
        progress_callback = payload.get("_progress_callback") if isinstance(payload, Mapping) else None
        public_payload = {
            key: value for key, value in dict(payload).items()
            if key not in {"_cancel_check", "_progress_callback"}
        }
        self._submission_payloads[submission_key] = public_payload
        if self.backend is not None:
            raw = self.backend.submit(submission_key, public_payload)
            receipt = self._normalize_backend_receipt(submission_key, raw)
        else:
            if not self.allow_real:
                raise ProviderCapabilityError("real Xunfei calls are disabled; pass an explicit smoke-test capability")
            receipt = self._submit_legacy_xunfei(
                submission_key,
                public_payload,
                cancel_check=cancel_check if callable(cancel_check) else None,
                progress_callback=progress_callback if callable(progress_callback) else None,
            )
        self._receipts[submission_key] = receipt
        self.tracker.observe(
            submission_key,
            temporary_works_id=receipt.temporary_works_id,
            formal_works_id=receipt.formal_works_id,
            canonical_key=receipt.canonical_key,
        )
        return receipt

    def query(self, submission_key: str) -> ProviderReceipt | None:
        return self.query_with_context(submission_key, self._submission_payloads.get(submission_key))

    def query_with_context(
        self,
        submission_key: str,
        context: Mapping[str, Any] | None = None,
        *,
        cancel_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> ProviderReceipt | None:
        existing = self._receipts.get(submission_key)
        if existing is not None:
            return existing
        if self.backend is not None and hasattr(self.backend, "query"):
            raw = self.backend.query(submission_key)
            if raw is None:
                return None
            receipt = self._normalize_backend_receipt(submission_key, raw)
            self._receipts[submission_key] = receipt
            return receipt
        if not self.allow_real:
            return None
        payload = dict(self._submission_payloads.get(submission_key) or {})
        if isinstance(context, Mapping):
            payload.update(context)
            self._submission_payloads[submission_key] = payload
        return self._query_legacy_xunfei(
            submission_key,
            payload,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )

    def download(self, receipt: ProviderReceipt) -> bytes:
        return self.downloader.download(receipt)

    def close(self) -> None:
        self.browser_runtime.close()

    def _normalize_backend_receipt(self, submission_key: str, raw: Any) -> ProviderReceipt:
        if isinstance(raw, ProviderReceipt):
            return raw
        output = getattr(raw, "output", None)
        if output is None and isinstance(raw, Mapping):
            output = raw.get("output")
        if isinstance(output, str):
            output = output.encode("utf-8")
        if not isinstance(output, (bytes, bytearray)):
            raise ProviderError("Xunfei backend did not return bytes", code="ARTIFACT_INVALID")

        raw_segments = raw.get("segments") if isinstance(raw, Mapping) else getattr(raw, "segments", None)
        segments = None
        if raw_segments is not None:
            if not isinstance(raw_segments, Mapping):
                raise ProviderError("Xunfei backend returned malformed item segments", code="ARTIFACT_INVALID", ambiguous=False)
            segments = {}
            for item_id, value in raw_segments.items():
                if not isinstance(value, (bytes, bytearray, memoryview)) or not value:
                    raise ProviderError("Xunfei backend returned an empty item segment", code="ARTIFACT_INVALID", ambiguous=False)
                segments[str(item_id)] = bytes(value)

        def value(name: str, default: str = "") -> str:
            if isinstance(raw, Mapping):
                return str(raw.get(name) or default)
            return str(getattr(raw, name, default) or default)
        output_format = value("output_format", value("format", "mp3")).lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9][a-z0-9+_-]{0,15}", output_format):
            raise ProviderError("Xunfei backend returned an invalid output format", code="ARTIFACT_INVALID", ambiguous=False)
        summary = raw.get("summary") if isinstance(raw, Mapping) else getattr(raw, "summary", {})
        if not isinstance(summary, Mapping):
            summary = {}
        return ProviderReceipt(
            # A backend may return only provider-native identifiers.  The
            # registered adapter is the authoritative scope in that case;
            # defaulting to xunfei-default would make a non-default account
            # fail receipt binding after a successful provider call.
            provider=value("provider", self.provider),
            account_scope=value("account_scope", self.account_scope),
            submission_key=submission_key,
            provider_job_id=value("provider_job_id", submission_key),
            canonical_key=value("canonical_key", value("provider_job_id", submission_key)),
            output=bytes(output),
            temporary_works_id=value("temporary_works_id") or None,
            formal_works_id=value("formal_works_id") or value("works_id") or None,
            summary=summary,
            segments=segments,
            output_format=output_format,
        )

    @staticmethod
    def _legacy_works_name(submission_key: str) -> str:
        return f"wordtts_{content_hash(submission_key)[:24]}"

    def _legacy_works(self, submission_key: str, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        item_specs = self._legacy_item_specs(payload)
        if not item_specs:
            raise DomainError("VALIDATION_ERROR", "Xunfei submission plan is empty")
        try:
            from word_tts_app import build_composite_work_plan

            works = build_composite_work_plan(
                item_specs,
                # One durable provider submission represents one durable work
                # unit.  Splitting here would make receipt/artifact ownership
                # ambiguous, so refuse an oversized plan instead.
                max_items=max(1, len(item_specs)),
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"无法构造讯飞多人配音计划：{exc}",
                code="VALIDATION_ERROR",
                ambiguous=False,
            ) from exc
        if len(works) != 1:
            raise ProviderCapabilityError(
                "讯飞多人配音计划超过单个可对账作品的安全上限；请拆分文档后重试"
            )
        work = dict(works[0])
        work.update({
            "work_id": submission_key,
            "job_id": submission_key,
            "work_index": 1,
            "works_name": self._legacy_works_name(submission_key),
        })
        return [work]

    @staticmethod
    def _legacy_item_specs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        plan = list(payload.get("plan") or [])
        profile = payload.get("profile") if isinstance(payload.get("profile"), Mapping) else {}
        role_voices = profile.get("role_voices") if isinstance(profile.get("role_voices"), Mapping) else None
        role_configs = profile.get("role_configs") if isinstance(profile.get("role_configs"), Mapping) else None
        result: list[dict[str, Any]] = []
        for index, item in enumerate(plan):
            if not isinstance(item, Mapping):
                raise ProviderError("Xunfei submission plan contains an invalid item", code="VALIDATION_ERROR", ambiguous=False)
            item_id = str(item.get("item_id") or f"item-{index}").strip()
            text = str(item.get("content") or "")
            if not item_id or not text.strip():
                raise ProviderError("Xunfei submission plan contains an empty item", code="VALIDATION_ERROR", ambiguous=False)
            result.append({
                "item_id": item_id,
                "text": text,
                # The durable engine plan calls this field ``speed`` while
                # the legacy Xunfei UI builder calls it ``rate``.  Preserve
                # the materialized value here; otherwise a recovered run
                # silently falls back to the default rate of 50.
                "rate": (
                    item.get("speed")
                    if item.get("speed") is not None
                    else item.get("rate", profile.get("rate", 50))
                ),
                "volume": item.get("volume", profile.get("volume", 50)),
                "pitch": item.get("pitch", profile.get("pitch", 50)),
                "default_voice": item.get("voice_key") or profile.get("default_female_voice") or "amanda",
                "female_voice": profile.get("default_female_voice") or item.get("voice_key") or "amanda",
                "male_voice": profile.get("default_male_voice") or item.get("voice_key") or "george",
                "role_voices": role_voices,
                "role_configs": role_configs,
                "default_role": item.get("role") or profile.get("default_role"),
            })
        return result

    @staticmethod
    def _legacy_audio_outputs(
        audio: Any,
        item_specs: list[Mapping[str, Any]],
        *,
        quality: Any = None,
    ) -> tuple[bytes, dict[str, bytes], dict[str, Any]]:
        """Encode a decoded composite and cut it only at verified pauses."""

        try:
            import word_tts_app as legacy_ui

            diagnostics: dict[str, Any] = {}
            pieces = legacy_ui.cut_composite_audio(
                audio,
                len(item_specs),
                item_lengths=[len(str(item.get("text") or "")) for item in item_specs],
                diagnostics=diagnostics,
            )
        except Exception as exc:
            raise ProviderError(
                f"讯飞合并音频无法安全切割：{exc}",
                code="SEGMENT_BOUNDARIES_UNVERIFIED",
                details={"cut_diagnostics": diagnostics if "diagnostics" in locals() else {}},
                ambiguous=False,
            ) from exc
        if len(pieces) != len(item_specs):
            raise ProviderError(
                "讯飞合并音频切割数量与输入条目不一致",
                code="SEGMENT_BOUNDARIES_UNVERIFIED",
                ambiguous=False,
            )
        segments = {
            str(item["item_id"]): _audio_to_mp3_bytes(piece, quality=quality)
            for item, piece in zip(item_specs, pieces)
        }
        return _audio_to_mp3_bytes(audio, quality=quality), segments, diagnostics

    @staticmethod
    def _legacy_single_audio_outputs(
        results: Any,
        item_specs: list[Mapping[str, Any]],
        *,
        quality: Any = None,
    ) -> tuple[bytes, dict[str, bytes]]:
        if not isinstance(results, Mapping):
            raise ProviderError("讯飞逐条生成没有返回结果", code="ARTIFACT_INVALID", ambiguous=False)
        try:
            from pydub import AudioSegment
        except ImportError as exc:
            raise ProviderCapabilityError("pydub is required to assemble single-segment output") from exc
        parts = []
        segments: dict[str, bytes] = {}
        for item in item_specs:
            item_id = str(item["item_id"])
            result = results.get(item_id)
            audio = result.get("audio") if isinstance(result, Mapping) else None
            if audio is None:
                message = result.get("error") if isinstance(result, Mapping) else None
                raise ProviderError(
                    str(message or f"讯飞逐条生成缺少条目音频：{item_id}"),
                    code="ARTIFACT_INVALID",
                    ambiguous=False,
                )
            segments[item_id] = _audio_to_mp3_bytes(audio, quality=quality)
            parts.append(audio)
        if not parts:
            raise ProviderError("讯飞逐条生成没有音频段", code="ARTIFACT_INVALID", ambiguous=False)
        full = parts[0]
        for part in parts[1:]:
            if not isinstance(part, AudioSegment):
                raise ProviderError("讯飞逐条生成返回了无效音频对象", code="ARTIFACT_INVALID", ambiguous=False)
            full = full + part
        return _audio_to_mp3_bytes(full, quality=quality), segments

    def _submit_legacy_xunfei(
        self,
        submission_key: str,
        payload: Mapping[str, Any],
        *,
        cancel_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> ProviderReceipt:
        try:
            import xunfei.runtime as legacy
        except ImportError as exc:
            raise ProviderCapabilityError("Xunfei provider package is unavailable") from exc
        item_specs = self._legacy_item_specs(payload)
        works = self._legacy_works(submission_key, payload)
        works_name = str(works[0]["works_name"])
        profile = payload.get("profile") if isinstance(payload.get("profile"), Mapping) else {}
        generation_mode = str(profile.get("generation_mode") or "composite_cut")
        quality = profile.get("quality")
        try:
            if generation_mode == "single_segment":
                import word_tts_app as legacy_ui

                kwargs = {"cancel_check": cancel_check} if callable(cancel_check) else {}
                if callable(progress_callback):
                    kwargs["progress_callback"] = progress_callback
                raw_single = _run_sync(lambda: legacy_ui._synth_items_batch(item_specs, **kwargs))
                output, segments = self._legacy_single_audio_outputs(raw_single, item_specs, quality=quality)
                result = {"audio": output, "segments": segments}
            else:
                kwargs = {"cancel_check": cancel_check} if callable(cancel_check) else {}
                if callable(progress_callback):
                    kwargs["progress_callback"] = progress_callback
                raw = _run_sync(lambda: legacy.synth_xunfei_composite(works, **kwargs))
                result = raw.get(submission_key) if isinstance(raw, Mapping) else None
        except Exception as exc:
            # ``begin_tts_submission`` records the durable intent before the
            # browser is opened, but a cooperative stop while login/editor
            # preparation is still in progress has not clicked Xunfei's
            # confirmation button. Treat that stop like a safe transient
            # failure. Once the session reports a confirmed/uncertain click,
            # retain the conservative ambiguous path to prevent duplicate
            # billing.
            if type(exc).__name__ == "XunfeiCancelled":
                active_session = getattr(legacy, "_session", None)
                submission_confirmed = bool(
                    getattr(active_session, "_confirm_click_succeeded", False)
                    or getattr(active_session, "_submission_state_uncertain", False)
                )
                if not submission_confirmed:
                    raise ProviderError(
                        "讯飞浏览器任务已在提交前取消，可安全重试",
                        code="TRANSIENT_PROVIDER_ERROR",
                        details={"cancelled_before_confirmation": True},
                        ambiguous=False,
                    ) from exc
            active_session = getattr(legacy, "_session", None)
            page_closed = False
            page = getattr(active_session, "_page", None)
            try:
                # Playwright can raise its transport error before the
                # context/page close callback runs.  Check the authoritative
                # page state as well as the callback flag so a manually closed
                # browser is still classified as a safe pre-confirm handoff.
                page_closed = page is not None and page.is_closed() is True
            except Exception:
                page_closed = False
            if getattr(active_session, "_browser_disconnected", False) or page_closed:
                submission_confirmed = bool(
                    getattr(active_session, "_confirm_click_succeeded", False)
                    or getattr(active_session, "_submission_state_uncertain", False)
                )
                if not submission_confirmed:
                    raise ProviderError(
                        "讯飞浏览器已关闭，任务在提交前中断，可安全重试",
                        code="TRANSIENT_PROVIDER_ERROR",
                        details={
                            "cancelled_before_confirmation": True,
                            "browser_disconnected": True,
                        },
                        ambiguous=False,
                    ) from exc
            raise _normalize_legacy_error(exc, works_name=works_name) from exc
        if not isinstance(result, Mapping) or result.get("audio") is None:
            if isinstance(result, Mapping) and result.get("ambiguous_works_id"):
                raise ProviderError(
                    _safe_provider_message(result.get("error"), "讯飞作品已提交但暂时无法定位"),
                    code="SUBMISSION_AMBIGUOUS",
                    details={"works_name": result.get("works_name") or works_name},
                    ambiguous=True,
                )
            raise _normalize_legacy_error(ProviderError(
                str((result or {}).get("error") if isinstance(result, Mapping) else "Xunfei composite submission failed"),
            ), works_name=works_name)
        diagnostics: dict[str, Any] = {}
        if generation_mode == "single_segment":
            output = bytes(result["audio"])
            segments = dict(result.get("segments") or {})
        else:
            output, segments, diagnostics = self._legacy_audio_outputs(
                result["audio"], item_specs, quality=quality,
            )
        formal = str(result.get("works_id") or "") or None
        temporary = str(result.get("temporary_works_id") or "") or None
        canonical = self.tracker.observe(submission_key, temporary_works_id=temporary, formal_works_id=formal)
        return ProviderReceipt(
            self.provider, self.account_scope, submission_key,
            formal or temporary or submission_key, canonical, output,
            temporary, formal, {
                "works_id": formal,
                "temporary_works_id": temporary,
                "works_name": result.get("works_name") or works_name,
                "format": "mp3",
                "segment_boundaries_verified": True,
                "cut_diagnostics": diagnostics,
            },
            segments=segments,
            output_format="mp3",
        )

    def _query_legacy_xunfei(
        self,
        submission_key: str,
        payload: Mapping[str, Any],
        *,
        cancel_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> ProviderReceipt | None:
        try:
            import xunfei.runtime as legacy
        except ImportError as exc:
            raise ProviderCapabilityError("Xunfei provider package is unavailable") from exc
        item_specs = self._legacy_item_specs(payload)
        works = self._legacy_works(submission_key, payload)
        works_name = str(works[0]["works_name"])
        # This is strictly a lookup/download operation.  The legacy resume
        # path searches the existing works list by name and never submits a
        # second work when no unique match is found.
        resume = {
            submission_key: {
                "ambiguous_submission": True,
                "works_name": works_name,
            },
        }
        profile = payload.get("profile") if isinstance(payload.get("profile"), Mapping) else {}
        generation_mode = str(profile.get("generation_mode") or "composite_cut")
        quality = profile.get("quality")
        try:
            if generation_mode == "single_segment":
                # The legacy single-segment API has one worksId per internal
                # speaker segment, while the durable receipt stores one
                # submission key.  Without those per-segment ids it is unsafe
                # to search or submit again after a process restart.
                raise ProviderError(
                    "逐条模式的外部作品 ID 尚未完整持久化，暂不能自动对账；不会重复提交",
                    code="SUBMISSION_AMBIGUOUS",
                    details={"generation_mode": generation_mode},
                    ambiguous=True,
                )
            kwargs = {"resume": resume}
            if callable(cancel_check):
                kwargs["cancel_check"] = cancel_check
            if callable(progress_callback):
                kwargs["progress_callback"] = progress_callback
            raw = _run_sync(lambda: legacy.synth_xunfei_composite(works, **kwargs))
            result = raw.get(submission_key) if isinstance(raw, Mapping) else None
        except Exception as exc:
            raise _normalize_legacy_error(exc, works_name=works_name) from exc
        if not isinstance(result, Mapping) or result.get("audio") is None:
            if isinstance(result, Mapping) and result.get("error"):
                raise ProviderError(
                    _safe_provider_message(result.get("error"), "未能核验讯飞作品"),
                    code="SUBMISSION_AMBIGUOUS",
                    details={"works_name": result.get("works_name") or works_name},
                    ambiguous=True,
                )
            return None
        diagnostics: dict[str, Any] = {}
        if generation_mode == "single_segment":
            output = bytes(result["audio"])
            segments = dict(result.get("segments") or {})
        else:
            output, segments, diagnostics = self._legacy_audio_outputs(
                result["audio"], item_specs, quality=quality,
            )
        formal = str(result.get("works_id") or "") or None
        temporary = str(result.get("temporary_works_id") or "") or None
        canonical = self.tracker.observe(
            submission_key,
            temporary_works_id=temporary,
            formal_works_id=formal,
        )
        receipt = ProviderReceipt(
            self.provider,
            self.account_scope,
            submission_key,
            formal or temporary or submission_key,
            canonical,
            output,
            temporary,
            formal,
            {
                "works_id": formal,
                "temporary_works_id": temporary,
                "works_name": result.get("works_name") or works_name,
                "reconciled": True,
                "format": "mp3",
                "segment_boundaries_verified": True,
                "cut_diagnostics": diagnostics,
            },
            segments=segments,
            output_format="mp3",
        )
        self._receipts[submission_key] = receipt
        return receipt


def _run_sync(awaitable_or_factory: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        awaitable = (
            awaitable_or_factory()
            if callable(awaitable_or_factory)
            else awaitable_or_factory
        )
        return asyncio.run(awaitable)
    # Call sites pass a factory so the coroutine is not constructed at all
    # when synchronous execution is rejected inside an active event loop.
    # Keep closing support for callers that still pass an already-created
    # coroutine during the compatibility window.
    close = getattr(awaitable_or_factory, "close", None)
    if callable(close):
        close()
    raise ProviderError("Xunfei adapter cannot run synchronous browser work inside an active event loop")


def _audio_to_mp3_bytes(audio: Any, *, quality: Any = None) -> bytes:
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    if not hasattr(audio, "export"):
        raise ProviderError("Xunfei adapter received an unsupported audio object", code="ARTIFACT_INVALID")
    output = io.BytesIO()
    bitrate = {
        "48 kbps（低）": "48k",
        "128 kbps（标准）": "128k",
        "192 kbps（高）": "192k",
        "320 kbps（极高）": "320k",
    }.get(str(quality or ""), "128k")
    audio.export(output, format="mp3", bitrate=bitrate)
    data = output.getvalue()
    ArtifactDownloader.verify(data, min_bytes=1)
    return data

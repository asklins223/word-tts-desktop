"""Page-only executor for the first system-input integration.

The durable system-input service owns intent, leases, receipts and recovery.
This module owns only the last-mile browser operation.  It deliberately does
not contain an HTTP client for the external platform: the existing
``platform_entry.paper_input`` module drives visible page controls and observes
only the read-only responses produced by those controls.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import platform_entry.paper_input as page_input
from platform_entry.adapter import textbook_page
from platform_entry.adapter.runtime import PersistentBrowserSession

from .artifact_store import ArtifactStore, ArtifactStoreError
from .data_safety import redact_public_json
from .repositories import RepositoryError
from .system_input import (
    SystemInputError,
    system_input_capabilities,
    system_input_capability,
)
from .system_input_content import (
    PageInputFactsError,
    _page_listening_text,
    page_input_collection_status,
    sanitize_page_input,
)


def _text(value: Any, *, limit: int = 1024) -> str:
    return str(value or "").strip()[:limit]


_LEGACY_PAGE_COLON_SPEAKER_MARKER_RE = re.compile(
    r"(?im)^[ \t]*[WwMm][ \t]*[:：][ \t]*"
)
def _textbook_page_text(value: Any, role: Any = None) -> str:
    """Keep system-input record construction aligned with the page adapter."""

    return textbook_page._textbook_page_text(value, role)


def _system_input_listening_text(page_value: Any, source_value: Any) -> str:
    """Build visible input text and repair labels lost by the old sanitizer.

    Current page facts are authoritative.  A saved fact from the affected
    version can be recognized safely when it is exactly the raw source after
    both colon and parenthesized labels were removed; in that one case,
    restore the raw ``W:``/``M:`` form while still removing ``(W)``/``(M)``.
    """

    page_text = _page_listening_text(page_value)
    source_text = _page_listening_text(source_value)
    if not page_text:
        return source_text
    if (
        source_text
        and _LEGACY_PAGE_COLON_SPEAKER_MARKER_RE.search(source_text)
        and _LEGACY_PAGE_COLON_SPEAKER_MARKER_RE.sub("", source_text).strip()
        == page_text
    ):
        return source_text
    return page_text


# Older input-run snapshots are immutable and were created before the
# listening parser preserved the Word reading-time prompt.  The reviewed
# listening-test template gives each selection/response card five seconds;
# keep that compatibility value at the execution boundary while new
# snapshots continue to use the parser-provided per-question value.
_LISTENING_ANSWER_TIME_FALLBACK_SECONDS = 5


def _safe_error_text(value: Any, *, limit: int = 1200) -> str:
    safe = redact_public_json(str(value or "")[:limit])
    return _text(safe, limit=limit)


def _raise_if_verification_aborted(payload: Mapping[str, Any]) -> None:
    """Honor a durable automatic-verification stop after lock acquisition."""

    check = payload.get("_abort_check") if isinstance(payload, Mapping) else None
    if not callable(check):
        return
    try:
        aborted = bool(check())
    except SystemInputError:
        raise
    except Exception as exc:
        raise SystemInputError(
            "无法确认系统录入是否已停止",
            code="INPUT_VERIFY_FAILED",
            details={
                "error_type": type(exc).__name__,
                "error_message": _safe_error_text(exc),
            },
        ) from exc
    if aborted:
        raise SystemInputError(
            "系统录入已停止，已取消自动核验",
            code="INPUT_RUN_STOPPED",
        )


def _safe_page_url(page: Any) -> str:
    """Return a diagnostic-only page URL without query credentials."""

    try:
        raw = _text(getattr(page, "url", ""), limit=2048)
        if not raw:
            return ""
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except Exception:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return ""
    host = hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if port is not None:
        netloc = f"{netloc}:{port}"
    fragment = _text(parsed.fragment, limit=256)
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", fragment))


def _artifact_error_details(
    repository: Any,
    artifact_id: str,
    workflow_id: str,
    exc: Exception,
) -> dict[str, Any]:
    """Add enough safe ownership/cause context to diagnose an artifact gate."""

    details: dict[str, Any] = {
        "artifact_id": artifact_id,
        "workflow_id": workflow_id,
        "cause_type": type(exc).__name__,
        "cause_message": _safe_error_text(exc),
    }
    try:
        storage = repository.get_artifact_storage(artifact_id)
    except Exception:
        storage = None
    if isinstance(storage, Mapping):
        owner = _text(storage.get("workflow_id"), limit=256)
        if owner and owner != workflow_id:
            details["artifact_owner_workflow_id"] = owner
    return details


def _preflight_error_details(exc: Exception, *, step: str, page: Any = None) -> dict[str, Any]:
    details: dict[str, Any] = {
        "phase": "preflight",
        "step": _text(step, limit=128) or "unknown",
        "error_type": type(exc).__name__,
        "error_message": _safe_error_text(exc),
    }
    page_url = _safe_page_url(page)
    if page_url:
        details["page_url"] = page_url
    return details


def _choice(value: Any, field: str) -> dict[str, Any]:
    """Turn a saved display choice into the page script's semantic choice."""

    if isinstance(value, Mapping):
        label = value.get("name", value.get("label", value.get("text")))
        identifier = value.get("id", value.get("value"))
        label_text = _text(label, limit=256)
        if not label_text:
            raise SystemInputError(
                f"{field} 缺少页面显示名称，不能安全提交",
                code="SYSTEM_INPUT_CONFIG_INCOMPLETE",
                details={"field": field},
            )
        if identifier is None or isinstance(identifier, bool) or not _text(identifier, limit=256):
            raise SystemInputError(
                f"{field} 缺少稳定 ID，不能安全提交",
                code="SYSTEM_INPUT_CONFIG_INCOMPLETE",
                details={"field": field},
            )
        result: dict[str, Any] = {"name": label_text}
        result["id"] = identifier
        return result
    raise SystemInputError(
        f"{field} 缺少页面显示名称和稳定 ID，不能安全提交",
        code="SYSTEM_INPUT_CONFIG_INCOMPLETE",
        details={"field": field},
    )


def _choice_list(value: Any, field: str, *, required: bool = True) -> list[dict[str, Any]]:
    if value in (None, "") and not required:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SystemInputError(
            f"{field} 必须是页面选项数组",
            code="SYSTEM_INPUT_CONFIG_INCOMPLETE",
            details={"field": field},
        )
    if not value and required:
        raise SystemInputError(
            f"{field} 不能为空",
            code="SYSTEM_INPUT_CONFIG_INCOMPLETE",
            details={"field": field},
        )
    return [_choice(item, f"{field}[{index}]") for index, item in enumerate(value)]


def _integer_or_none(value: Any, field: str, *, minimum: int | None = None) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise SystemInputError(f"{field} 必须是整数", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SystemInputError(f"{field} 必须是整数", code="SYSTEM_INPUT_CONFIG_INCOMPLETE") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise SystemInputError(f"{field} 必须是整数", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
    number = int(number)
    if minimum is not None and number < minimum:
        raise SystemInputError(f"{field} 不能小于 {minimum}", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
    return number


def _configuration_for_unit(unit: Mapping[str, Any]) -> dict[str, Any]:
    value = unit.get("configuration")
    return dict(value) if isinstance(value, Mapping) else {}


def _page_result_feedback(result: Mapping[str, Any]) -> dict[str, Any]:
    feedback = result.get("feedback")
    safe = redact_public_json(feedback if isinstance(feedback, Mapping) else {})
    return dict(safe) if isinstance(safe, Mapping) else {}


_PROFILE_LOCKS: dict[str, threading.RLock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()


def _profile_execution_lock(profile_dir: Path) -> threading.RLock:
    """Serialize persistent-context use for one profile within this process.

    Playwright cannot safely launch two persistent contexts against the same
    Chromium user-data directory. More importantly, concurrent UI flows
    would otherwise interleave edits in one account. The durable record lease
    protects the target record; this lock protects the shared browser profile.
    """

    key = str(profile_dir.expanduser().resolve())
    with _PROFILE_LOCKS_GUARD:
        lock = _PROFILE_LOCKS.get(key)
        if lock is None:
            # A run keeps the profile lock for the lifetime of its reusable
            # browser session.  The individual preflight/page methods also
            # take this lock around each Playwright call, so the worker must
            # be able to re-enter it on the same thread.  A plain Lock here
            # self-deadlocks at the first preflight and leaves a stop request
            # stuck in the renderer forever.
            lock = threading.RLock()
            _PROFILE_LOCKS[key] = lock
        return lock


class _ReusableBrowserSessionMixin:
    """Keep one visible browser context for all units in an input run."""

    def _init_reusable_browser_sessions(self) -> None:
        self._browser_sessions: dict[str, PersistentBrowserSession] = {}
        self._browser_sessions_guard = threading.Lock()
        self._managed_browser_runs: set[str] = set()
        self._browser_launcher = getattr(self, "_browser_launcher", None)

    @staticmethod
    def _browser_run_key(input_run_id: Any) -> str:
        return _text(input_run_id, limit=256) or "__direct__"

    def begin_run(self, input_run_id: Any) -> None:
        key = self._browser_run_key(input_run_id)
        # The session remains alive between preflight and every unit execution,
        # so a short method-level lock is not enough: another run could launch
        # a second persistent context against this same profile in the gap.
        # RLock keeps the existing method-level guards re-entrant on the worker
        # thread while reserving the profile for this run until end_run().
        self._profile_lock.acquire()
        try:
            with self._browser_sessions_guard:
                previous = self._browser_sessions.pop(key, None)
                self._managed_browser_runs.add(key)
                self._browser_sessions[key] = PersistentBrowserSession(
                    self.profile_dir,
                    launch_browser=self._browser_launcher,
                )
            if previous is not None:
                previous.close()
        except Exception:
            with self._browser_sessions_guard:
                self._managed_browser_runs.discard(key)
                self._browser_sessions.pop(key, None)
            self._profile_lock.release()
            raise

    def _browser_session_for_payload(self, payload: Mapping[str, Any]) -> PersistentBrowserSession:
        key = self._browser_run_key(payload.get("input_run_id"))
        with self._browser_sessions_guard:
            session = self._browser_sessions.get(key)
            if session is None:
                session = PersistentBrowserSession(
                    self.profile_dir,
                    launch_browser=self._browser_launcher,
                )
                self._browser_sessions[key] = session
            return session

    def end_run(self, input_run_id: Any) -> None:
        key = self._browser_run_key(input_run_id)
        with self._browser_sessions_guard:
            session = self._browser_sessions.pop(key, None)
            managed = key in self._managed_browser_runs
            self._managed_browser_runs.discard(key)
        try:
            if session is not None:
                session.close()
        finally:
            if managed:
                self._profile_lock.release()


class SystemInputExecutorRouter:
    """Route a durable input payload to a registered page adapter.

    The service intentionally injects one callable executor.  Keeping this
    router at that boundary lets the application add a textbook or vocabulary
    adapter later without teaching the durable state machine about every page
    layout.  Adapter registration is code-owned; user configuration can only
    select a registered capability and cannot provide an import path or a
    callable.
    """

    # The durable service may attach a private, non-serialized callback to a
    # real page payload. Custom test/integration executors do not opt into the
    # callback, so their payload contract remains JSON-safe and unchanged.
    accepts_input_run_control = True

    def __init__(self, adapters: Mapping[str, Any] | None = None) -> None:
        self._adapters: dict[str, Any] = {
            str(key).strip(): value
            for key, value in dict(adapters or {}).items()
            if str(key).strip() and value is not None
        }

    def register(self, adapter_key: str, adapter: Any) -> None:
        key = _text(adapter_key, limit=128)
        if not key or adapter is None:
            raise ValueError("adapter_key and adapter are required")
        if key in self._adapters:
            raise ValueError(f"system input adapter already registered: {key}")
        self._adapters[key] = adapter

    def _input_type_for_payload(self, payload: Mapping[str, Any]) -> str:
        unit = payload.get("unit")
        if isinstance(unit, Mapping):
            configuration = _configuration_for_unit(unit)
            value = configuration.get("input_type") or unit.get("input_type")
            if value:
                return _text(value, limit=32).casefold()
        target = payload.get("target")
        if isinstance(target, Mapping):
            return _text(target.get("input_type"), limit=32).casefold()
        return ""

    def _adapter_for_payload(self, payload: Mapping[str, Any]) -> Any:
        input_type = self._input_type_for_payload(payload)
        capability = system_input_capability(input_type)
        if capability is None:
            raise SystemInputError(
                "当前录入类型没有注册能力",
                code="SYSTEM_INPUT_TYPE_UNSUPPORTED",
                details={"input_type": input_type},
            )
        if not capability.get("external_supported"):
            raise SystemInputError(
                capability.get("reason") or "当前录入类型暂不支持外部录入",
                code="SYSTEM_INPUT_TYPE_UNSUPPORTED",
                details={"input_type": input_type},
            )
        adapter_key = _text(capability.get("adapter_key"), limit=128)
        adapter = self._adapters.get(adapter_key)
        if adapter is None:
            raise SystemInputError(
                "当前录入类型的页面适配器未连接",
                code="SYSTEM_INPUT_TYPE_UNSUPPORTED",
                details={"input_type": input_type, "adapter_key": adapter_key},
            )
        return adapter

    def supports_input_type(self, input_type: str) -> bool:
        capability = system_input_capability(input_type)
        if capability is None or not capability.get("external_supported"):
            return False
        return _text(capability.get("adapter_key"), limit=128) in self._adapters

    def capabilities(self) -> list[dict[str, Any]]:
        """Expose registered adapter availability without adapter internals."""

        result = []
        for capability in system_input_capabilities():
            row = dict(capability)
            row["executor_available"] = self.supports_input_type(str(row.get("input_type") or ""))
            result.append(row)
        return result

    def begin_run(self, input_run_id: Any, input_type: Any) -> None:
        capability = system_input_capability(_text(input_type, limit=32).casefold())
        if not isinstance(capability, Mapping):
            return
        adapter = self._adapters.get(_text(capability.get("adapter_key"), limit=128))
        begin = getattr(adapter, "begin_run", None)
        if callable(begin):
            begin(input_run_id)

    def end_run(self, input_run_id: Any, input_type: Any) -> None:
        capability = system_input_capability(_text(input_type, limit=32).casefold())
        if not isinstance(capability, Mapping):
            return
        adapter = self._adapters.get(_text(capability.get("adapter_key"), limit=128))
        end = getattr(adapter, "end_run", None)
        if callable(end):
            end(input_run_id)

    def preflight(self, payload: Mapping[str, Any]) -> None:
        adapter = self._adapter_for_payload(payload)
        preflight = getattr(adapter, "preflight", None)
        if callable(preflight):
            preflight(payload)

    def __call__(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        adapter = self._adapter_for_payload(payload)
        if callable(adapter):
            return adapter(payload)
        execute = getattr(adapter, "execute", None)
        if callable(execute):
            return execute(payload)
        raise SystemInputError(
            "页面录入适配器没有可执行入口",
            code="INPUT_EXECUTOR_UNAVAILABLE",
        )

    def verify(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Run the adapter's read-only record verification when supported."""

        adapter = self._adapter_for_payload(payload)
        verify = getattr(adapter, "verify_record", None)
        if not callable(verify):
            # 老的试卷适配器只提供 verify_paper 命名；保留兼容入口。
            verify = getattr(adapter, "verify_paper", None)
        if not callable(verify):
            raise SystemInputError(
                "页面录入适配器不支持只读核验",
                code="INPUT_VERIFY_UNSUPPORTED",
            )
        return verify(payload)


def _materialize_audio_file(
    repository: Any,
    artifacts: ArtifactStore,
    artifact_id: str,
    workflow_id: str,
    destination: Path,
) -> None:
    """Materialize one verified tts-segment artifact for a page upload."""

    try:
        storage = repository.get_artifact_storage(artifact_id, workflow_id=workflow_id)
    except Exception as exc:
        raise SystemInputError(
            "录入音频产物无法读取",
            code="ARTIFACT_INVALID",
            details=_artifact_error_details(repository, artifact_id, workflow_id, exc),
        ) from exc
    if not isinstance(storage, Mapping):
        raise SystemInputError(
            "录入音频产物元数据格式无效",
            code="ARTIFACT_INVALID",
            details={"artifact_id": artifact_id},
        )
    if str(storage.get("artifact_type") or "") != "tts-segment":
        raise SystemInputError(
            "系统录入只能使用 tts-segment 音频产物",
            code="ARTIFACT_INVALID",
            details={"artifact_id": artifact_id},
        )
    if _text(storage.get("format"), limit=16).lower().lstrip(".") != "mp3":
        raise SystemInputError(
            "系统录入原文音频必须是 MP3",
            code="ARTIFACT_FORMAT_UNSUPPORTED",
            details={"artifact_id": artifact_id},
        )

    storage_key = _text(storage.get("storage_key"), limit=512)
    try:
        expected_size = int(storage.get("size_bytes") or -1)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SystemInputError(
            "录入音频产物大小元数据无效",
            code="ARTIFACT_INVALID",
            details={"artifact_id": artifact_id},
        ) from exc
    expected_sha = _text(storage.get("sha256"), limit=128).lower()
    if not storage_key or expected_size < 0 or len(expected_sha) != 64:
        raise SystemInputError(
            "录入音频产物缺少完整校验信息",
            code="ARTIFACT_INVALID",
            details={"artifact_id": artifact_id},
        )

    digest = hashlib.sha256()
    size = 0
    try:
        with artifacts.read(storage_key) as source, destination.open("xb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            target.flush()
            os.fsync(target.fileno())
    except (OSError, ArtifactStoreError, KeyError, TypeError, ValueError) as exc:
        raise SystemInputError(
            "录入音频产物无法 materialize 到页面上传临时目录",
            code="ARTIFACT_INVALID",
            details=_artifact_error_details(repository, artifact_id, workflow_id, exc),
        ) from exc
    if size != expected_size or digest.hexdigest() != expected_sha:
        raise SystemInputError(
            "录入音频产物在读取时校验失败",
            code="ARTIFACT_METADATA_CONFLICT",
            details={"artifact_id": artifact_id},
        )


class PlatformInputWorkflowPageExecutor(_ReusableBrowserSessionMixin):
    """Execute one recorded input unit through the visible PlatformInput page."""

    input_type = "paper"
    adapter_key = "platform_input.paper"
    accepts_input_run_control = True

    def __init__(
        self,
        repository: Any,
        artifacts: ArtifactStore,
        *,
        profile_dir: Path,
        admin_url: str,
        api_base: str,
        login_timeout: float,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.profile_dir = profile_dir
        self.admin_url = admin_url
        self.api_base = api_base
        self.login_timeout = max(1.0, float(login_timeout))
        self._profile_lock = _profile_execution_lock(profile_dir)
        # Keep the historical patch point used by integrations/tests while
        # the session wrapper takes ownership of the actual context lifetime.
        # Resolve the compatibility patch point at launch time.  Existing
        # integrations replace ``platform_entry.paper_input._launch_browser``
        # after constructing the executor (and the real callable can also be
        # refreshed by an embedding application), so do not freeze the
        # function object during construction.
        self._browser_launcher = lambda playwright, profile_dir: page_input._launch_browser(
            playwright,
            profile_dir,
        )
        self._init_reusable_browser_sessions()

    @staticmethod
    def _edit_existing_requested(payload: Mapping[str, Any]) -> bool:
        """Use the durable mapping as the retry/edit decision.

        The page script must not decide to create a second paper merely because
        a worker was restarted.  A known external record is the durable fact
        that an earlier attempt already created the paper; in that case the
        visible-page flow is restricted to opening an editable existing row.
        """

        entry = payload.get("entry")
        if not isinstance(entry, Mapping):
            return False
        return bool(_text(entry.get("external_record_id"), limit=512))

    def _validated_spec(self, payload: Mapping[str, Any], base_dir: Path) -> Any:
        """Build and validate page semantics before opening or writing a page.

        ``normalize_spec`` is the script's single page-schema gate.  Keeping
        this call here, before Playwright is launched and before the durable
        external write fence, means missing stems/options/answers/reference
        answers fail as a local retryable error rather than an ambiguous page
        operation.
        """

        raw_spec = self._build_raw_spec(payload, base_dir)
        try:
            return page_input.normalize_spec(raw_spec, base_dir=base_dir)
        except page_input.PlatformInputError as exc:
            raise SystemInputError(
                "页面录入内容未通过本地完整性校验",
                code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                details={
                    "workflow_id": _text(payload.get("workflow_id"), limit=256),
                    "cause_type": type(exc).__name__,
                    "cause_message": _safe_error_text(exc),
                },
            ) from exc

    def _materialize_audio(self, artifact_id: str, workflow_id: str, destination: Path) -> None:
        _materialize_audio_file(
            self.repository,
            self.artifacts,
            artifact_id,
            workflow_id,
            destination,
        )

    def _materialize_page_image(
        self,
        artifact_id: str,
        workflow_id: str,
        destination: Path,
    ) -> str:
        """Materialize one trusted image reference for the page upload control.

        Page facts persist only an internal artifact ID.  The local path exists
        for the duration of one adapter attempt and is never accepted from a
        renderer/configuration payload.  A dedicated artifact type and small
        format/size allowlist keep an arbitrary source or archive from being
        uploaded as an image by mistake.
        """

        try:
            storage = self.repository.get_artifact_storage(artifact_id, workflow_id=workflow_id)
        except Exception as exc:
            raise SystemInputError(
                "信息记录表图片产物无法读取",
                code="ARTIFACT_INVALID",
                details=_artifact_error_details(self.repository, artifact_id, workflow_id, exc),
            ) from exc
        if not isinstance(storage, Mapping):
            raise SystemInputError(
                "信息记录表图片产物元数据格式无效",
                code="ARTIFACT_INVALID",
                details={"artifact_id": artifact_id},
            )
        if str(storage.get("artifact_type") or "") != "system-input-image":
            raise SystemInputError(
                "信息记录表图片不是受信任的页面图片产物",
                code="ARTIFACT_INVALID",
                details={"artifact_id": artifact_id},
            )
        image_format = _text(storage.get("format"), limit=16).lower().lstrip(".")
        if image_format not in {"jpg", "jpeg", "png"}:
            raise SystemInputError(
                "信息记录表图片只支持 JPG/PNG",
                code="ARTIFACT_FORMAT_UNSUPPORTED",
                details={"artifact_id": artifact_id},
            )
        storage_key = _text(storage.get("storage_key"), limit=512)
        try:
            expected_size = int(storage.get("size_bytes") or -1)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SystemInputError(
                "信息记录表图片大小元数据无效",
                code="ARTIFACT_INVALID",
                details={"artifact_id": artifact_id},
            ) from exc
        expected_sha = _text(storage.get("sha256"), limit=128).lower()
        if not storage_key or expected_size < 0 or expected_size > 16 * 1024 * 1024 or len(expected_sha) != 64:
            raise SystemInputError(
                "信息记录表图片产物校验信息无效",
                code="ARTIFACT_INVALID",
                details={"artifact_id": artifact_id},
            )

        digest = hashlib.sha256()
        size = 0
        try:
            with self.artifacts.read(storage_key) as source, destination.open("xb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                target.flush()
                os.fsync(target.fileno())
        except (OSError, ArtifactStoreError, KeyError, TypeError, ValueError) as exc:
            raise SystemInputError(
                "信息记录表图片无法 materialize 到页面上传临时目录",
                code="ARTIFACT_INVALID",
                details=_artifact_error_details(self.repository, artifact_id, workflow_id, exc),
            ) from exc
        if size != expected_size or digest.hexdigest() != expected_sha:
            raise SystemInputError(
                "信息记录表图片产物在读取时校验失败",
                code="ARTIFACT_METADATA_CONFLICT",
                details={"artifact_id": artifact_id},
            )
        return image_format

    def _materialize_page_assets(
        self,
        page_input: Mapping[str, Any],
        workflow_id: str,
        base_dir: Path,
        ordinal: int,
    ) -> dict[str, Any]:
        """Resolve internal page asset references without persisting paths."""

        if str(page_input.get("type") or "") not in {
            "听后记录并转述信息",
            "信息转述及询问",
        }:
            return dict(page_input)
        recording = page_input.get("recording")
        if not isinstance(recording, Mapping):
            return dict(page_input)
        artifact_id = _text(recording.get("image_artifact_id"), limit=256)
        if not artifact_id:
            return dict(page_input)
        destination_base = base_dir / f"segment-{ordinal + 1}-table"
        # The extension is replaced after the trusted artifact metadata is
        # checked; the temporary directory remains private to this attempt.
        destination = destination_base.with_suffix(".png")
        image_format = self._materialize_page_image(artifact_id, workflow_id, destination)
        if image_format != "png":
            renamed = destination_base.with_suffix(f".{image_format}")
            destination.replace(renamed)
            destination = renamed
        result = dict(page_input)
        result_recording = dict(recording)
        result_recording.pop("image_artifact_id", None)
        result_recording["image_path"] = str(destination)
        result["recording"] = result_recording
        return result

    @staticmethod
    def _build_grouped_page_content(
        facts_with_audio: Sequence[tuple[Mapping[str, Any], str, Mapping[str, Any]]],
        audio_paths_by_stem: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Join parser facts to materialized audio without flattening groups."""

        audio_paths_by_stem = dict(audio_paths_by_stem or {})
        selection_materials: list[dict[str, Any]] = []
        info_acquisition_materials: list[dict[str, Any]] = []
        response_questions: list[dict[str, Any]] = []
        imitation_questions: list[dict[str, Any]] = []
        record_groups: list[dict[str, Any]] = []

        for facts, audio_path, segment in facts_with_audio:
            group_type = str(facts.get("type") or "")
            if group_type == "听后选择":
                raw_materials = facts.get("materials")
                if not isinstance(raw_materials, Sequence) or isinstance(raw_materials, (str, bytes, bytearray)):
                    raw_materials = []
                for raw_material in raw_materials:
                    if not isinstance(raw_material, Mapping):
                        continue
                    material = dict(raw_material)
                    material["audio_path"] = audio_path
                    # TTS text may intentionally retain speaker markers for
                    # voice routing, but the visible platform field must not
                    # receive those ``(W)/(M)`` labels.  Re-apply this at the
                    # final page boundary for older saved snapshots too.
                    material["listening_text"] = _system_input_listening_text(
                        material.get("listening_text"),
                        segment.get("raw_text") or segment.get("tts_text"),
                    )
                    raw_questions = material.get("questions")
                    if isinstance(raw_questions, Sequence) and not isinstance(
                        raw_questions,
                        (str, bytes, bytearray),
                    ):
                        # The selection Word prompts are either “5 seconds
                        # for this question” or “10 seconds for these two
                        # questions”.  The parser has already split the
                        # latter into 5 seconds per question.  This fallback
                        # only covers immutable snapshots produced before
                        # that parser field existed and never overwrites an
                        # explicit value.
                        material["questions"] = [
                            {
                                **dict(question),
                                "answer_time": question.get(
                                    "answer_time",
                                    _LISTENING_ANSWER_TIME_FALLBACK_SECONDS,
                                ),
                            }
                            for question in raw_questions
                            if isinstance(question, Mapping)
                        ]
                    selection_materials.append(material)
            elif group_type == "信息获取":
                raw_materials = facts.get("materials")
                if not isinstance(raw_materials, Sequence) or isinstance(raw_materials, (str, bytes, bytearray)):
                    raw_materials = []
                for raw_material in raw_materials:
                    if not isinstance(raw_material, Mapping):
                        continue
                    material = dict(raw_material)
                    material["audio_path"] = audio_path
                    material["listening_text"] = _system_input_listening_text(
                        material.get("listening_text"),
                        segment.get("raw_text") or segment.get("tts_text"),
                    )
                    raw_questions = material.get("questions")
                    if isinstance(raw_questions, Sequence) and not isinstance(
                        raw_questions,
                        (str, bytes, bytearray),
                    ):
                        questions = []
                        for raw_question in raw_questions:
                            if not isinstance(raw_question, Mapping):
                                continue
                            question = dict(raw_question)
                            prompt_stem = _text(
                                question.get("prompt_audio_filename_stem"),
                                limit=256,
                            )
                            if prompt_stem and prompt_stem in audio_paths_by_stem:
                                question["prompt_audio_path"] = audio_paths_by_stem[prompt_stem]
                            questions.append(question)
                        material["questions"] = questions
                    info_acquisition_materials.append(material)
            elif group_type == "听后应答":
                raw_questions = facts.get("questions")
                if not isinstance(raw_questions, Sequence) or isinstance(raw_questions, (str, bytes, bytearray)):
                    raw_questions = []
                for raw_question in raw_questions:
                    if not isinstance(raw_question, Mapping):
                        continue
                    question = dict(raw_question)
                    question["audio_path"] = audio_path
                    question["listening_text"] = _system_input_listening_text(
                        question.get("listening_text"),
                        segment.get("raw_text") or segment.get("tts_text"),
                    )
                    if question.get("score") is None and segment.get("score") is not None:
                        question["score"] = segment.get("score")
                    if question.get("answer_time") is None:
                        # The reviewed 2026 response section also gives five
                        # seconds per sentence.  Preserve explicit parser
                        # values, while keeping immutable pre-upgrade input
                        # snapshots executable.
                        question["answer_time"] = _LISTENING_ANSWER_TIME_FALLBACK_SECONDS
                    response_questions.append(question)
            elif group_type == "模仿朗读":
                raw_questions = facts.get("questions")
                if not isinstance(raw_questions, Sequence) or isinstance(raw_questions, (str, bytes, bytearray)):
                    raw_questions = []
                for raw_question in raw_questions:
                    if not isinstance(raw_question, Mapping):
                        continue
                    question = dict(raw_question)
                    question["audio_path"] = audio_path
                    question["listening_text"] = _system_input_listening_text(
                        question.get("listening_text"),
                        segment.get("raw_text") or segment.get("tts_text"),
                    )
                    if question.get("score") is None and segment.get("score") is not None:
                        question["score"] = segment.get("score")
                    imitation_questions.append(question)
            elif group_type == "听后记录并转述信息":
                raw_recording = facts.get("recording")
                recording = dict(raw_recording) if isinstance(raw_recording, Mapping) else {}
                recording["audio_path"] = audio_path
                recording["listening_text"] = _system_input_listening_text(
                    recording.get("listening_text"),
                    segment.get("raw_text") or segment.get("tts_text"),
                )
                record_group: dict[str, Any] = {
                    "type": group_type,
                    "recording": recording,
                }
                if isinstance(facts.get("retelling"), Mapping):
                    record_group["retelling"] = dict(facts["retelling"])
                record_groups.append(record_group)
            elif group_type == "信息转述及询问":
                raw_recording = facts.get("recording")
                recording = dict(raw_recording) if isinstance(raw_recording, Mapping) else {}
                recording["audio_path"] = audio_path
                recording["listening_text"] = _system_input_listening_text(
                    recording.get("listening_text"),
                    segment.get("raw_text") or segment.get("tts_text"),
                )
                instruction_stem = _text(
                    recording.get("instruction_audio_filename_stem"),
                    limit=256,
                )
                if instruction_stem and instruction_stem in audio_paths_by_stem:
                    recording["instruction_audio_path"] = audio_paths_by_stem[instruction_stem]
                asking_instruction_stem = _text(
                    recording.get("asking_instruction_audio_filename_stem"),
                    limit=256,
                )
                if (
                    asking_instruction_stem
                    and asking_instruction_stem in audio_paths_by_stem
                ):
                    recording["asking_instruction_audio_path"] = audio_paths_by_stem[
                        asking_instruction_stem
                    ]
                retelling = facts.get("retelling")
                record_group = {
                    "type": group_type,
                    "recording": recording,
                }
                if isinstance(retelling, Mapping):
                    normalized_retelling = dict(retelling)
                    prompt_stem = _text(
                        normalized_retelling.get("prompt_audio_filename_stem"),
                        limit=256,
                    )
                    if prompt_stem and prompt_stem in audio_paths_by_stem:
                        normalized_retelling["prompt_audio_path"] = audio_paths_by_stem[prompt_stem]
                    record_group["retelling"] = normalized_retelling
                asking = facts.get("asking")
                if isinstance(asking, Sequence) and not isinstance(asking, (str, bytes, bytearray)):
                    record_group["asking"] = [
                        dict(item) for item in asking if isinstance(item, Mapping)
                    ]
                record_groups.append(record_group)

        groups: list[dict[str, Any]] = []
        if selection_materials:
            groups.append({"type": "听后选择", "materials": selection_materials})
        if response_questions:
            groups.append({"type": "听后应答", "questions": response_questions})
        if imitation_questions:
            groups.append({"type": "模仿朗读", "questions": imitation_questions})
        if info_acquisition_materials:
            groups.append({"type": "信息获取", "materials": info_acquisition_materials})
        groups.extend(record_groups)
        return groups

    @staticmethod
    def _special_template_group_types(template_name: str) -> set[str]:
        """Map a题型专项 template to the page-content family it owns."""

        name = _text(template_name, limit=256).casefold()
        template_tokens = {
            "听后选择": ("听后选择",),
            "听后应答": ("听后应答",),
            "模仿朗读": ("模仿朗读",),
            "听后记录并转述信息": ("听后记录并转述信息", "信息转述", "听后记录"),
            "信息获取": ("信息获取",),
            "信息转述及询问": ("信息转述及询问", "信息转述及询问题"),
        }
        return {
            group_type
            for group_type, tokens in template_tokens.items()
            if any(token.casefold() in name for token in tokens)
        }

    def _build_raw_spec(self, payload: Mapping[str, Any], base_dir: Path) -> dict[str, Any]:
        workflow_id = _text(payload.get("target", {}).get("workflow_id") if isinstance(payload.get("target"), Mapping) else "")
        workflow_id = workflow_id or _text(payload.get("workflow_id"))
        unit = payload.get("unit")
        if not isinstance(unit, Mapping):
            raise SystemInputError("系统录入目标单元不存在", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
        configuration = _configuration_for_unit(unit)
        input_type = _text(configuration.get("input_type") or unit.get("input_type"), limit=32)
        if input_type != "paper":
            raise SystemInputError(
                "当前页面执行器只支持试卷录入",
                code="SYSTEM_INPUT_TYPE_UNSUPPORTED",
                details={"input_type": input_type},
            )
        category = _text(configuration.get("paperCategory") or configuration.get("paper_category"), limit=64) or "题型专项"
        template_name = _text(
            configuration.get("platformTemplateName")
            or configuration.get("platform_template_name"),
            limit=256,
        )
        if not template_name:
            raise SystemInputError("录入题型专项模板未配置", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
        # ``unit_label`` is a parsed boundary label, not a user-supplied paper
        # name. Do not silently turn it into the platform title when the
        # required configuration field is blank.
        paper_name = _text(configuration.get("paperName"), limit=256)
        if not paper_name:
            raise SystemInputError("试卷名称未配置", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")

        districts = _choice_list(configuration.get("districtIds"), "districtIds", required=False)
        paper: dict[str, Any] = {
            "title": paper_name,
            "province": _choice(configuration.get("provinceId"), "provinceId"),
            "city": _choice(configuration.get("cityId"), "cityId"),
            "districts": districts,
            "stage": _choice(configuration.get("stageId"), "stageId"),
            "grade": _choice(configuration.get("gradeId"), "gradeId"),
            "year": _integer_or_none(configuration.get("year"), "year", minimum=2000),
            "duration": _integer_or_none(configuration.get("answerTimeMinutes"), "answerTimeMinutes", minimum=1),
        }
        if category == "听说考试":
            paper["paper_type"] = _choice(configuration.get("paperType"), "paperType")

        # Keep platform template identity/version as bounded metadata for the
        # code-owned paper-rule registry.  The visible page still resolves the
        # template by its page controls; these values are never sent as an API
        # payload or treated as a user-supplied selector/import path.
        raw_spec_metadata: dict[str, str] = {}
        for config_key, spec_key in (
            ("platformTemplateId", "template_id"),
            ("platformTemplateVersion", "template_version"),
        ):
            value = _text(configuration.get(config_key), limit=256)
            if value:
                raw_spec_metadata[spec_key] = value

        raw_items: list[dict[str, Any]] = []
        facts_with_audio: list[tuple[Mapping[str, Any], str, Mapping[str, Any]]] = []
        audio_paths_by_stem: dict[str, str] = {}
        # Keep the sanitized durable facts for completeness validation.  The
        # page payload below intentionally replaces an internal image artifact
        # ID with a private temporary image_path before it is handed to the
        # page script; validating that materialized payload would therefore
        # incorrectly report a required table image as missing.
        validated_page_inputs: list[Mapping[str, Any]] = []
        structured_fact_count = 0
        segments = unit.get("segments")
        if isinstance(segments, (str, bytes)) or not isinstance(segments, Sequence) or not segments:
            raise SystemInputError("当前录入单元没有可提交的内容片段", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")

        special_group_types = (
            self._special_template_group_types(template_name)
            if category == "题型专项"
            else set()
        )
        if category == "题型专项" and not special_group_types:
            raw_group_types = {
                _text(segment.get("page_input", {}).get("type"), limit=64)
                for segment in segments
                if isinstance(segment, Mapping)
                and isinstance(segment.get("page_input"), Mapping)
                and _text(segment.get("page_input", {}).get("type"), limit=64)
            }
            if len(raw_group_types) > 1:
                raise SystemInputError(
                    "题型专项模板无法对应当前文档中的多个题型，请选择对应题型模板",
                    code="SYSTEM_INPUT_CONFIG_INCOMPLETE",
                    details={"template_name": template_name, "page_types": sorted(raw_group_types)},
                )
        for ordinal, segment in enumerate(segments):
            if not isinstance(segment, Mapping):
                raise SystemInputError("内容片段格式无效", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")

            # Determine the selected family before touching audio/image
            # artifacts.  An imitation-reading special run must not fail on
            # an unrelated record-table image in the same source document.
            raw_page_input = segment.get("page_input")
            page_input: dict[str, Any] | None = None
            audio_only_auxiliary = segment.get("audio_only_auxiliary") is True
            if isinstance(raw_page_input, Mapping):
                raw_group_type = _text(raw_page_input.get("type"), limit=64)
                if special_group_types and raw_group_type not in special_group_types:
                    continue
                if not audio_only_auxiliary:
                    structured_fact_count += 1
                try:
                    page_input = sanitize_page_input(raw_page_input)
                except PageInputFactsError as exc:
                    raise SystemInputError(
                        f"第 {ordinal + 1} 个内容片段的页面内容事实无效",
                        code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                    ) from exc
                if page_input is None:
                    raise SystemInputError(
                        f"第 {ordinal + 1} 个内容片段缺少页面内容事实",
                        code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                    )
                if not audio_only_auxiliary:
                    validated_page_inputs.append(page_input)
            elif special_group_types and audio_only_auxiliary:
                category_hint = _text(
                    segment.get("category") or segment.get("item_type"),
                    limit=128,
                )
                if not any(
                    group_type == "信息转述及询问"
                    and (
                        "信息转述" in category_hint
                        or "询问信息" in category_hint
                    )
                    for group_type in special_group_types
                ):
                    continue
            text = _text(segment.get("raw_text") or segment.get("tts_text"), limit=1_000_000)
            if not text:
                raise SystemInputError(
                    f"第 {ordinal + 1} 个内容片段没有听力原文",
                    code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                )
            score = segment.get("score")
            if score is None and not audio_only_auxiliary:
                raise SystemInputError(
                    f"第 {ordinal + 1} 个内容片段没有分数",
                    code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                )
            artifact_id = _text(segment.get("audio_artifact_id"), limit=256)
            if not artifact_id:
                raise SystemInputError(
                    f"第 {ordinal + 1} 个内容片段没有原文音频产物",
                    code="AUDIO_GATE_FAILED",
                )
            audio_path = base_dir / f"segment-{ordinal + 1}.mp3"
            self._materialize_audio(artifact_id, workflow_id, audio_path)
            stem = _text(
                segment.get("audio_filename_stem")
                or segment.get("filename_stem"),
                limit=256,
            )
            if stem:
                audio_paths_by_stem[stem] = str(audio_path)
            if audio_only_auxiliary:
                continue
            try:
                score_number = float(score)
            except (TypeError, ValueError, OverflowError) as exc:
                raise SystemInputError(
                    f"第 {ordinal + 1} 个内容片段的分数无效",
                    code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                ) from exc
            if not math.isfinite(score_number) or score_number < 0:
                raise SystemInputError(
                    f"第 {ordinal + 1} 个内容片段的分数无效",
                    code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                )
            raw_items.append({
                "listening_text": text,
                "original_audio_path": str(audio_path),
                "score": int(score_number) if score_number.is_integer() else score_number,
            })

            if page_input is not None:
                page_input = self._materialize_page_assets(
                    page_input,
                    workflow_id,
                    base_dir,
                    ordinal,
                )
                facts_with_audio.append((page_input, str(audio_path), segment))

        if structured_fact_count:
            if structured_fact_count != len(raw_items):
                raise SystemInputError(
                    "录入单元的页面内容事实不完整，不能混用结构化和旧版平铺内容",
                    code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                )
            page_content_status = page_input_collection_status(
                validated_page_inputs,
                required=True,
            )
            if page_content_status["status"] != "complete":
                raise SystemInputError(
                    page_content_status["reason"] or "页面内容事实不完整，不能开始录入",
                    code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                    details=page_content_status,
                )
            groups = self._build_grouped_page_content(
                facts_with_audio,
                audio_paths_by_stem,
            )
            if not groups:
                raise SystemInputError(
                    "录入单元没有可提交的结构化题型内容",
                    code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                )
            if category == "题型专项":
                # 模仿朗读专项页只需要原文、音频和分值。 Older parser
                # projections may still contain a synthesized reference
                # answer; remove it at the page boundary so a retry cannot
                # recreate an unwanted answer row.
                for group in groups:
                    if group.get("type") != "模仿朗读":
                        continue
                    for question in group.get("questions", ()):
                        if isinstance(question, dict):
                            question.pop("reference_answers", None)
                            question.pop("answers", None)
            return {
                "paper_category": category,
                "paper": paper,
                "template_name": template_name,
                **raw_spec_metadata,
                "question_groups": groups,
            }

        # A legacy single-editor task remains compatible.  A full listening
        # exam, however, must never silently degrade to audio-only cards: its
        # grouped page facts are required to bind stems/options/answers.
        if category == "听说考试":
            raise SystemInputError(
                "听说考试缺少结构化题型内容，拒绝只上传原文音频",
                code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
            )

        return {
            "paper_category": category,
            "paper": paper,
            "template_name": template_name,
            **raw_spec_metadata,
            "items": raw_items,
        }

    def preflight(self, payload: Mapping[str, Any]) -> None:
        """Check the visible platform session before crossing the write fence.

        The actual page flow still performs its own ``wait_until_ready`` call,
        but that check used to happen only after the durable external operation
        had been marked ``IN_FLIGHT``.  A failed login check must not be
        reported as an ambiguous external write, so the service calls this
        read-only browser probe before ``begin_operation``.
        """

        if not isinstance(payload, Mapping):
            raise SystemInputError("页面录入 payload 无效", code="INPUT_EXECUTOR_INVALID_RESULT")

        # Perform all local content/materialization/schema checks before any
        # browser is opened.  In particular, a missing answer or a malformed
        # shared-material group must not become a page-side ambiguous attempt.
        with tempfile.TemporaryDirectory(prefix="wordtts-system-input-preflight-") as temp_dir:
            spec = self._validated_spec(payload, Path(temp_dir))
            paper_name = _text(spec.paper.get("title"), limit=256)
            template_name = _text(spec.template_name, limit=256)
            category = _text(spec.paper_category, limit=64) or "题型专项"
            edit_existing = self._edit_existing_requested(payload)
            entry = payload.get("entry")
            existing_paper_id = (
                _text(entry.get("external_record_id"), limit=512)
                if isinstance(entry, Mapping)
                else ""
            ) or None

            try:
                from playwright.sync_api import sync_playwright
            except (ImportError, SyntaxError) as exc:
                raise SystemInputError(
                    "外部平台页面脚本或 Playwright 不可用",
                    code="INPUT_EXECUTOR_UNAVAILABLE",
                    details={
                        "error_type": type(exc).__name__,
                        "error_message": _safe_error_text(exc),
                    },
                ) from exc

            # ``wait_until_ready`` and opening an existing/new page only use
            # visible controls.  The existing-paper branch deliberately does
            # not select a template again and cannot fall back to creating one.
            page = None
            observer = None
            step = "launch_browser"
            try:
                with self._profile_lock:
                    browser_session = self._browser_session_for_payload(payload)
                    browser_session.open()
                    step = "open_page"
                    page = browser_session.page()
                    observer = page_input.ReadOnlyFeedbackObserver(
                        page,
                        api_base=self.api_base,
                        admin_url=self.admin_url,
                        paper_title=paper_name,
                    )
                    try:
                        automation = page_input.PlatformInputPageAutomation(
                            page,
                            spec,
                            observer,
                            existing_paper_id=existing_paper_id,
                        )
                        control_check = payload.get("_control_check")
                        if callable(control_check):
                            automation._control_check = control_check
                        step = "wait_until_ready"
                        automation.wait_until_ready(self.admin_url, self.login_timeout)
                        if edit_existing:
                            step = "start_existing_paper"
                            existing_phase = automation.start_existing_paper()
                            if existing_phase == "base":
                                # Mirror the editable-page branch of the
                                # real flow so the preflight validates the
                                # required base controls before entering
                                # the content page.
                                step = "fill_base_form"
                                automation.fill_base_form()
                                step = "click_next_to_content"
                                automation.next_to_content()
                            browser_session.mark_preflight_phase("existing_content_ready")
                        else:
                            step = "start_new_paper"
                            automation.start_new_paper()
                            # Template cards are populated only after the
                            # page receives the base-form selections. A
                            # preflight that skips this step always sees
                            # an empty template list and reports a valid
                            # template as unavailable.
                            step = "fill_base_form"
                            automation.fill_base_form()
                            step = "select_template"
                            automation.select_template()
                            # The preflight stops before the write boundary,
                            # so the real executor must continue from this
                            # selected first-step page rather than repeating
                            # the new-paper flow from scratch.
                            browser_session.mark_preflight_phase(
                                "new_paper_template_selected"
                            )
                    finally:
                        if callable(getattr(observer, "close", None)):
                            observer.close()
            except page_input.PlatformInputLoginError as exc:
                raise SystemInputError(
                    "外部平台登录会话无效，本次等待已结束；请重新发起录入，并在打开的 Chrome 窗口完成登录",
                    code="INPUT_PLATFORM_SESSION_INVALID",
                    details=_preflight_error_details(exc, step=step, page=page),
                ) from exc
            except page_input.PlatformInputExistingPaperIncompatibleError as exc:
                raise SystemInputError(
                    "平台已有试卷的分类与当前听说考试目标不兼容，请创建替代试卷后继续",
                    code="INPUT_EXISTING_PAPER_INCOMPATIBLE",
                    details=_preflight_error_details(exc, step=step, page=page),
                ) from exc
            except page_input.PlatformInputError as exc:
                if isinstance(exc, page_input.PlatformInputUiError) and "模板" in str(exc):
                    raise SystemInputError(
                        "当前账号无法使用所选外部平台模板，请重新选择可用模板",
                        code="INPUT_PLATFORM_TEMPLATE_UNAVAILABLE",
                        details=_preflight_error_details(exc, step=step, page=page),
                    ) from exc
                raise SystemInputError(
                    "外部平台页面会话预检失败，请确认登录状态后重试",
                    code="INPUT_PLATFORM_PREFLIGHT_FAILED",
                    details=_preflight_error_details(exc, step=step, page=page),
                ) from exc
            except Exception as exc:
                raise SystemInputError(
                    "外部平台页面会话预检失败",
                    code="INPUT_PLATFORM_PREFLIGHT_FAILED",
                    details=_preflight_error_details(exc, step=step, page=page),
                ) from exc

    def __call__(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise SystemInputError("页面录入 payload 无效", code="INPUT_EXECUTOR_INVALID_RESULT")
        workflow_id = _text(payload.get("workflow_id"), limit=256)
        if not workflow_id:
            raise SystemInputError("页面录入缺少 workflow_id", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
        with tempfile.TemporaryDirectory(prefix="wordtts-system-input-") as temp_dir:
            base_dir = Path(temp_dir)
            try:
                spec = self._validated_spec(payload, base_dir)
                with self._profile_lock:
                    browser_session = self._browser_session_for_payload(payload)
                    resume_phase = browser_session.consume_preflight_phase()
                    result = page_input.execute_live(
                        spec,
                        profile_dir=self.profile_dir,
                        admin_url=self.admin_url,
                        api_base=self.api_base,
                        login_timeout=self.login_timeout,
                        return_to_list=True,
                        edit_existing=self._edit_existing_requested(payload),
                        existing_paper_id=(
                            _text(payload.get("entry", {}).get("external_record_id"), limit=512)
                            if isinstance(payload.get("entry"), Mapping)
                            else None
                        ),
                        browser_session=browser_session,
                        control_check=(
                            payload.get("_control_check")
                            if callable(payload.get("_control_check"))
                            else None
                        ),
                        resume_phase=resume_phase,
                    )
            except SystemInputError:
                raise
            except Exception as exc:
                raise SystemInputError(
                    "页面录入执行失败",
                    code="INPUT_EXECUTOR_FAILED",
                    details={
                        "workflow_id": workflow_id,
                        "error_type": type(exc).__name__,
                        "error_message": _safe_error_text(exc),
                    },
                ) from exc

        if not isinstance(result, Mapping):
            raise SystemInputError("页面执行器没有返回可核验反馈", code="INPUT_EXECUTOR_INVALID_RESULT")
        feedback = _page_result_feedback(result)
        external_id = _text(result.get("paperId") or feedback.get("paperId"), limit=512)
        if not external_id:
            raise SystemInputError("页面保存后没有观察到录入 ID", code="INPUT_RESULT_UNCONFIRMED")
        return {
            "status": _text(result.get("status"), limit=64) or "SUCCEEDED",
            "external_record_id": external_id,
            "paperId": external_id,
            # The current platform does not expose a stable review URL.  The
            # durable entry keeps the submitted paper name for manual search.
            "review_url": None,
            "feedback": feedback,
            "steps": redact_public_json(result.get("steps") or []),
        }

    def verify_paper(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Look up platform records by title through the visible list page.

        This is the automated read-only half of the reconciliation loop: it
        answers "did the interrupted page write actually create the paper?"
        without letting the user — or the script — submit anything. The only
        page actions are the ordinary list search controls.
        """

        if not isinstance(payload, Mapping):
            raise SystemInputError("只读核验 payload 无效", code="INPUT_EXECUTOR_INVALID_RESULT")
        workflow_id = _text(payload.get("workflow_id"), limit=256)
        if not workflow_id:
            raise SystemInputError("只读核验缺少 workflow_id", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
        paper_title = _text(payload.get("paper_title"), limit=256)
        if not paper_title:
            raise SystemInputError(
                "只读核验缺少试卷名称", code="SYSTEM_INPUT_CONFIG_INCOMPLETE"
            )
        try:
            _raise_if_verification_aborted(payload)
            with self._profile_lock:
                _raise_if_verification_aborted(payload)
                result = page_input.verify_live(
                    paper_title=paper_title,
                    profile_dir=self.profile_dir,
                    admin_url=self.admin_url,
                    api_base=self.api_base,
                    login_timeout=self.login_timeout,
                )
        except SystemInputError:
            raise
        except Exception as exc:
            raise SystemInputError(
                "只读核验执行失败",
                code="INPUT_VERIFY_FAILED",
                details={
                    "workflow_id": workflow_id,
                    "error_type": type(exc).__name__,
                    "error_message": _safe_error_text(exc),
                },
            ) from exc
        if not isinstance(result, Mapping):
            raise SystemInputError("只读核验没有返回可读结果", code="INPUT_EXECUTOR_INVALID_RESULT")
        raw_matches = result.get("matches")
        if not isinstance(raw_matches, list) or any(
            not isinstance(row, Mapping) for row in raw_matches
        ):
            raise SystemInputError(
                "只读核验返回的 matches 结构无效",
                code="INPUT_EXECUTOR_INVALID_RESULT",
            )
        matches: list[dict[str, Any]] = []
        for row in raw_matches[:20]:
            record_id = _text(row.get("paperId"), limit=512)
            matches.append({
                "external_record_id": record_id or None,
                "status": _text(row.get("status"), limit=64) or None,
            })
        return {
            "status": _text(result.get("status"), limit=64) or "NOT_FOUND",
            "side_effect_policy": "PAGE_UI_READ_ONLY",
            "paper_title": paper_title,
            "matches": matches,
            "steps": redact_public_json(result.get("steps") or []),
        }


class TextbookInputWorkflowPageExecutor(_ReusableBrowserSessionMixin):
    """通过可见“新增课文”页面提交一个课文录入单元。

    与试卷适配器同一条 durable 边界：payload 只包含页面显示事实和
    已通过音频闸门的片段。译文来自解析器保留的“中文：”行；平台
    表单虽然给译文标注必填星号，但实测允许留空保存（与人工录入
    行为一致），因此译文缺失不拦截，仅在有译文时填写。
    """

    input_type = "textbook"
    adapter_key = "platform_input.textbook"
    accepts_input_run_control = True

    _FORM_FIELDS = (
        ("textbookNameZh", "name_zh"),
        ("textbookNameEn", "name_en"),
        ("textbookForm", "form"),
        ("textbookVersion", "version"),
        ("textbookStage", "stage"),
        ("textbookGrade", "grade"),
        ("textbookVolume", "volume"),
        ("textbookUnit", "unit"),
        ("textbookLesson", "lesson"),
    )

    def __init__(
        self,
        repository: Any,
        artifacts: ArtifactStore,
        *,
        profile_dir: Path,
        admin_url: str,
        api_base: str,
        login_timeout: float,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.profile_dir = profile_dir
        self.admin_url = admin_url
        self.api_base = api_base
        self.login_timeout = max(1.0, float(login_timeout))
        self._profile_lock = _profile_execution_lock(profile_dir)
        self._init_reusable_browser_sessions()

    def _workflow_id(self, payload: Mapping[str, Any]) -> str:
        target = payload.get("target")
        workflow_id = (
            _text(target.get("workflow_id"), limit=256)
            if isinstance(target, Mapping)
            else ""
        )
        return workflow_id or _text(payload.get("workflow_id"), limit=256)

    def _build_records(self, payload: Mapping[str, Any], base_dir: Path) -> list[dict[str, Any]]:
        unit = payload.get("unit")
        if not isinstance(unit, Mapping):
            raise SystemInputError("系统录入目标单元不存在", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
        configuration = _configuration_for_unit(unit)
        input_type = _text(configuration.get("input_type") or unit.get("input_type"), limit=32)
        if input_type != "textbook":
            raise SystemInputError(
                "当前页面执行器只支持课文录入",
                code="SYSTEM_INPUT_TYPE_UNSUPPORTED",
                details={"input_type": input_type},
            )
        workflow_id = self._workflow_id(payload)

        record: dict[str, Any] = {}
        missing_fields: list[str] = []
        for config_key, record_key in self._FORM_FIELDS:
            value = _text(configuration.get(config_key), limit=256)
            if not value:
                missing_fields.append(config_key)
            record[record_key] = value
        if missing_fields:
            raise SystemInputError(
                "课文分类信息缺少必填字段：" + "、".join(missing_fields),
                code="SYSTEM_INPUT_CONFIG_INCOMPLETE",
                details={"fields": missing_fields},
            )

        segments = unit.get("segments")
        if isinstance(segments, (str, bytes)) or not isinstance(segments, Sequence) or not segments:
            raise SystemInputError(
                "当前录入单元没有可提交的内容片段",
                code="SYSTEM_INPUT_CONFIG_INCOMPLETE",
            )
        items: list[dict[str, Any]] = []
        roles: list[str] = []
        categories: set[str] = set()
        paragraph_groups: dict[str, dict[str, Any]] = {}
        paragraph_order: list[str] = []
        for ordinal, segment in enumerate(segments):
            if not isinstance(segment, Mapping):
                raise SystemInputError("内容片段格式无效", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
            role = _text(segment.get("role"), limit=256)
            category = _text(segment.get("category") or segment.get("item_type"), limit=128)
            if role and role not in roles:
                roles.append(role)
            if category:
                categories.add(category)
            original = _textbook_page_text(
                segment.get("raw_text") or segment.get("tts_text"),
                role,
            )
            translation = _text(segment.get("translation"), limit=1_000_000)
            artifact_id = _text(segment.get("audio_artifact_id"), limit=256)
            if not original:
                raise SystemInputError(
                    f"第 {ordinal + 1} 条课文内容缺少原文",
                    code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                    details={"item_id": _text(segment.get("item_id"), limit=256)},
                )
            if not artifact_id:
                raise SystemInputError(
                    f"第 {ordinal + 1} 条课文内容缺少音频产物",
                    code="AUDIO_GATE_FAILED",
                    details={"item_id": _text(segment.get("item_id"), limit=256)},
                )
            audio_path = base_dir / f"textbook-segment-{ordinal + 1}.mp3"
            _materialize_audio_file(
                self.repository,
                self.artifacts,
                artifact_id,
                workflow_id,
                audio_path,
            )
            items.append({
                "original": original,
                "translation": translation,
                "audio_path": str(audio_path),
                "role": role,
                "paragraph_id": _text(segment.get("paragraph_id"), limit=256),
                "paragraph_scope": _text(segment.get("paragraph_scope"), limit=256),
                "paragraph_title": _text(segment.get("paragraph_title"), limit=256),
            })
            paragraph_id = _text(segment.get("paragraph_id"), limit=256) or "paragraph-1"
            if paragraph_id not in paragraph_groups:
                paragraph_groups[paragraph_id] = {
                    "title": _text(segment.get("paragraph_title"), limit=256),
                    "items": [],
                }
                paragraph_order.append(paragraph_id)
            elif not paragraph_groups[paragraph_id]["title"]:
                paragraph_groups[paragraph_id]["title"] = _text(
                    segment.get("paragraph_title"),
                    limit=256,
                )
            paragraph_groups[paragraph_id]["items"].append(items[-1])
        if roles or categories.intersection({"句子跟读", "对话跟读"}):
            # The parser is the source of truth for sentence/dialogue rows;
            # force the page form even when an older saved unit still carries
            # the former “同步课文” default.
            record["form"] = "角色扮演"
        record["roles"] = roles
        record["items"] = items
        if record["form"] == "段落":
            record["paragraphs"] = [paragraph_groups[key] for key in paragraph_order]
        return [record]

    def preflight(self, payload: Mapping[str, Any]) -> None:
        """本地内容校验 + 只读的登录/列表页探活，不产生页面副作用。"""

        if not isinstance(payload, Mapping):
            raise SystemInputError("页面录入 payload 无效", code="INPUT_EXECUTOR_INVALID_RESULT")
        with tempfile.TemporaryDirectory(prefix="wordtts-textbook-preflight-") as temp_dir:
            self._build_records(payload, Path(temp_dir))
        try:
            from playwright.sync_api import sync_playwright
        except (ImportError, SyntaxError) as exc:
            raise SystemInputError(
                "外部平台页面脚本或 Playwright 不可用",
                code="INPUT_EXECUTOR_UNAVAILABLE",
                details={
                    "error_type": type(exc).__name__,
                    "error_message": _safe_error_text(exc),
                },
            ) from exc
        from platform_entry.adapter.runtime import _launch_browser
        from platform_entry.adapter.textbook_page import _open_text_list

        with self._profile_lock:
            browser_session = self._browser_session_for_payload(payload)
            browser_session.open()
            page = browser_session.page()
            _open_text_list(page, max(1, int(self.login_timeout)))

    def __call__(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise SystemInputError("页面录入 payload 无效", code="INPUT_EXECUTOR_INVALID_RESULT")
        workflow_id = self._workflow_id(payload)
        if not workflow_id:
            raise SystemInputError("页面录入缺少 workflow_id", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
        with tempfile.TemporaryDirectory(prefix="wordtts-textbook-input-") as temp_dir:
            records = self._build_records(payload, Path(temp_dir))
            try:
                with self._profile_lock:
                    browser_session = self._browser_session_for_payload(payload)
                    result = textbook_page.execute_records(
                        records,
                        profile_dir=self.profile_dir,
                        api_base=self.api_base,
                        login_timeout=self.login_timeout,
                        browser_session=browser_session,
                        control_check=(
                            payload.get("_control_check")
                            if callable(payload.get("_control_check"))
                            else None
                        ),
                    )
            except SystemInputError:
                raise
            except TimeoutError as exc:
                # 打开课文管理页面超时通常意味着登录会话失效；映射为
                # 会话错误码，让界面给出“重新登录”的指引而不是笼统失败。
                raise SystemInputError(
                    "外部平台登录会话无效或已过期；请重新发起录入，并在打开的 Chrome 窗口完成登录",
                    code="INPUT_PLATFORM_SESSION_INVALID",
                    details={"error_message": _safe_error_text(exc)},
                ) from exc
            except Exception as exc:
                raise SystemInputError(
                    "课文页面录入执行失败",
                    code="INPUT_EXECUTOR_FAILED",
                    details={
                        "workflow_id": workflow_id,
                        "error_type": type(exc).__name__,
                        "error_message": _safe_error_text(exc),
                    },
                ) from exc

        if not isinstance(result, Mapping):
            raise SystemInputError("页面执行器没有返回可核验反馈", code="INPUT_EXECUTOR_INVALID_RESULT")
        executed = result.get("records") if isinstance(result.get("records"), list) else []
        external_id = ""
        for row in executed:
            if isinstance(row, Mapping):
                external_id = _text(row.get("external_record_id"), limit=512)
                if external_id:
                    break
        if not external_id:
            raise SystemInputError("页面保存后没有观察到录入 ID", code="INPUT_RESULT_UNCONFIRMED")
        feedback = {
            "record_count": len(executed),
            "records": [
                {
                    "name_zh": _text(row.get("name_zh"), limit=256) if isinstance(row, Mapping) else "",
                    "name_en": _text(row.get("name_en"), limit=256) if isinstance(row, Mapping) else "",
                    "external_record_id": _text(row.get("external_record_id"), limit=512) or None,
                }
                for row in executed
                if isinstance(row, Mapping)
            ],
        }
        return {
            "status": _text(result.get("status"), limit=64) or "SUCCEEDED",
            "external_record_id": external_id,
            "review_url": None,
            "feedback": feedback,
            "steps": [],
        }

    def verify_record(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """只读核验：按课文名称搜索课文管理列表。"""

        if not isinstance(payload, Mapping):
            raise SystemInputError("只读核验 payload 无效", code="INPUT_EXECUTOR_INVALID_RESULT")
        workflow_id = self._workflow_id(payload)
        if not workflow_id:
            raise SystemInputError("只读核验缺少 workflow_id", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
        title = _text(payload.get("paper_title"), limit=256)
        if not title:
            raise SystemInputError(
                "只读核验缺少课文名称", code="SYSTEM_INPUT_CONFIG_INCOMPLETE"
            )
        try:
            _raise_if_verification_aborted(payload)
            with self._profile_lock:
                _raise_if_verification_aborted(payload)
                result = textbook_page.verify_live(
                    title,
                    profile_dir=self.profile_dir,
                    login_timeout=self.login_timeout,
                )
        except SystemInputError:
            raise
        except Exception as exc:
            raise SystemInputError(
                "只读核验执行失败",
                code="INPUT_VERIFY_FAILED",
                details={
                    "workflow_id": workflow_id,
                    "error_type": type(exc).__name__,
                    "error_message": _safe_error_text(exc),
                },
            ) from exc
        if not isinstance(result, Mapping):
            raise SystemInputError("只读核验没有返回可读结果", code="INPUT_EXECUTOR_INVALID_RESULT")
        raw_matches = result.get("matches")
        if not isinstance(raw_matches, list) or any(
            not isinstance(row, Mapping) for row in raw_matches
        ):
            raise SystemInputError(
                "只读核验返回的 matches 结构无效",
                code="INPUT_EXECUTOR_INVALID_RESULT",
            )
        matches = [
            {
                "external_record_id": _text(row.get("external_record_id"), limit=512) or None,
                "status": _text(row.get("status"), limit=64) or None,
            }
            for row in raw_matches[:20]
        ]
        return {
            "status": _text(result.get("status"), limit=64) or "NOT_FOUND",
            "side_effect_policy": "PAGE_UI_READ_ONLY",
            "paper_title": title,
            "matches": matches,
            "steps": [],
        }


def build_system_input_executor(repository: Any, artifacts: ArtifactStore) -> SystemInputExecutorRouter | None:
    """Build the real page executor only after an explicit opt-in."""

    if os.environ.get("WORDTTS_SYSTEM_INPUT_ENABLED", "").strip() != "1":
        return None
    default_profile_dir = page_input._default_profile_dir
    admin_url = page_input.ADMIN_URL
    api_base = page_input.API_BASE_URL
    if not admin_url or not api_base:
        return None
    configured_profile = os.environ.get("WORDTTS_PLATFORM_INPUT_PROFILE_DIR", "").strip()
    if configured_profile:
        profile_dir = Path(os.path.abspath(os.path.expanduser(configured_profile)))
    else:
        profile_dir = default_profile_dir()
    timeout_text = os.environ.get("WORDTTS_PLATFORM_INPUT_LOGIN_TIMEOUT", "300").strip()
    try:
        timeout = float(timeout_text)
    except (TypeError, ValueError):
        timeout = 300.0
    paper_executor = PlatformInputWorkflowPageExecutor(
        repository,
        artifacts,
        profile_dir=profile_dir,
        admin_url=admin_url,
        api_base=api_base,
        login_timeout=timeout,
    )
    textbook_executor = TextbookInputWorkflowPageExecutor(
        repository,
        artifacts,
        profile_dir=profile_dir,
        admin_url=admin_url,
        api_base=api_base,
        login_timeout=timeout,
    )
    return SystemInputExecutorRouter({
        paper_executor.adapter_key: paper_executor,
        textbook_executor.adapter_key: textbook_executor,
    })


__all__ = [
    "PlatformInputWorkflowPageExecutor",
    "SystemInputExecutorRouter",
    "TextbookInputWorkflowPageExecutor",
    "build_system_input_executor",
]

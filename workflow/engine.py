"""Application-level vertical workflow runner backed by the durable ports.

The engine deliberately knows nothing about Playwright or Electron.  A
provider only needs ``submit``, ``query`` and ``download``; all billable
boundaries, retries, receipts and artifacts are persisted by the repository
before this module returns.
"""

from __future__ import annotations

import io
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Mapping

from .artifact_store import ArtifactStore, ArtifactStoreError
from .domain import DomainError, content_hash
from .fake_provider import AmbiguousProviderError, FakeProviderError
from .providers import ProviderError, TTSProviderPort
from .repositories import RepositoryError, WorkflowRepository

TTSProvider = TTSProviderPort


PROVIDER_LEASE_TTL_SECONDS = 300
PROVIDER_LEASE_HEARTBEAT_INTERVAL_SECONDS = 30.0


class _LeaseHeartbeat:
    """Renew a provider lease while a blocking adapter call is in progress.

    The adapter runs outside SQLite and may spend minutes in a browser or
    network request.  A failed heartbeat is intentionally not raised from
    this helper: the call's post-fence renewal is authoritative and will
    convert the result into a durable stale-attempt transition.  This keeps a
    transient heartbeat/database error from interrupting a provider call at a
    point where its external outcome is already uncertain.
    """

    def __init__(
        self,
        repository: WorkflowRepository,
        lease_id: str,
        owner_id: str,
        fencing_token: int,
        *,
        ttl_seconds: int,
        interval_seconds: float,
    ) -> None:
        self._repository = repository
        self._lease_id = lease_id
        self._owner_id = owner_id
        self._fencing_token = fencing_token
        self._ttl_seconds = ttl_seconds
        self._interval_seconds = max(0.01, float(interval_seconds))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="provider-lease-heartbeat", daemon=True)

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._interval_seconds + 1.0))

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._repository.renew_lease(
                    self._lease_id,
                    self._owner_id,
                    self._fencing_token,
                    ttl_seconds=self._ttl_seconds,
                )
            except Exception:
                # The main thread performs the final fencing renewal.  A
                # heartbeat exception must never be mistaken for evidence
                # that the provider call itself did not happen.
                continue


@dataclass(frozen=True)
class TTSRunResult:
    workflow_id: str
    step_id: str
    attempt_id: str
    work_unit_id: str
    submission_id: str
    receipt_id: str | None
    artifact_ids: tuple[str, ...]
    status: str
    error_code: str | None = None
    reused: bool = False
    error_message: str | None = None
    error_details: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkflowEngine:
    """Run the deterministic local TTS path used by 2A fault tests."""

    def __init__(self, repository: WorkflowRepository, artifact_store: ArtifactStore) -> None:
        self.repository = repository
        self.artifact_store = artifact_store

    def run_tts(
        self,
        workflow_id: str,
        provider: TTSProvider,
        *,
        generation_mode: str = "composite_cut",
        owner_id: str = "workflow-engine",
        cancel_check: Callable[[], bool] | None = None,
        pause_check: Callable[[], bool] | None = None,
        item_ids: Iterable[str] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> TTSRunResult:
        if generation_mode not in {"composite_cut", "single_segment"}:
            raise DomainError("VALIDATION_ERROR", f"unsupported generation mode: {generation_mode}")
        if not getattr(provider, "provider", None) or not getattr(provider, "account_scope", None):
            raise DomainError("VALIDATION_ERROR", "provider and account_scope are required")

        lease_id, fencing_token, _ = self.repository.acquire_lease(
            workflow_id,
            "provider",
            f"{provider.provider}:{provider.account_scope}",
            owner_id,
            ttl_seconds=PROVIDER_LEASE_TTL_SECONDS,
        )
        try:
            return self._run_tts(
                workflow_id,
                provider,
                generation_mode=generation_mode,
                lease_id=lease_id,
                fencing_token=fencing_token,
                owner_id=owner_id,
                cancel_check=cancel_check,
                pause_check=pause_check,
                item_ids=item_ids,
                progress_callback=progress_callback,
            )
        finally:
            try:
                self.repository.release_lease(lease_id, owner_id, fencing_token)
            except RepositoryError:
                # The lease remains recoverable by the expiry scanner if the
                # process failed while closing the provider session.
                pass

    def _run_tts(
        self,
        workflow_id: str,
        provider: TTSProvider,
        *,
        generation_mode: str,
        lease_id: str,
        fencing_token: int,
        owner_id: str,
        cancel_check: Callable[[], bool] | None = None,
        pause_check: Callable[[], bool] | None = None,
        item_ids: Iterable[str] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> TTSRunResult:

        self._wait_if_paused(workflow_id, cancel_check, pause_check)
        if self._cancel_requested(cancel_check):
            raise RepositoryError("generation cancelled before provider submission", code="WORKFLOW_CANCELLED")
        snapshot = self.repository.get_workflow(workflow_id)
        configuration = self.repository.get_configuration(workflow_id)
        all_items = self.repository.list_items(workflow_id)
        if not all_items:
            raise DomainError("DEPENDENCY_NOT_READY", "TTS requires at least one parsed work item")
        budget_items = [
            item for item in all_items
            if str(item.get("status") or "") != "SKIPPED"
        ]
        # An explicit item scope is used only by a targeted retry.  Keep the
        # document order from the durable WorkItem list even when the caller
        # sends IDs in a different order; this makes the provider payload and
        # submission key deterministic and prevents a retry from silently
        # including an item outside the requested scope.
        requested_item_ids = None
        if item_ids is not None:
            requested_item_ids = [str(item_id).strip() for item_id in item_ids if str(item_id).strip()]
            if not requested_item_ids or len(set(requested_item_ids)) != len(requested_item_ids):
                raise DomainError("VALIDATION_ERROR", "item_ids must contain at least one unique item id")
            available_ids = {str(item["item_id"]) for item in all_items}
            missing = [item_id for item_id in requested_item_ids if item_id not in available_ids]
            if missing:
                raise DomainError("NOT_FOUND", f"workflow item does not exist: {missing[0]}")
            requested_set = set(requested_item_ids)
            all_items = [item for item in all_items if str(item["item_id"]) in requested_set]
            if not all_items:
                raise DomainError("VALIDATION_ERROR", "item_ids does not select any workflow item")
        # SKIPPED is a durable user decision, not a transient provider
        # failure.  It never enters the plan, is never submitted or billed,
        # and remains part of the total/progress projection.
        if item_ids is None and all(str(item.get("status") or "") == "SKIPPED" for item in all_items):
            self.repository.complete_skipped_workflow(workflow_id)
            return TTSRunResult(
                workflow_id, "", "", "", "", None, tuple(), "SUCCEEDED", None, False,
            )
        all_items = [item for item in all_items if str(item.get("status") or "") != "SKIPPED"]
        if item_ids is None:
            # Recovery dispatch has no renderer-provided item scope.  Do not
            # submit already-delivered items again if a targeted retry or a
            # crash happened before the worker could finish.  Keep the fully
            # delivered case intact so the normal submission-key reuse path
            # can return the existing successful plan.
            delivered_ids = {
                str(row["item_id"])
                for row in self.repository.list_verified_tts_segments(workflow_id)
                if row.get("item_id") is not None
            }
            if delivered_ids and len(delivered_ids) >= len(all_items):
                completed_plan = self.repository.get_latest_successful_tts_plan(workflow_id)
                if completed_plan is not None:
                    return TTSRunResult(
                        workflow_id,
                        str(completed_plan["step_id"]),
                        str(completed_plan["attempt_id"]),
                        str(completed_plan["work_unit_id"]),
                        str(completed_plan["submission_id"]),
                        None,
                        tuple(self.repository.list_work_unit_artifacts(completed_plan["work_unit_id"])),
                        "SUCCEEDED",
                        None,
                        True,
                    )
            if delivered_ids and len(delivered_ids) < len(all_items):
                all_items = [item for item in all_items if str(item["item_id"]) not in delivered_ids]
        if not all_items:
            raise DomainError("NO_ELIGIBLE_ITEMS", "所选条目均已跳过，没有需要生成的内容")
        # Preview is a bounded, explicit generation scope.  The renderer uses
        # the same three-item rule for its progress copy, but the accepted
        # workflow configuration is the authority for the worker.  Applying
        # the limit here prevents a preview checkbox from accidentally
        # triggering a billable full-document submission.
        try:
            preview_limit = int(configuration.get("preview_limit") or 3)
        except (TypeError, ValueError):
            preview_limit = 3
        preview_limit = max(1, min(1000, preview_limit))
        preview = item_ids is None and bool(configuration.get("preview")) and len(all_items) > preview_limit
        items = all_items[:preview_limit] if preview else all_items
        ordered_plan = [
            self._effective_plan_item({
                "ordinal": index,
                "item_id": str(item["item_id"]),
                "identity_key": str(item["item_identity_key"]),
                "content": str(item["normalized_content"]),
                "content_hash": str(item["content_hash"]),
                "role": item["role"],
                "voice_key": item["voice_key"],
            }, configuration)
            for index, item in enumerate(items)
        ]
        input_hash = content_hash({
            "mode": generation_mode,
            "items": ordered_plan,
            "scope": {
                "kind": "items" if requested_item_ids is not None else "workflow",
                "item_ids": [str(item["item_id"]) for item in items],
            },
        })
        capability_snapshot = self._capability_snapshot(provider)
        formats = capability_snapshot.get("formats") if isinstance(capability_snapshot, Mapping) else None
        output_format = (
            str(formats[0]).lower().lstrip(".")
            if isinstance(formats, (list, tuple)) and formats
            else "bin"
        )
        profile = {
            "generation_mode": generation_mode,
            "format": output_format,
            "provider": provider.provider,
            "quality": configuration.get("quality", "128 kbps（标准）"),
            "preview": preview,
            "item_scope": [str(item["item_id"]) for item in items] if requested_item_ids is not None else None,
            "rate": configuration.get("rate", 50),
            "volume": configuration.get("volume", 50),
            "pitch": configuration.get("pitch", 50),
            "default_female_voice": configuration.get("default_female_voice"),
            "default_male_voice": configuration.get("default_male_voice"),
            "role_configs": configuration.get("role_configs"),
            "role_voices": configuration.get("role_voices"),
            "default_role": configuration.get("default_role"),
            # Provider capability changes must produce a new idempotency key.
            # Keep only the adapter's redacted, deterministic snapshot here;
            # credentials and browser/session objects never enter the plan.
            "capabilities": capability_snapshot,
        }
        profile_hash = content_hash(profile)
        submission_key = f"{snapshot.workflow_group_id}:{generation_mode}:{input_hash}:{profile_hash[:16]}"
        existing_plan = self.repository.get_tts_plan(workflow_id, submission_key)
        if existing_plan and existing_plan["status"] == "SUCCEEDED":
            return TTSRunResult(
                workflow_id, str(existing_plan["step_id"]), str(existing_plan["attempt_id"]), str(existing_plan["work_unit_id"]),
                str(existing_plan["submission_id"]), None, tuple(self.repository.list_work_unit_artifacts(existing_plan["work_unit_id"])),
                "SUCCEEDED", None, True,
            )
        self._wait_if_paused(workflow_id, cancel_check, pause_check)
        if self._cancel_requested(cancel_check):
            raise RepositoryError("generation cancelled before provider submission", code="WORKFLOW_CANCELLED")
        if snapshot.control_state != "RUNNING":
            raise RepositoryError(f"workflow control state is {snapshot.control_state}", code="STATE_CONFLICT")
        plan = self.repository.prepare_tts_plan(
            workflow_id,
            provider=provider.provider,
            provider_account_scope=provider.account_scope,
            unit_type="composite" if generation_mode == "composite_cut" else "single",
            tts_submission_key=submission_key,
            ordered_plan=ordered_plan,
            input_hash=input_hash,
            submission_profile_hash=profile_hash,
            capability_snapshot={
                "generation_mode": generation_mode,
                "account_scope": provider.account_scope,
                **capability_snapshot,
            },
            step_key="tts",
            step_type="TTS",
            lease_fencing_token=fencing_token,
            lease_id=lease_id,
            lease_owner_id=owner_id,
        )

        if plan["status"] == "SUCCEEDED":
            return TTSRunResult(
                workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                str(plan["submission_id"]), None, tuple(self.repository.list_work_unit_artifacts(plan["work_unit_id"])),
                "SUCCEEDED", None, True,
            )

        self._wait_if_paused(workflow_id, cancel_check, pause_check)
        if self._cancel_requested(cancel_check):
            raise RepositoryError("generation cancelled before provider submission", code="WORKFLOW_CANCELLED")

        # Retry budget identity is deliberately independent from the current
        # payload hash and ordered submission key.  A changed batch/config can
        # therefore get a new provider submission intent without receiving a
        # fresh set of billable attempts for the same group/step/items.
        budget_key = "tts:" + content_hash({
            "provider": provider.provider,
            "provider_account_scope": provider.account_scope,
            "workflow_group_id": snapshot.workflow_group_id,
            "step_key": "tts",
            "canonical_item_identity_set": sorted(
                str(item["item_identity_key"]) for item in budget_items
            ),
            "operation_type": (
                f"{generation_mode}:preview" if preview else f"{generation_mode}:full"
            ),
        })
        budget_id: str | None = None
        budget_reused = False
        if plan["submission_state"] not in {"AMBIGUOUS", "IN_FLIGHT", "SUBMITTED", "CONFIRMED"}:
            existing_budget = self.repository.get_budget(plan["workflow_group_id"], budget_key)
            if existing_budget and int(existing_budget["reserved_attempts"]) > 0:
                budget_id = str(existing_budget["retry_budget_id"])
                budget_reused = True
            else:
                budget_id = self.repository.reserve_budget(
                    plan["workflow_group_id"], budget_key, budget_kind="tts", max_attempts=3,
                )
        else:
            existing_budget = self.repository.get_budget(plan["workflow_group_id"], budget_key)
            if existing_budget and int(existing_budget["reserved_attempts"]) > 0:
                budget_id = str(existing_budget["retry_budget_id"])
                budget_reused = True

        receipt = None
        receipt_id: str | None = None
        submission_state = str(plan["submission_state"])
        if submission_state in {"AMBIGUOUS", "IN_FLIGHT", "SUBMITTED", "CONFIRMED"}:
            self._wait_if_paused(workflow_id, cancel_check, pause_check)
            if self._cancel_requested(cancel_check):
                raise RepositoryError("generation cancelled while provider result was unresolved", code="WORKFLOW_CANCELLED")
            if callable(progress_callback):
                try:
                    progress_callback({
                        "stage": "reconciling",
                        "status": "reconciling",
                        "item_id": str(ordered_plan[0].get("item_id") or "") if ordered_plan else "",
                    })
                except Exception:
                    pass
            query_with_context = getattr(provider, "query_with_context", None)

            def query_provider() -> Any:
                if callable(query_with_context):
                    query_context = {"plan": ordered_plan, "profile": profile}
                    query_kwargs = {}
                    if callable(cancel_check):
                        query_kwargs["cancel_check"] = cancel_check
                    if callable(progress_callback):
                        query_kwargs["progress_callback"] = progress_callback
                    try:
                        return query_with_context(
                            submission_key,
                            query_context,
                            **query_kwargs,
                        )
                    except TypeError as exc:
                        # Keep injected providers written against the original
                        # two-argument port usable while the real browser
                        # adapter receives the cancellation/progress hooks.
                        message = str(exc)
                        if not query_kwargs or not any(
                            token in message for token in ("cancel_check", "progress_callback")
                        ):
                            raise
                        return query_with_context(submission_key, query_context)
                return provider.query(submission_key)

            try:
                # A provider lookup is an external call too.  Renewing here
                # fences an expired/replaced worker before it can consume a
                # stale browser session or publish the observed receipt.
                receipt = self._provider_call_with_lease(
                    lease_id,
                    owner_id,
                    fencing_token,
                    query_provider,
                )
            except ProviderError as exc:
                # A failed provider lookup is not proof that the prior
                # submission disappeared.  Persist the conservative
                # AMBIGUOUS state so a scheduler/restart cannot keep treating
                # the work as a safe retryable output failure.
                if exc.code == "STALE_ATTEMPT":
                    self._record_stale_lease(plan, receipt_id=None, error=exc)
                else:
                    try:
                        self.repository.mark_tts_failure(
                            plan,
                            error_code=exc.code,
                            error_message=str(exc)[:2000],
                            error_details=getattr(exc, "details", None),
                            ambiguous=True,
                        )
                    except Exception:
                        # If the lease or database is already unavailable, the
                        # existing IN_FLIGHT/SUBMITTED fact remains for recovery.
                        # Do not replace the provider lookup's conservative
                        # outcome with a secondary persistence exception.
                        pass
                return TTSRunResult(
                    workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                    str(plan["submission_id"]), None, tuple(), "AMBIGUOUS", exc.code, bool(plan["reused"]),
                    str(exc), dict(getattr(exc, "details", {}) or {}),
                )
            except Exception as exc:
                if getattr(exc, "code", "") == "STALE_ATTEMPT":
                    self._record_stale_lease(plan, receipt_id=None, error=exc)
                else:
                    try:
                        self.repository.mark_tts_failure(
                            plan,
                            error_code=getattr(exc, "code", "SUBMISSION_AMBIGUOUS"),
                            error_message=str(exc)[:2000],
                            error_details=getattr(exc, "details", None),
                            ambiguous=True,
                        )
                    except Exception:
                        pass
                return TTSRunResult(
                    workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                    str(plan["submission_id"]), None, tuple(), "AMBIGUOUS",
                    getattr(exc, "code", "SUBMISSION_AMBIGUOUS"), bool(plan["reused"]),
                    str(exc)[:2000], dict(getattr(exc, "details", {}) or {}),
                )
            if receipt is None:
                if submission_state != "AMBIGUOUS":
                    self.repository.mark_tts_failure(
                        plan,
                        error_code="SUBMISSION_AMBIGUOUS",
                        error_message="未找到已提交作品；系统未重复提交，请核验讯飞作品列表后再决定",
                        ambiguous=True,
                    )
                return TTSRunResult(
                    workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                    str(plan["submission_id"]), None, tuple(), "AMBIGUOUS", "SUBMISSION_AMBIGUOUS", bool(plan["reused"]),
                    "未找到已提交作品；系统未重复提交，请核验讯飞作品列表后再决定",
                )
        else:
            try:
                self._wait_if_paused(workflow_id, cancel_check, pause_check)
                self.repository.begin_tts_submission(plan)
                self._wait_if_paused(workflow_id, cancel_check, pause_check)
                if self._cancel_requested(cancel_check):
                    message = "用户取消时讯飞提交结果尚未确认；系统未重复提交，请核验讯飞作品列表后再决定"
                    self.repository.mark_tts_failure(
                        plan,
                        error_code="SUBMISSION_AMBIGUOUS",
                        error_message=message,
                        ambiguous=True,
                    )
                    return TTSRunResult(
                        workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                        str(plan["submission_id"]), None, tuple(), "AMBIGUOUS", "SUBMISSION_AMBIGUOUS", bool(plan["reused"]),
                        message,
                    )
                provider_payload: dict[str, Any] = {"plan": ordered_plan, "profile": profile}
                if cancel_check is not None:
                    # This private, in-memory callback is consumed only by
                    # adapters that can interrupt their browser workflow. It
                    # is never persisted in the submission plan or sent to a
                    # generic backend adapter.
                    provider_payload["_cancel_check"] = cancel_check
                if progress_callback is not None:
                    # This callback is process-local observability only.  It
                    # never enters the durable provider submission payload or
                    # the idempotency hash.
                    provider_payload["_progress_callback"] = progress_callback
                # This is the last local fence before the billable provider
                # call.  If the lease was lost, leave the durable IN_FLIGHT
                # boundary for recovery instead of invoking a stale worker.
                receipt = self._provider_call_with_lease(
                    lease_id,
                    owner_id,
                    fencing_token,
                    lambda: provider.submit(submission_key, provider_payload),
                )
            except RepositoryError as exc:
                if getattr(exc, "code", "") == "STALE_ATTEMPT":
                    self._record_stale_lease(plan, receipt_id=None, error=exc)
                    return TTSRunResult(
                        workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                        str(plan["submission_id"]), None, tuple(), "AMBIGUOUS", exc.code, bool(plan["reused"]),
                        str(exc), dict(getattr(exc, "details", {}) or {}),
                    )
                raise
            except AmbiguousProviderError as exc:
                message = str(exc)[:2000]
                self.repository.mark_tts_failure(
                    plan,
                    error_code="SUBMISSION_AMBIGUOUS",
                    error_message=message,
                    error_details=getattr(exc, "details", None),
                    ambiguous=True,
                )
                return TTSRunResult(
                    workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                    str(plan["submission_id"]), None, tuple(), "AMBIGUOUS", "SUBMISSION_AMBIGUOUS", bool(plan["reused"]),
                    message, dict(getattr(exc, "details", {}) or {}),
                )
            except FakeProviderError as exc:
                code = getattr(exc, "code", "TRANSIENT_PROVIDER_ERROR")
                message = str(exc)[:2000]
                self.repository.mark_tts_failure(
                    plan,
                    error_code=code,
                    error_message=message,
                )
                if budget_id and not budget_reused:
                    self.repository.release_budget(budget_id)
                return TTSRunResult(
                    workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                    str(plan["submission_id"]), None, tuple(), "WAITING_RETRY", code, bool(plan["reused"]),
                    message,
                )
            except ProviderError as exc:
                code = getattr(exc, "code", "TRANSIENT_PROVIDER_ERROR")
                message = str(exc)[:2000]
                ambiguous = getattr(exc, "ambiguous", None)
                ambiguous = True if ambiguous is None else bool(ambiguous)
                self.repository.mark_tts_failure(
                    plan,
                    error_code=code,
                    error_message=message,
                    error_details=getattr(exc, "details", None),
                    ambiguous=ambiguous,
                )
                if budget_id and not budget_reused and not ambiguous:
                    self.repository.release_budget(budget_id)
                return TTSRunResult(
                    workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                    str(plan["submission_id"]), None, tuple(),
                    "AMBIGUOUS" if ambiguous else "WAITING_RETRY",
                    code, bool(plan["reused"]), message,
                    dict(getattr(exc, "details", {}) or {}),
                )
            except Exception as exc:
                # Reaching this branch after begin_tts_submission is itself an
                # uncertain side-effect boundary; leave the row reconcileable.
                code = getattr(exc, "code", "SUBMISSION_AMBIGUOUS")
                message = str(exc)[:2000] or "讯飞提交后未能确认结果"
                self.repository.mark_tts_failure(
                    plan,
                    error_code=code,
                    error_message=message,
                    error_details=getattr(exc, "details", None),
                    ambiguous=True,
                )
                return TTSRunResult(
                    workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                    str(plan["submission_id"]), None, tuple(), "AMBIGUOUS", code, bool(plan["reused"]),
                    message, dict(getattr(exc, "details", {}) or {}),
                )

        try:
            self._wait_if_paused(workflow_id, cancel_check, pause_check)
            receipt_id = self.repository.record_tts_receipt(plan, self._receipt_mapping(receipt))
            self._wait_if_paused(workflow_id, cancel_check, pause_check)
            output = self._provider_call_with_lease(
                lease_id,
                owner_id,
                fencing_token,
                lambda: provider.download(receipt),
            )
            if not isinstance(output, (bytes, bytearray)) or not output:
                raise ArtifactStoreError("provider returned an empty or non-byte output")
            specs = self._stage_output(
                plan,
                bytes(output),
                ordered_plan,
                receipt=receipt,
                generation_mode=generation_mode,
            )
            self._wait_if_paused(workflow_id, cancel_check, pause_check)
            if self._cancel_requested(cancel_check):
                raise RepositoryError("generation cancelled before artifact publication", code="WORKFLOW_CANCELLED")
            self.repository.renew_lease(
                lease_id,
                owner_id,
                fencing_token,
                ttl_seconds=PROVIDER_LEASE_TTL_SECONDS,
            )
            artifact_ids = self.repository.complete_tts(
                plan,
                receipt_id=receipt_id,
                artifacts=specs,
                keep_workflow_open=bool(requested_item_ids is not None),
            )
        except RepositoryError as exc:
            # A control-plane fence (pause/cancel) is not a provider failure.
            # In particular, do not rewrite a confirmed submission as
            # AMBIGUOUS merely because cancellation won the final publication
            # race; the cleanup/reconciliation projection owns that state.
            if getattr(exc, "code", "") in {"WORKFLOW_CANCELLED", "CONTROL_STATE_CONFLICT"}:
                raise
            if getattr(exc, "code", "") == "STALE_ATTEMPT":
                self._record_stale_lease(plan, receipt_id=receipt_id, error=exc)
                return TTSRunResult(
                    workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                    str(plan["submission_id"]), receipt_id, tuple(),
                    "WAITING_RETRY" if receipt_id is not None else "AMBIGUOUS",
                    exc.code, bool(plan["reused"]),
                    str(exc), dict(getattr(exc, "details", {}) or {}),
                )
            self.repository.mark_tts_failure(
                plan,
                error_code=getattr(exc, "code", "PERSISTENCE_AMBIGUOUS"),
                error_message=str(exc)[:2000],
                error_details=getattr(exc, "details", None),
                ambiguous=True,
            )
            return TTSRunResult(
                workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                str(plan["submission_id"]), receipt_id, tuple(), "AMBIGUOUS",
                getattr(exc, "code", "PERSISTENCE_AMBIGUOUS"), bool(plan["reused"]),
                str(exc)[:2000], dict(getattr(exc, "details", {}) or {}),
            )
        except (ProviderError, ArtifactStoreError) as exc:
            if getattr(exc, "code", "") == "STALE_ATTEMPT":
                self._record_stale_lease(plan, receipt_id=receipt_id, error=exc)
                return TTSRunResult(
                    workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                    str(plan["submission_id"]), receipt_id, tuple(),
                    "WAITING_RETRY" if receipt_id is not None else "AMBIGUOUS",
                    exc.code, bool(plan["reused"]),
                    str(exc)[:2000], dict(getattr(exc, "details", {}) or {}),
                )
            ambiguous = getattr(exc, "ambiguous", None)
            ambiguous = False if ambiguous is None else bool(ambiguous)
            # Once record_tts_receipt has succeeded, the provider side effect
            # is known.  A failure while downloading, staging, or publishing
            # the output must retry from that receipt and never turn the
            # submission back into a fresh submit attempt.
            preserve_submission = receipt_id is not None and not ambiguous
            self.repository.mark_tts_failure(
                plan,
                error_code=getattr(exc, "code", "ARTIFACT_INVALID"),
                error_message=str(exc)[:2000],
                error_details=getattr(exc, "details", None),
                ambiguous=ambiguous,
                preserve_submission=preserve_submission,
            )
            return TTSRunResult(
                workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                str(plan["submission_id"]), receipt_id, tuple(),
                "AMBIGUOUS" if ambiguous else "WAITING_RETRY",
                getattr(exc, "code", "ARTIFACT_INVALID"), bool(plan["reused"]),
                str(exc)[:2000], dict(getattr(exc, "details", {}) or {}),
            )
        except Exception as exc:
            if getattr(exc, "code", "") == "STALE_ATTEMPT":
                self._record_stale_lease(plan, receipt_id=receipt_id, error=exc)
                return TTSRunResult(
                    workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                    str(plan["submission_id"]), receipt_id, tuple(),
                    "WAITING_RETRY" if receipt_id is not None else "AMBIGUOUS",
                    exc.code, bool(plan["reused"]),
                    str(exc)[:2000], dict(getattr(exc, "details", {}) or {}),
                )
            self.repository.mark_tts_failure(
                plan,
                error_code=getattr(exc, "code", "PERSISTENCE_AMBIGUOUS"),
                error_message=str(exc)[:2000],
                error_details=getattr(exc, "details", None),
                ambiguous=True,
            )
            return TTSRunResult(
                workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
                str(plan["submission_id"]), receipt_id, tuple(), "AMBIGUOUS",
                getattr(exc, "code", "PERSISTENCE_AMBIGUOUS"), bool(plan["reused"]),
                str(exc)[:2000], dict(getattr(exc, "details", {}) or {}),
            )

        if budget_id:
            current_budget = self.repository.get_budget(plan["workflow_group_id"], budget_key)
            if current_budget and int(current_budget["reserved_attempts"]) > 0:
                self.repository.commit_budget_use(budget_id)
        return TTSRunResult(
            workflow_id, str(plan["step_id"]), str(plan["attempt_id"]), str(plan["work_unit_id"]),
            str(plan["submission_id"]), receipt_id, tuple(artifact_ids), "SUCCEEDED", None, bool(plan["reused"]),
        )

    def _provider_call_with_lease(
        self,
        lease_id: str,
        owner_id: str,
        fencing_token: int,
        operation: Callable[[], Any],
    ) -> Any:
        """Fence and heartbeat one potentially long-running provider call."""

        self.repository.renew_lease(
            lease_id,
            owner_id,
            fencing_token,
            ttl_seconds=PROVIDER_LEASE_TTL_SECONDS,
        )
        with _LeaseHeartbeat(
            self.repository,
            lease_id,
            owner_id,
            fencing_token,
            ttl_seconds=PROVIDER_LEASE_TTL_SECONDS,
            interval_seconds=PROVIDER_LEASE_HEARTBEAT_INTERVAL_SECONDS,
        ):
            result = operation()
        # A response received after fencing is no longer valid for publication.
        # The caller handles STALE_ATTEMPT as an ambiguous or receipt-preserving
        # local transition, depending on whether a durable receipt exists.
        self.repository.renew_lease(
            lease_id,
            owner_id,
            fencing_token,
            ttl_seconds=PROVIDER_LEASE_TTL_SECONDS,
        )
        return result

    def _record_stale_lease(
        self,
        plan: Mapping[str, Any],
        *,
        receipt_id: str | None,
        error: Exception,
    ) -> None:
        """Converge a lost lease without requiring the lost lease itself."""

        preserve_submission = receipt_id is not None
        try:
            self.repository.mark_tts_failure(
                plan,
                error_code="STALE_ATTEMPT",
                error_message=str(error)[:2000] or "provider lease is stale or expired",
                error_details=getattr(error, "details", None),
                ambiguous=not preserve_submission,
                preserve_submission=preserve_submission,
                require_lease=False,
            )
        except Exception:
            # Recovery still has the durable submission/receipt boundary.  A
            # secondary local failure must not hide the original stale result.
            pass

    @staticmethod
    def _role_key(value: Any) -> str:
        return " ".join(str(value or "").strip().split()).casefold()

    @classmethod
    def _effective_plan_item(
        cls,
        item: Mapping[str, Any],
        configuration: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Materialize renderer voice settings into the durable TTS plan.

        Parsed documents normally do not contain a voice assignment.  The
        desktop renderer owns the selection UI, while the worker owns the
        accepted workflow snapshot; resolving the two here makes the provider
        payload independent of renderer memory and ensures the exact voice and
        parameters are included in the idempotency hash.
        """

        config = configuration if isinstance(configuration, Mapping) else {}
        role = cls._role_key(item.get("role"))
        role_voices = config.get("role_voices")
        role_voices = role_voices if isinstance(role_voices, Mapping) else {}
        role_voice = (
            (role_voices.get(role) or role_voices.get(f"role:{role}"))
            if role
            else None
        )
        male_role = bool(re.match(r"^(mr|mr\.|sir|男|先生)\b", role))
        default_female = str(config.get("default_female_voice") or "amanda").strip()
        default_male = str(config.get("default_male_voice") or "george").strip()
        voice_key = str(item.get("voice_key") or role_voice or (default_male if male_role else default_female) or "amanda").strip()

        role_configs = config.get("role_configs")
        role_configs = role_configs if isinstance(role_configs, Mapping) else {}
        voice_configs = config.get("voice_configs")
        voice_configs = voice_configs if isinstance(voice_configs, Mapping) else {}
        if role:
            params = role_configs.get(f"role:{role}")
        else:
            params = None
        if not isinstance(params, Mapping):
            params = role_configs.get("__default_male__" if male_role else "__default_female__")
        if not isinstance(params, Mapping):
            params = voice_configs.get(voice_key)
        if not isinstance(params, Mapping):
            params = config

        def parameter(name: str, fallback: int) -> int:
            value = item.get(name)
            if value is None:
                value = params.get("rate" if name == "speed" else name, fallback)
            try:
                number = round(float(value))
            except (TypeError, ValueError, OverflowError):
                number = fallback
            return max(0, min(100, int(number)))

        result = dict(item)
        result["voice_key"] = voice_key
        result["speed"] = parameter("speed", 35 if male_role else 50)
        result["pitch"] = parameter("pitch", 50)
        result["volume"] = parameter("volume", 50)
        return result

    @staticmethod
    def _cancel_requested(cancel_check: Callable[[], bool] | None) -> bool:
        if not callable(cancel_check):
            return False
        try:
            return bool(cancel_check())
        except Exception:
            # A broken cancellation probe must not convert an otherwise
            # valid provider run into an invented cancellation outcome.
            return False

    def _wait_if_paused(
        self,
        workflow_id: str,
        cancel_check: Callable[[], bool] | None,
        pause_check: Callable[[], bool] | None,
    ) -> None:
        """Cooperatively park at a safe boundary until resume is durable."""

        if not callable(pause_check):
            return
        while True:
            try:
                paused = bool(pause_check())
            except Exception:
                # A missing in-memory probe must not invent a pause.  The
                # durable repository fence still protects publication.
                paused = False
            if not paused:
                return
            if self._cancel_requested(cancel_check):
                raise RepositoryError("generation cancelled while paused", code="WORKFLOW_CANCELLED")
            time.sleep(0.2)

    @staticmethod
    def _receipt_mapping(receipt: Any) -> dict[str, Any]:
        provider = str(getattr(receipt, "provider", ""))
        account_scope = str(getattr(receipt, "account_scope", ""))
        provider_job_id = str(getattr(receipt, "provider_job_id", ""))
        canonical_key = str(getattr(receipt, "canonical_key", "") or provider_job_id)
        summary = getattr(receipt, "summary", None)
        summary = dict(summary) if isinstance(summary, Mapping) else {}
        summary.setdefault("provider_job_id", provider_job_id)
        temporary_works_id = getattr(receipt, "temporary_works_id", None)
        formal_works_id = getattr(receipt, "formal_works_id", None)
        if temporary_works_id:
            summary.setdefault("temporary_works_id", str(temporary_works_id))
        if formal_works_id:
            summary.setdefault("formal_works_id", str(formal_works_id))
        output_format = str(getattr(receipt, "output_format", "") or "").lower().lstrip(".")
        if output_format:
            summary.setdefault("format", output_format)
        if getattr(receipt, "segments", None) is not None:
            summary.setdefault("segment_boundaries_verified", True)
        return {
            "provider": provider,
            "account_scope": account_scope,
            "provider_job_id": provider_job_id,
            "canonical_key": canonical_key,
            "summary": summary,
        }

    @staticmethod
    def _capability_snapshot(provider: TTSProvider) -> dict[str, Any]:
        getter = getattr(provider, "capability_snapshot", None)
        if callable(getter):
            value = getter()
            if isinstance(value, Mapping):
                return dict(value)
        return {
            "provider": str(getattr(provider, "provider", "")),
            "account_scope": str(getattr(provider, "account_scope", "")),
            "capability_version": "legacy-port",
        }

    def _stage_output(
        self,
        plan: Mapping[str, Any],
        output: bytes,
        ordered_plan: list[Mapping[str, Any]],
        *,
        receipt: Any,
        generation_mode: str,
    ) -> list[dict[str, Any]]:
        summary = getattr(receipt, "summary", None)
        output_format = str(
            getattr(receipt, "output_format", None)
            or (summary.get("format") if isinstance(summary, Mapping) else None)
            or "bin"
        ).lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9][a-z0-9+_-]{0,15}", output_format):
            raise ProviderError("provider returned an invalid output format", code="ARTIFACT_INVALID", ambiguous=False)
        segments = getattr(receipt, "segments", None)
        if segments is None and isinstance(summary, Mapping):
            segments = summary.get("segments")
        if segments is None:
            if len(ordered_plan) == 1:
                segments = {str(ordered_plan[0]["item_id"]): output}
            else:
                raise ProviderError(
                    "composite provider output has no verified per-item segments",
                    code="SEGMENT_BOUNDARIES_UNVERIFIED",
                    ambiguous=False,
                )
        if not isinstance(segments, Mapping):
            raise ProviderError("provider item segments are malformed", code="ARTIFACT_INVALID", ambiguous=False)
        expected_item_ids = [str(item["item_id"]) for item in ordered_plan]
        normalized_segments: dict[str, bytes] = {}
        for item_id in expected_item_ids:
            value = segments.get(item_id)
            if not isinstance(value, (bytes, bytearray, memoryview)) or not value:
                raise ProviderError(
                    f"provider item segment is missing or empty: {item_id}",
                    code="SEGMENT_BOUNDARIES_UNVERIFIED",
                    ambiguous=False,
                )
            normalized_segments[item_id] = bytes(value)
        if {str(key) for key in segments} != set(expected_item_ids):
            raise ProviderError(
                "provider returned item segments outside the durable plan",
                code="SEGMENT_BOUNDARIES_UNVERIFIED",
                ambiguous=False,
            )

        specs: list[dict[str, Any]] = []
        staged = self.artifact_store.stage_stream(io.BytesIO(output))
        primary = self.artifact_store.promote(staged, format=output_format)
        provider_name = str(getattr(receipt, "provider", None) or plan.get("provider") or "tts-provider")
        specs.append({
            "blob": primary,
            "artifact_type": "tts-composite" if generation_mode == "composite_cut" else "tts-output",
            "producer": provider_name,
        })
        children = self.repository.list_work_unit_items(str(plan["work_unit_id"]))
        if len(children) != len(ordered_plan):
            raise RepositoryError("TTS work-unit item mapping is incomplete", code="PERSISTENCE_ERROR")
        children_by_item = {str(child["item_id"]): child for child in children}
        if set(children_by_item) != set(expected_item_ids):
            raise RepositoryError("TTS work-unit item mapping does not match the provider plan", code="PERSISTENCE_ERROR")
        for item_id in expected_item_ids:
            child = children_by_item[item_id]
            staged_segment = self.artifact_store.stage_stream(io.BytesIO(normalized_segments[item_id]))
            segment = self.artifact_store.promote(staged_segment, format=output_format)
            specs.append({
                "blob": segment,
                "artifact_type": "tts-segment",
                "item_id": item_id,
                "work_unit_segment_id": child["work_unit_segment_id"],
                "parent_index": 0,
                "relation_type": "CUT_SEGMENT",
                "producer": provider_name,
            })
        return specs

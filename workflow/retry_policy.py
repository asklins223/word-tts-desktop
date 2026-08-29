"""Conservative retry classification shared by scheduler and API commands."""

from __future__ import annotations

from dataclasses import dataclass
from random import Random


@dataclass(frozen=True)
class RetryDecision:
    error_code: str
    automatic: bool
    action: str
    delay_seconds: float
    reason: str


class RetryPolicy:
    RETRYABLE = {
        "TRANSIENT_PROVIDER_ERROR",
        "PROVIDER_RATE_LIMITED",
        "DOWNLOAD_ERROR",
        "PERSISTENCE_ERROR",
    }
    NEVER_AUTOMATIC = {
        "PERSISTENCE_AMBIGUOUS",
        "SUBMISSION_AMBIGUOUS",
        "STALE_ATTEMPT",
        "ARTIFACT_INVALID",
        "AUTH_ERROR",
        "VALIDATION_ERROR",
        "CONTENT_CONFLICT",
    }

    def __init__(self, *, max_attempts: int = 3, base_delay_seconds: float = 1.0, max_delay_seconds: float = 60.0, random: Random | None = None) -> None:
        self.max_attempts = max(0, int(max_attempts))
        self.base_delay_seconds = max(0.0, float(base_delay_seconds))
        self.max_delay_seconds = max(self.base_delay_seconds, float(max_delay_seconds))
        self.random = random or Random()

    def decide(
        self,
        error_code: str,
        *,
        attempt_no: int,
        side_effect_state: str = "NOT_STARTED",
        retry_after_seconds: float | None = None,
    ) -> RetryDecision:
        if side_effect_state in {"IN_FLIGHT", "SUBMITTED", "AMBIGUOUS"} or error_code in {
            "PERSISTENCE_AMBIGUOUS", "SUBMISSION_AMBIGUOUS"
        }:
            return RetryDecision(error_code, False, "RECONCILE", 0.0, "副作用边界不明确，只允许对账")
        if error_code in self.NEVER_AUTOMATIC:
            return RetryDecision(error_code, False, "MANUAL", 0.0, "错误不可安全自动重试")
        if error_code not in self.RETRYABLE or attempt_no >= self.max_attempts:
            return RetryDecision(error_code, False, "FAIL", 0.0, "重试策略或持久预算不允许继续")
        if retry_after_seconds is not None:
            delay = max(0.0, min(float(retry_after_seconds), self.max_delay_seconds))
        else:
            ceiling = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** max(0, attempt_no - 1)))
            delay = self.random.uniform(0.0, ceiling) if ceiling else 0.0
        return RetryDecision(error_code, True, "RETRY", delay, "可重试的纯/提交前错误")

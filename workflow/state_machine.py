"""Centralized workflow/step/control transitions and target validation."""

from __future__ import annotations

from typing import Mapping

from .domain import CommandTarget, DomainError


class InvalidTransition(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__("STATE_CONFLICT", message)


RESULT_TERMINAL = {"SUCCEEDED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"}
EXECUTION_TERMINAL = "TERMINAL"
CONTROL_TERMINAL = "TERMINATED"

EXECUTION_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"PREPARING", "RUNNING", "BLOCKED", "TERMINAL"},
    "PREPARING": {"RUNNING", "WAITING_USER", "BLOCKED", "RECOVERING", "TERMINAL"},
    "RUNNING": {"WAITING_RETRY", "WAITING_USER", "RECOVERING", "BLOCKED", "TERMINAL"},
    "WAITING_RETRY": {"RUNNING", "WAITING_USER", "BLOCKED", "TERMINAL"},
    "WAITING_USER": {"RUNNING", "RECOVERING", "BLOCKED", "TERMINAL"},
    "RECOVERING": {"PREPARING", "RUNNING", "WAITING_RETRY", "WAITING_USER", "BLOCKED", "TERMINAL"},
    "BLOCKED": {"RECOVERING", "WAITING_USER", "TERMINAL"},
    "TERMINAL": set(),
}

CONTROL_TRANSITIONS: dict[str, set[str]] = {
    "RUNNING": {"PAUSE_REQUESTED", "TERMINATING", "TERMINATED"},
    "PAUSE_REQUESTED": {"PAUSED", "RUNNING", "TERMINATING"},
    "PAUSED": {"RUNNING", "TERMINATING", "TERMINATED"},
    "TERMINATING": {"TERMINATED", "RUNNING", "PAUSED"},
    "TERMINATED": set(),
}

STEP_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"READY", "PREPARING", "CANCELLED", "BLOCKED"},
    "READY": {"PREPARING", "RUNNING", "WAITING_USER", "CANCELLED", "BLOCKED"},
    "PREPARING": {"RUNNING", "WAITING_RETRY", "WAITING_USER", "BLOCKED", "CANCELLED"},
    "RUNNING": {"VERIFYING", "SUCCEEDED", "WAITING_RETRY", "RETRYABLE_FAILED", "PERMANENT_FAILED", "AMBIGUOUS", "WAITING_USER", "BLOCKED", "CANCELLED"},
    "VERIFYING": {"SUCCEEDED", "WAITING_RETRY", "RETRYABLE_FAILED", "PERMANENT_FAILED", "AMBIGUOUS", "WAITING_USER", "BLOCKED", "CANCELLED"},
    "SUCCEEDED": set(),
    "WAITING_RETRY": {"READY", "RUNNING", "WAITING_USER", "BLOCKED", "CANCELLED"},
    "RETRYABLE_FAILED": {"READY", "RUNNING", "WAITING_USER", "BLOCKED", "CANCELLED"},
    "PERMANENT_FAILED": set(),
    "AMBIGUOUS": {"READY", "VERIFYING", "WAITING_USER", "BLOCKED", "CANCELLED"},
    "WAITING_USER": {"READY", "VERIFYING", "RUNNING", "BLOCKED", "CANCELLED"},
    "BLOCKED": {"RECOVERING"},
    "CANCELLED": set(),
}


def require_expected(actual: int, expected: int) -> None:
    if actual != expected:
        raise InvalidTransition(f"state_version conflict: expected {expected}, current {actual}")


def transition(current: str, target: str, table: Mapping[str, set[str]], label: str) -> str:
    if current == target:
        return target
    if target not in table.get(current, set()):
        raise InvalidTransition(f"invalid {label} transition: {current} -> {target}")
    return target


def transition_execution(current: str, target: str) -> str:
    return transition(current, target, EXECUTION_TRANSITIONS, "execution_state")


def transition_control(current: str, target: str) -> str:
    return transition(current, target, CONTROL_TRANSITIONS, "control_state")


def transition_step(current: str, target: str) -> str:
    return transition(current, target, STEP_TRANSITIONS, "step status")


def validate_target(target: CommandTarget | Mapping[str, object]) -> CommandTarget:
    return target if isinstance(target, CommandTarget) else CommandTarget.from_mapping(target)


def command_transition(snapshot: Mapping[str, object], action: str) -> dict[str, str]:
    """Return only fields changed by a workflow-level command.

    Repositories apply this result with an optimistic version predicate.  The
    function is pure so routes and recovery code cannot invent a second state
    machine.
    """

    current_execution = str(snapshot["execution_state"])
    current_control = str(snapshot["control_state"])
    status = str(snapshot["status"])
    result: dict[str, str] = {}
    if action == "parse":
        if status != "DRAFT":
            raise InvalidTransition("parse is only valid for a draft workflow")
        result.update(status="ACTIVE", execution_state=transition_execution(current_execution, "PREPARING"))
    elif action == "generate":
        if current_control == "PAUSED":
            result["control_state"] = transition_control(current_control, "RUNNING")
        result["execution_state"] = transition_execution(current_execution, "RUNNING")
        if status == "DRAFT":
            result["status"] = "ACTIVE"
    elif action == "pause":
        if current_control == "RUNNING":
            result["control_state"] = transition_control(current_control, "PAUSE_REQUESTED")
        elif current_control != "PAUSED":
            raise InvalidTransition(f"cannot pause from {current_control}")
    elif action == "resume":
        if current_control == "PAUSED":
            result["control_state"] = transition_control(current_control, "RUNNING")
        elif current_control == "PAUSE_REQUESTED":
            result["control_state"] = transition_control(current_control, "RUNNING")
        else:
            raise InvalidTransition(f"cannot resume from {current_control}")
    elif action == "cancel":
        if current_control != "TERMINATED":
            result["control_state"] = transition_control(current_control, "TERMINATING")
            result["execution_state"] = transition_execution(current_execution, "BLOCKED")
    else:
        raise DomainError("VALIDATION_ERROR", f"unsupported workflow action: {action}")
    return result


def require_not_terminal(result_status: str, execution_state: str) -> None:
    if result_status in RESULT_TERMINAL or execution_state == EXECUTION_TERMINAL:
        raise InvalidTransition("terminal workflow cannot be reopened")

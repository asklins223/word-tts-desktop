"""Opt-in timing for visible-browser automation.

The normal path does not write logs or add browser calls.  Set
``WORDTTS_BROWSER_PERF=1`` to emit one compact JSON record for slow page
steps and waits; this makes an occasional Windows stall attributable to a
specific stage instead of leaving it as an unexplained pause.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from typing import Any, Iterator


def _truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def browser_perf_enabled() -> bool:
    return _truthy(
        os.environ.get("WORDTTS_BROWSER_PERF")
        or os.environ.get("PLATFORM_INPUT_PERF")
    )


def _minimum_duration_ms() -> float:
    try:
        return max(0.0, float(os.environ.get("WORDTTS_BROWSER_PERF_MIN_MS", "25")))
    except (TypeError, ValueError):
        return 25.0


class BrowserPerfTracer:
    """Small, dependency-free tracer shared by page actions and waits."""

    def __init__(self, operation: str = "browser") -> None:
        self.operation = str(operation or "browser")
        self.enabled = browser_perf_enabled()
        self.minimum_duration_ms = _minimum_duration_ms()

    @contextmanager
    def span(self, name: str, **fields: Any) -> Iterator[dict[str, Any]]:
        metadata = dict(fields)
        if not self.enabled:
            yield metadata
            return

        started = time.perf_counter()
        error: BaseException | None = None
        try:
            yield metadata
        except BaseException as exc:
            error = exc
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            if error is not None or duration_ms >= self.minimum_duration_ms:
                payload = {
                    "operation": self.operation,
                    "name": str(name),
                    "duration_ms": round(duration_ms, 1),
                    **metadata,
                }
                if error is not None:
                    payload["error"] = type(error).__name__
                self._emit(payload)

    def _emit(self, payload: dict[str, Any]) -> None:
        try:
            print(
                "[browser-perf] "
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                file=sys.stderr,
                flush=True,
            )
        except Exception:
            # Diagnostics must never turn a successful page action into a
            # failed input run, even if stderr has been closed by the host.
            pass


def page_perf(page: Any, *, operation: str = "browser") -> BrowserPerfTracer:
    """Return one tracer for a page without requiring a Playwright subclass."""

    attribute = "_wordtts_browser_perf_tracer"
    try:
        existing = getattr(page, attribute, None)
    except Exception:
        existing = None
    if isinstance(existing, BrowserPerfTracer):
        return existing

    tracer = BrowserPerfTracer(operation)
    try:
        setattr(page, attribute, tracer)
    except Exception:
        pass
    return tracer


__all__ = ["BrowserPerfTracer", "browser_perf_enabled", "page_perf"]

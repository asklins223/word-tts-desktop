"""Process-local capability and one-time ticket primitives.

Tickets are deliberately memory-only: restarting the backend invalidates every
ticket and avoids persisting bearer material in the workflow database.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass


class TicketError(RuntimeError):
    code = "UNAUTHORIZED"


class TicketExpired(TicketError):
    code = "CURSOR_EXPIRED"


@dataclass
class _Ticket:
    token_hash: str
    action: str
    resource_id: str
    audience: str
    expires_at: float
    used: bool = False


class OneTimeTicketManager:
    def __init__(self, *, max_ttl_seconds: int = 300, max_tickets: int = 4096, clock=time.monotonic) -> None:
        self.max_ttl_seconds = max(1, int(max_ttl_seconds))
        self.max_tickets = max(16, int(max_tickets))
        self._clock = clock
        self._tickets: dict[str, _Ticket] = {}
        # Keep a bounded tombstone window so an immediately presented expired
        # bearer still gets the intentional expiry error without allowing
        # unclaimed expired tickets to exhaust live-ticket capacity.
        self._expired_tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(self, *, action: str, resource_id: str, audience: str, ttl_seconds: int = 60) -> tuple[str, float]:
        ttl = min(max(1, int(ttl_seconds)), self.max_ttl_seconds)
        token = secrets.token_urlsafe(32)
        expires_at = self._clock() + ttl
        ticket = _Ticket(self._hash(token), action, resource_id, audience, expires_at)
        with self._lock:
            self._purge_locked()
            if len(self._tickets) >= self.max_tickets:
                raise TicketError("ticket capacity is exhausted")
            self._tickets[ticket.token_hash] = ticket
        return token, expires_at

    def consume(self, token: str, *, action: str, resource_id: str, audience: str) -> None:
        if not isinstance(token, str) or not token:
            raise TicketError("ticket is required")
        token_hash = self._hash(token)
        with self._lock:
            self._purge_locked()
            ticket = self._tickets.get(token_hash)
            if ticket is None:
                if token_hash in self._expired_tokens:
                    self._expired_tokens.pop(token_hash, None)
                    raise TicketExpired("ticket has expired")
                raise TicketError("ticket is invalid or already consumed")
            if ticket.used:
                raise TicketError("ticket is invalid or already consumed")
            if self._clock() >= ticket.expires_at:
                ticket.used = True
                raise TicketExpired("ticket has expired")
            if not (
                hmac.compare_digest(ticket.action, action)
                and hmac.compare_digest(ticket.resource_id, resource_id)
                and hmac.compare_digest(ticket.audience, audience)
            ):
                raise TicketError("ticket audience or resource mismatch")
            ticket.used = True

    def _purge_locked(self) -> None:
        now = self._clock()
        # Used entries can be removed immediately. Move expired, unclaimed
        # entries into a bounded tombstone set instead of retaining them in
        # the capacity-counted live map forever.
        stale = [key for key, item in self._tickets.items() if item.used]
        for key in stale:
            self._tickets.pop(key, None)
        expired = [key for key, item in self._tickets.items() if now >= item.expires_at]
        for key in expired:
            item = self._tickets.pop(key, None)
            if item is not None:
                self._expired_tokens[key] = item.expires_at

        # Retain tombstones for at most one maximum ticket lifetime. This is
        # long enough for normal clients to observe expiry and bounds memory
        # even when clients issue tickets but never present them.
        old_tombstones = [
            key for key, expires_at in self._expired_tokens.items()
            if now >= expires_at + self.max_ttl_seconds
        ]
        for key in old_tombstones:
            self._expired_tokens.pop(key, None)
        while len(self._expired_tokens) > self.max_tickets:
            oldest = next(iter(self._expired_tokens), None)
            if oldest is None:
                break
            self._expired_tokens.pop(oldest, None)

    def invalidate_all(self) -> None:
        with self._lock:
            self._tickets.clear()
            self._expired_tokens.clear()


def verify_capability(supplied: str | None, expected: str | None) -> bool:
    if not supplied or not expected:
        return False
    return hmac.compare_digest(str(supplied), str(expected))

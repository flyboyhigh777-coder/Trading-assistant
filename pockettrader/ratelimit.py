"""Per-user command throttling.

Deliberately keyed on the Telegram user, not on an IP: users behind one carrier
NAT or one office egress share an address, so an IP bucket punishes bystanders
for someone else's spam.  The user id is the thing we actually want to limit.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: float = 0.0


class RateLimiter:
    """Sliding-window limiter: at most ``limit`` hits per ``window`` seconds."""

    def __init__(self, limit: int = 20, window: float = 60.0):
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window <= 0:
            raise ValueError("window must be positive")
        self.limit = limit
        self.window = window
        self._lock = threading.Lock()
        self._hits: dict[int, deque[float]] = {}

    def check(self, user_id: int, now: float | None = None) -> Decision:
        """Record a hit and say whether it is allowed."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(int(user_id), deque())
            cutoff = now - self.window
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return Decision(False, retry_after=max(hits[0] + self.window - now, 0.0))
            hits.append(now)
            return Decision(True)

    def reset(self, user_id: int | None = None) -> None:
        with self._lock:
            if user_id is None:
                self._hits.clear()
            else:
                self._hits.pop(int(user_id), None)

"""Sliding-window, thread-safe rate limiter.

Guarantees no more than ``max_rpm`` requests start in any 60-second
window. Threads call ``acquire()`` before each request; if the budget
is exhausted the call blocks until the oldest in-window timestamp
ages past 60s.

This is the *prevention* layer for 429 errors. The retry-on-429 path
in the Gemini client is the *fallback* layer; if the rate limiter is
configured correctly, the fallback should fire vanishingly rarely.

Design notes
------------
- Pure stdlib; no async, no asyncio. Workers use a small thread pool.
- The lock is held only while inspecting/updating the deque, never
  during ``time.sleep`` — many threads can wait concurrently.
- ``_max`` is clamped to ``>= 1`` so misconfiguration cannot deadlock.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque


class RateLimiter:
    """Hard cap on requests-per-minute across all calling threads."""

    def __init__(self, max_rpm: int) -> None:
        self._max: int = max(1, int(max_rpm))
        self._window: Deque[float] = deque()
        self._lock = threading.Lock()

    @property
    def max_rpm(self) -> int:
        return self._max

    def acquire(self) -> None:
        """Block until the caller is allowed to fire one request."""
        while True:
            with self._lock:
                now = time.monotonic()
                # Age out timestamps older than the 60-second window
                while self._window and now - self._window[0] >= 60.0:
                    self._window.popleft()
                if len(self._window) < self._max:
                    self._window.append(now)
                    return
                # Budget exhausted — compute wait until the oldest expires.
                # +0.05s slop avoids busy-looping on the exact boundary.
                wait = 60.0 - (now - self._window[0]) + 0.05
            time.sleep(wait)

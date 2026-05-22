"""Append-only, SHA-256-keyed verdict cache.

The cache key is ``sha256(judge_id || "\n" || prompt)``. Same (judge,
prompt) always returns the same verdict. Different judge → different
cache entry. Different prompt → different cache entry. There is no
silent cross-judge contamination — switching the judge invalidates
the cache cleanly.

Invariants enforced by the API
------------------------------

- ``append`` is the only mutating operation.
- ``load`` reads the file once and returns a dict; later entries with
  the same key shadow earlier ones (last-write-wins).
- Corruption is impossible by code: only ``rm`` clears the cache.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class VerdictCache:
    """File-backed cache for judge verdicts."""

    def __init__(self, path: Path) -> None:
        self._path: Path = path
        self._mem: dict[str, dict[str, Any]] | None = None

    # ── Path / key helpers ────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def key(*, judge_id: str, prompt: str) -> str:
        return hashlib.sha256(
            f"{judge_id}\n{prompt}".encode("utf-8")
        ).hexdigest()

    # ── Public API ────────────────────────────────────────────────────

    def load(self) -> dict[str, dict[str, Any]]:
        """Return the full cache as ``{key: verdict_dict}``. Cached in-process."""
        if self._mem is not None:
            return self._mem
        result: dict[str, dict[str, Any]] = {}
        if self._path.exists():
            with self._path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        result[rec["key"]] = rec["verdict"]
                    except (json.JSONDecodeError, KeyError):
                        continue
        self._mem = result
        return result

    def get(self, *, judge_id: str, prompt: str) -> dict[str, Any] | None:
        return self.load().get(self.key(judge_id=judge_id, prompt=prompt))

    def append(
        self, *, judge_id: str, prompt: str, verdict: dict[str, Any],
    ) -> str:
        """Append a verdict to the on-disk cache. Returns the cache key."""
        k = self.key(judge_id=judge_id, prompt=prompt)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"key": k, "judge_id": judge_id, "verdict": verdict},
                    ensure_ascii=False,
                )
                + "\n"
            )
        if self._mem is not None:
            self._mem[k] = verdict
        return k

    def invalidate_mem(self) -> None:
        """Drop the in-process cache. Forces a fresh disk read on next ``load``."""
        self._mem = None

    def __len__(self) -> int:
        return len(self.load())

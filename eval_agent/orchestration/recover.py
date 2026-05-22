"""Rebuild verdict cache + bootstrap state files from ``state/runs/``.

Phase 2 ``recover`` command. When the cache file is missing or
corrupt — but per-run ``results.jsonl`` files survived — we can
reconstruct the deduplicated verdict cache without re-judging anything.
Also lazily bootstraps ``feature_list.json`` and ``progress.md`` in the
given ``state_dir`` if they are missing.

The recovery is idempotent: a second call against an already-rebuilt
cache adds zero rows and leaves the file byte-size unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RecoverReport:
    """Outcome of a single ``recover`` invocation."""

    cache_rebuilt: bool
    cache_entries_recovered: int
    feature_list_bootstrapped: bool
    progress_md_bootstrapped: bool


def _read_existing_cache_keys(cache_path: Path) -> set[str]:
    """Return the set of cache keys already present in the on-disk cache.

    Malformed rows are skipped silently (same policy as VerdictCache.load).
    A missing file returns an empty set.
    """
    keys: set[str] = set()
    if not cache_path.exists():
        return keys
    with cache_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                k = rec.get("key")
                if isinstance(k, str) and len(k) == 64:
                    keys.add(k)
            except json.JSONDecodeError:
                continue
    return keys


def _iter_run_rows(runs_dir: Path) -> list[dict[str, Any]]:
    """Yield every JSONL row across ``runs_dir/*/results.jsonl``.

    Rows are returned in filesystem-sorted order so two recover calls on
    the same disk state pick the same last-write-wins winner per key.
    """
    rows: list[dict[str, Any]] = []
    if not runs_dir.is_dir():
        return rows
    for results_path in sorted(runs_dir.glob("*/results.jsonl")):
        with results_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _bootstrap_feature_list(state_dir: Path) -> bool:
    path = state_dir / "feature_list.json"
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"version": 1, "features": []}
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


def _bootstrap_progress_md(state_dir: Path) -> bool:
    path = state_dir / "progress.md"
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# eval-agent — session log\n", encoding="utf-8")
    return True


def recover(*, state_dir: Path) -> RecoverReport:
    """Rebuild the verdict cache from ``state_dir/runs/*/results.jsonl``.

    Skips run rows that have a truthy ``error`` or are missing
    ``cache_key`` / ``judge_id``. Deduplicates by ``cache_key``;
    last-write-wins across runs (sorted by run directory name).
    """
    cache_path = state_dir / "cache" / "verdict_cache.jsonl"
    runs_dir = state_dir / "runs"

    feature_list_bootstrapped = _bootstrap_feature_list(state_dir)
    progress_md_bootstrapped = _bootstrap_progress_md(state_dir)

    existing_keys = _read_existing_cache_keys(cache_path)

    # Collect deduplicated reconstruction (last-write-wins).
    reconstructed: dict[str, dict[str, Any]] = {}
    for row in _iter_run_rows(runs_dir):
        if row.get("error"):
            continue
        cache_key = row.get("cache_key")
        judge_id = row.get("judge_id")
        verdict = row.get("verdict")
        if not isinstance(cache_key, str) or len(cache_key) != 64:
            continue
        if not isinstance(judge_id, str) or not judge_id:
            continue
        if not isinstance(verdict, dict):
            continue
        reconstructed[cache_key] = {
            "key": cache_key,
            "judge_id": judge_id,
            "verdict": verdict,
        }

    # Only write rows whose key is not already present on disk.
    new_rows = [r for k, r in reconstructed.items() if k not in existing_keys]

    if new_rows:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("a", encoding="utf-8") as f:
            for rec in new_rows:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    total_on_disk = len(existing_keys) + len(new_rows)

    return RecoverReport(
        cache_rebuilt=bool(new_rows),
        cache_entries_recovered=total_on_disk,
        feature_list_bootstrapped=feature_list_bootstrapped,
        progress_md_bootstrapped=progress_md_bootstrapped,
    )

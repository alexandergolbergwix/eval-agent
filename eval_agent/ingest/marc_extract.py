"""Read MHM-Pipeline ``marc_extracted.json`` from disk.

Stage 1 of the pipeline emits a post-processed, semantic-key dict per
record. We never re-derive these; we treat them as the contract.

Public surface
--------------
``load(path)`` — read + return ``list[dict]``.
``index_by_id(records)`` — return ``{record_id: record}`` for fast lookup.
``project(record, keys)`` — pick a subset of keys, coerced to human-readable
strings (lists of dicts → ``|``-joined JSON, dicts → JSON, etc.).

The ``notes`` key gets special treatment: pipeline Stage 1 prepends
the source filename as ``notes[0]``; we strip it because Gemini
mistakes filenames for content.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load(path: Path) -> list[dict[str, Any]]:
    """Read ``marc_extracted.json``."""
    return json.loads(path.read_text(encoding="utf-8"))


def index_by_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return ``{_control_number: record}``. Empty-id records skipped."""
    out: dict[str, dict[str, Any]] = {}
    for r in records:
        rid = r.get("_control_number") or r.get("001") or ""
        if rid:
            out[str(rid)] = r
    return out


def project(record: dict[str, Any], keys: list[str]) -> dict[str, str]:
    """Pick keys from a record and coerce values to single-line strings.

    Reusable across evaluators. Each evaluator declares its own ``keys``
    list — that's the per-evaluator MARC slice (context engineering).
    """
    out: dict[str, str] = {}
    for k in keys:
        v = record.get(k)
        if v is None or v == "" or v == []:
            continue
        if k == "notes" and isinstance(v, list):
            # First element is the source filename (a pipeline marker); skip.
            real = [str(x) for x in v[1:] if x]
            if not real:
                continue
            out[k] = " | ".join(real)
            continue
        if isinstance(v, list):
            out[k] = " | ".join(
                json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else str(x)
                for x in v if x
            )
        elif isinstance(v, dict):
            out[k] = json.dumps(v, ensure_ascii=False)
        else:
            out[k] = str(v)
    return out

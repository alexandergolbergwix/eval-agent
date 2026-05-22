"""Read MHM-Pipeline ``ner_results.json`` from disk.

Stage 2 of the pipeline emits one record per input row with:

  - ``entities``           — list of entity dicts (source ∈ {person_ner,
                              provenance_ner, contents_ner})
  - ``ml_genres``          — list of {label, confidence}
  - ``ml_colophon_sentences`` — list[str] (or list[dict] in older pipeline versions)

Each entity dict carries ``confidence`` (always present) and may
carry ``model_confidence`` (person NER only — real softmax over the
span's tokens). The eval-agent uses ``confidence`` uniformly across
all sources — see pipeline CLAUDE.md Rule 41.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def get_entities(record: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """Filter ``record['entities']`` to a single ``source``."""
    return [e for e in (record.get("entities") or []) if e.get("source") == source]


def get_ml_genres(record: dict[str, Any]) -> list[dict[str, Any]]:
    return list(record.get("ml_genres") or [])


def get_ml_colophon_sentences(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise ``ml_colophon_sentences`` to a list of ``{sentence, confidence}``.

    Old pipeline versions emit ``list[str]``; new emit ``list[dict]``. We
    return the dict shape uniformly so evaluators don't need to branch.
    """
    raw = record.get("ml_colophon_sentences") or []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            sentence = item
            conf = 1.0  # passed the per-fold threshold during classification
        elif isinstance(item, dict):
            sentence = item.get("text") or item.get("sentence") or ""
            conf = float(item.get("confidence", 1.0))
        else:
            continue
        if not sentence:
            continue
        out.append({"sentence": sentence, "confidence": conf})
    return out


def get_confidence(entity: dict[str, Any]) -> float:
    """Return entity's ``confidence`` (always; not ``model_confidence``).

    Pipeline CLAUDE.md Rule 41 documents the semantics:
    - Person NER: bimodal 0.60/0.85 keyword-classifier score.
    - Other sources: continuous softmax/sigmoid.

    Using ``confidence`` uniformly is what the GUI's auto-approve
    gate uses, so this matches user-facing behaviour.
    """
    c = entity.get("confidence")
    if c is None:
        return 0.0
    try:
        return float(c)
    except (TypeError, ValueError):
        return 0.0

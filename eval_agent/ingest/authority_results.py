"""Read MHM-Pipeline ``authority_enriched.json`` from disk.

Stage 3 (authority resolution) emits one record per input row. Each
record is the Stage-1 MARC dict PLUS two enrichment keys:

  - ``marc_authority_matches`` — list of match dicts (name → authority id)
  - ``kima_places``            — dict of place name → Wikidata URI

The eval-agent never imports pipeline Python — this module is the only
contract for the authority-evaluation path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Authority match ``confidence`` is a tri-level enum in newer output and
# may be absent in older output. Map to a 0..1 float so the threshold
# filter (shared with the NER path) works uniformly.
_CONFIDENCE_MAP: dict[str, float] = {"high": 0.9, "medium": 0.6, "low": 0.3}


def load(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def get_matches(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``record['marc_authority_matches']`` (empty list if absent)."""
    return list(record.get("marc_authority_matches") or [])


def has_authority_id(match: dict[str, Any]) -> bool:
    """True when the match resolved to any authority identifier.

    A match with no Mazal / VIAF / Wikidata id is an *unmatched* row —
    there is no authority record to verify, so the evaluator skips it.
    """
    return bool(
        match.get("mazal_id")
        or match.get("viaf_uri")
        or match.get("wikidata_qid")
    )


def get_enriched_entities(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return NER ``entities`` that were enriched with an authority id.

    Stage 3 attaches ``mazal_id`` / ``viaf_uri`` / ``wikidata_qid`` onto
    the NER entity dict when a name resolves. These are authority
    decisions the curator reviews in the editor's NER-entity rows, so the
    evaluator judges them too. Entity dicts use ``person`` (or ``text``)
    for the surface form rather than ``name``; normalise to the
    match-shape the evaluator's prompt builder expects.
    """
    out: list[dict[str, Any]] = []
    for ent in record.get("entities") or []:
        if not has_authority_id(ent):
            continue
        name = str(ent.get("person") or ent.get("text") or ent.get("name") or "")
        out.append(
            {
                "name": name,
                "role": str(ent.get("role") or ""),
                "field": str(ent.get("grounded_field") or ent.get("source") or ""),
                "matched_name": str(ent.get("preferred_name_lat") or name),
                "mazal_id": ent.get("mazal_id") or "",
                "viaf_uri": ent.get("viaf_uri") or "",
                "wikidata_qid": ent.get("wikidata_qid") or "",
                "confidence": ent.get("model_confidence", ent.get("confidence")),
                "_origin": "entity",
                "_entity_kind": str(ent.get("type") or ""),
            }
        )
    return out


def places_as_matches(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``kima_places`` as match-shaped dicts for evaluation."""
    out: list[dict[str, Any]] = []
    for name, uri in get_places(record).items():
        qid = ""
        if isinstance(uri, str) and "/entity/" in uri:
            qid = uri.rsplit("/entity/", 1)[-1].strip("/")
        out.append(
            {
                "name": str(name),
                "role": "place",
                "field": "651",
                "matched_name": str(name),
                "wikidata_qid": qid,
                "source": "kima",
                "confidence": "high",
                "_origin": "kima",
                "match_type": "place",
            }
        )
    return out


def get_confidence(match: dict[str, Any]) -> float:
    """Coerce a match's confidence to a 0..1 float.

    Accepts the tri-level enum ("high"/"medium"/"low"), a numeric value,
    or falls back to the ``matched`` flag (1 → 0.85, 0 → 0.0) for older
    output that lacks an explicit confidence.
    """
    c = match.get("confidence")
    if isinstance(c, (int, float)):
        try:
            return float(c)
        except (TypeError, ValueError):
            return 0.0
    if isinstance(c, str):
        return _CONFIDENCE_MAP.get(c.strip().lower(), 0.5)
    # No explicit confidence — infer from the matched flag.
    return 0.85 if match.get("matched") else 0.0


def get_places(record: dict[str, Any]) -> dict[str, str]:
    """Return the ``kima_places`` {name: wikidata_uri} dict (or empty)."""
    places = record.get("kima_places")
    return dict(places) if isinstance(places, dict) else {}


def match_identity(match: dict[str, Any], index: int) -> str:
    """Stable per-match identity within a record: name|field|role|index."""
    name = str(match.get("name", ""))
    field = str(match.get("field", ""))
    role = str(match.get("role", ""))
    return f"{name}|{field}|{role}|{index}"

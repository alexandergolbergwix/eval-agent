"""Pluggable Evaluator interface.

An Evaluator declares:

  - ``id``                — canonical name (matches feature_list.json)
  - ``sub_types``          — categories broken out in metrics
  - ``marc_field_keys``    — semantic MARC slice this evaluator needs
  - ``confidence_field``   — which entity key drives the threshold filter
  - ``rubric_name``        — name of the per-evaluator rubric markdown

And implements:

  - ``extract_candidates(record, marc, threshold)`` → list[Candidate]
  - ``build_prompt(candidate)``                     → str
  - ``parse_verdict(raw, candidate)``               → Verdict

Adding a new evaluator (e.g. for Stage 3 authority resolution) is one
new module under ``eval_agent/evaluators/`` + a rubric file + a
registry entry. No harness changes needed.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
RUBRICS_DIR = REPO_ROOT / "config" / "rubrics"


@dataclass(frozen=True)
class Candidate:
    """One model prediction queued for judging."""

    record_id: str
    evaluator_id: str
    sub_type: str
    payload: dict[str, Any]
    confidence: float
    marc_context: dict[str, str] = field(default_factory=dict)


@dataclass
class Verdict:
    """Structured verdict matching ``config/schemas/verdict.v1.json``."""

    record_id: str
    evaluator_id: str
    sub_type: str
    candidate_payload: dict[str, Any]
    confidence: float
    name_ok: str = "no"
    type_ok: str = "no"
    role_ok: str = "n/a"
    overall: str = "fail"
    reasoning: str = ""
    error: str | None = None
    judge_id: str = ""
    cache_key: str = ""
    judged_at: str = ""

    def to_jsonl_record(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "judge_id": self.judge_id,
            "record_id": self.record_id,
            "evaluator_id": self.evaluator_id,
            "sub_type": self.sub_type,
            "candidate": self.candidate_payload,
            "confidence": self.confidence,
            "verdict": {
                "name_ok": self.name_ok,
                "type_ok": self.type_ok,
                "role_ok": self.role_ok,
                "overall": self.overall,
                "reasoning": self.reasoning,
            },
            "cache_key": self.cache_key,
            "judged_at": self.judged_at or datetime.now(timezone.utc).isoformat(),
            "error": self.error,
        }


class Evaluator(ABC):
    """Subclass for each (stage, model) being evaluated."""

    id: str = ""
    sub_types: list[str] = []
    marc_field_keys: list[str] = []
    rubric_name: str = ""

    # ── Abstract surface ──────────────────────────────────────────────

    @abstractmethod
    def extract_candidates(
        self,
        *,
        ner_record: dict[str, Any],
        marc_record: dict[str, Any],
        threshold: float,
    ) -> Iterable[Candidate]:
        ...

    @abstractmethod
    def build_prompt(self, candidate: Candidate) -> str:
        ...

    def parse_verdict(self, raw: dict[str, Any] | None, candidate: Candidate) -> Verdict:
        """Map a Gemini response (or None) into a structured Verdict."""
        v = Verdict(
            record_id=candidate.record_id,
            evaluator_id=candidate.evaluator_id,
            sub_type=candidate.sub_type,
            candidate_payload=dict(candidate.payload),
            confidence=candidate.confidence,
        )
        if raw is None:
            v.error = "no verdict (judge failure)"
            return v
        v.name_ok = str(raw.get("name_ok", "no"))
        v.type_ok = str(raw.get("type_ok", "no"))
        v.role_ok = str(raw.get("role_ok", "n/a"))
        v.overall = str(raw.get("overall", "fail"))
        v.reasoning = str(raw.get("reasoning", ""))
        return v

    # ── Shared helpers ────────────────────────────────────────────────

    def rubric_text(self) -> str:
        path = RUBRICS_DIR / self.rubric_name
        return path.read_text(encoding="utf-8")

    def format_marc(self, marc: dict[str, str]) -> str:
        if not marc:
            return "  (no relevant MARC fields present)"
        return "\n".join(f"  {k}: {v}" for k, v in sorted(marc.items()))

    def render_prompt(
        self,
        candidate: Candidate,
        *,
        prediction_block: str,
    ) -> str:
        """Compose rubric + per-candidate prediction + MARC context."""
        return (
            self.rubric_text()
            + "\n\n────────────────────────────────────────\n"
            + f"Record ID: {candidate.record_id}\n\n"
            + prediction_block
            + f"\nRelevant MARC fields for this record:\n{self.format_marc(candidate.marc_context)}\n"
            + "\nReturn only the JSON verdict."
        )

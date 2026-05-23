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
    """One model prediction queued for judging.

    Carries the pipeline's deterministic MARC-grounding signal so the
    Gemini judge sees what fields the entity's text actually appears
    in. The judge no longer has to re-derive that itself — its job is
    just to confirm the verdict given the structured evidence.

    The grounding fields are populated by the pipeline's
    ``filter_with_marc_grounding`` post-filter and read out of
    ``ner_results.json`` at extract-candidate time. When the entity
    pre-dates F8 grounding (older runs) the fields default to safe
    "unknown" values and the prompt falls back to a generic instruction.
    """

    record_id: str
    evaluator_id: str
    sub_type: str
    payload: dict[str, Any]
    confidence: float
    marc_context: dict[str, str] = field(default_factory=dict)
    # F8 MARC-grounding signal — every entity in ner_results.json now
    # carries these. ``grounded`` is the strict role-mapped check;
    # ``exists_in`` lists every MARC field where the text appears;
    # ``grounded_field`` is the specific field that satisfied the
    # strict gate (or None).
    grounded: bool | None = None
    grounded_field: str | None = None
    exists_in: list[dict[str, Any]] = field(default_factory=list)
    # MARC fields the entity's role/type implies — duplicated from
    # the pipeline's role/type map so the prompt can frame the
    # judge's question as "is the match in <these> fields?"
    role_fields: list[str] = field(default_factory=list)


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

    def format_grounding(self, candidate: Candidate) -> str:
        """Render the F8 grounding signal as a prompt-readable block.

        Three cases, each unambiguous to the judge:

        * ``grounded=True``  — pipeline confirmed a role-mapped match.
        * ``grounded=False`` + ``exists_in`` non-empty — name is in
          MARC but in the wrong field; the role is probably wrong.
        * ``grounded=False`` + ``exists_in`` empty — discovery: the
          model found something the structured fields don't index.

        When the candidate predates F8 (``grounded is None``), the
        block politely says "no grounding signal available" and the
        judge falls back to its standalone reasoning.
        """
        if candidate.grounded is None and not candidate.exists_in:
            return "  (no MARC-grounding signal available for this candidate)"

        lines: list[str] = []
        role_fields_str = ", ".join(candidate.role_fields) if candidate.role_fields else "(unknown)"
        if candidate.grounded is True:
            lines.append(
                "  STATE = ROLE-GROUNDED — the pipeline's deterministic post-filter"
            )
            lines.append(
                f"  confirms the predicted text appears in the role-mapped MARC field"
            )
            lines.append(
                f"  for this prediction. Role-mapped fields: {role_fields_str}."
            )
            lines.append(
                f"  Matched in: {candidate.grounded_field or '(unspecified)'}."
            )
            lines.append(
                "  This is the STRONGEST positive signal — unless the predicted text"
            )
            lines.append(
                "  itself is wrong (typo, mis-segmentation), agree with the model."
            )
        elif candidate.grounded is False and candidate.exists_in:
            lines.append(
                "  STATE = WRONG-FIELD — the predicted text appears in MARC, but"
            )
            lines.append(
                f"  NOT in the role-mapped field(s) ({role_fields_str})."
            )
            lines.append(
                "  This is a STRONG negative signal: the entity is most likely a"
            )
            lines.append(
                "  wrong-role prediction. The correct role is implied by the field"
            )
            lines.append(
                "  where the name DID appear. Set role_ok = 'no' unless an"
            )
            lines.append(
                "  alternative reading is clearly supported by the cited substring."
            )
        else:
            lines.append(
                "  STATE = DISCOVERY — the predicted text was NOT found in any"
            )
            lines.append(
                "  structured MARC field. Either the NER model is enriching the"
            )
            lines.append(
                "  catalog with information that was never indexed, OR the model"
            )
            lines.append(
                "  hallucinated. Be conservative: set name_ok = 'no' unless the"
            )
            lines.append(
                "  predicted text is clearly visible in a free-text note (and you"
            )
            lines.append(
                "  can quote it from the MARC context block above)."
            )

        if candidate.exists_in:
            lines.append("")
            lines.append("  Pipeline-detected matches (every MARC field where the text appears):")
            for row in candidate.exists_in[:8]:
                mt = row.get("match_type", "?")
                field = row.get("field", "?")
                value = str(row.get("value") or "")
                if len(value) > 80:
                    value = value[:77] + "..."
                marker = "●" if mt == "full" else "○"
                lines.append(f"    {marker} {field}  ({mt})  → \"{value}\"")
            if len(candidate.exists_in) > 8:
                lines.append(f"    … +{len(candidate.exists_in) - 8} more")
        return "\n".join(lines)

    def render_prompt(
        self,
        candidate: Candidate,
        *,
        prediction_block: str,
    ) -> str:
        """Compose rubric + per-candidate prediction + MARC context +
        the deterministic grounding signal the pipeline pre-computed.

        The grounding block ANCHORS the judge: instead of re-running
        the "is this name in MARC?" search from scratch (which Gemini
        does inconsistently), the judge gets the answer up-front and
        only has to reason about *whether the deterministic finding
        agrees with the prediction*. Reduces judge variance dramatically.
        """
        return (
            self.rubric_text()
            + "\n\n────────────────────────────────────────\n"
            + f"Record ID: {candidate.record_id}\n\n"
            + prediction_block
            + f"\nRelevant MARC fields for this record:\n"
            + f"{self.format_marc(candidate.marc_context)}\n"
            + "\nDeterministic MARC-grounding signal (from the pipeline's "
              "F8 post-filter — trust this over your own search):\n"
            + f"{self.format_grounding(candidate)}\n"
            + "\nReturn only the JSON verdict."
        )

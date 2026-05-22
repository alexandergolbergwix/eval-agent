"""MARC 500 Colophon Classifier (binary) evaluator."""

from __future__ import annotations

from typing import Any, Iterable

from eval_agent.evaluators._base import Candidate, Evaluator
from eval_agent.ingest import marc_extract, ner_results


class Marc500ColophonEvaluator(Evaluator):
    id = "marc500_colophon"
    sub_types = ["COLOPHON"]
    rubric_name = "marc500_colophon.md"
    marc_field_keys = [
        "title", "notes", "provenance", "colophon_text", "data_from_colophon",
    ]

    def extract_candidates(
        self,
        *,
        ner_record: dict[str, Any],
        marc_record: dict[str, Any],
        threshold: float,
    ) -> Iterable[Candidate]:
        rid = str(ner_record.get("_control_number", ""))
        ctx = marc_extract.project(marc_record, self.marc_field_keys)
        for c in ner_results.get_ml_colophon_sentences(ner_record):
            conf = float(c.get("confidence", 1.0))
            if conf < threshold:
                continue
            yield Candidate(
                record_id=rid,
                evaluator_id=self.id,
                sub_type="COLOPHON",
                payload=dict(c),
                confidence=conf,
                marc_context=ctx,
            )

    def build_prompt(self, candidate: Candidate) -> str:
        p = candidate.payload
        block = (
            f"Model: MARC 500 Colophon Classifier (binary)\n"
            f"Prediction:\n"
            f"  sentence:    {p.get('sentence', '')}\n"
            f"  classified-as: COLOPHON (above per-fold threshold)\n"
            f"  confidence:  {candidate.confidence:.3f}\n"
        )
        return self.render_prompt(candidate, prediction_block=block)

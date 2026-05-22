"""Person NER (Joint name + role) evaluator."""

from __future__ import annotations

from typing import Any, Iterable

from eval_agent.evaluators._base import Candidate, Evaluator
from eval_agent.ingest import marc_extract, ner_results


class PersonNERevaluator(Evaluator):
    id = "person_ner"
    sub_types = ["AUTHOR", "TRANSCRIBER", "TRANSLATOR", "COMMENTATOR",
                 "OWNER", "EDITOR", "CENSOR"]
    rubric_name = "person_ner.md"
    marc_field_keys = [
        "title", "authors", "contributors", "provenance", "notes",
        "colophon_text", "data_from_colophon",
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
        for ent in ner_results.get_entities(ner_record, "person_ner"):
            conf = ner_results.get_confidence(ent)
            if conf < threshold:
                continue
            yield Candidate(
                record_id=rid,
                evaluator_id=self.id,
                sub_type=str(ent.get("role", "")),
                payload=dict(ent),
                confidence=conf,
                marc_context=ctx,
            )

    def build_prompt(self, candidate: Candidate) -> str:
        p = candidate.payload
        block = (
            f"Model: Person NER (Joint name + role)\n"
            f"Prediction:\n"
            f"  text:        {p.get('person', p.get('text', ''))}\n"
            f"  role:        {p.get('role', 'n/a')}\n"
            f"  confidence:  {candidate.confidence:.3f}\n"
        )
        return self.render_prompt(candidate, prediction_block=block)

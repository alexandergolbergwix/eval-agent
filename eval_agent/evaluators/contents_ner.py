"""Contents NER (WORK / FOLIO / WORK_AUTHOR) evaluator."""

from __future__ import annotations

from typing import Any, Iterable

from eval_agent.evaluators._base import Candidate, Evaluator
from eval_agent.ingest import marc_extract, ner_results

# Pipeline's type → MARC field map (mirrors
# ``converter.authority.ner_post_filters._CONTENTS_TYPE_TO_MARC_FIELDS``).
_TYPE_TO_FIELDS: dict[str, tuple[str, ...]] = {
    "WORK":        ("contents", "notes", "canonical_references",
                    "colophon_text"),
    "FOLIO":       ("contents", "notes"),
    "WORK_AUTHOR": ("contents", "notes", "canonical_references"),
}


class ContentsNERevaluator(Evaluator):
    id = "contents_ner"
    sub_types = ["WORK", "FOLIO", "WORK_AUTHOR"]
    rubric_name = "contents_ner.md"
    marc_field_keys = [
        "title", "variant_titles", "contents", "notes", "colophon_text",
        "canonical_references", "related_works",
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
        for ent in ner_results.get_entities(ner_record, "contents_ner"):
            conf = ner_results.get_confidence(ent)
            if conf < threshold:
                continue
            etype = str(ent.get("type", "")).upper()
            yield Candidate(
                record_id=rid,
                evaluator_id=self.id,
                sub_type=etype,
                payload=dict(ent),
                confidence=conf,
                marc_context=ctx,
                grounded=ent.get("grounded") if isinstance(ent.get("grounded"), bool) else None,
                grounded_field=ent.get("grounded_field"),
                exists_in=list(ent.get("exists_in") or []),
                role_fields=list(_TYPE_TO_FIELDS.get(etype, ("contents", "notes"))),
            )

    def build_prompt(self, candidate: Candidate) -> str:
        p = candidate.payload
        block = (
            f"Model: Contents NER (WORK / FOLIO / WORK_AUTHOR)\n"
            f"Prediction:\n"
            f"  text:        {p.get('text', '')}\n"
            f"  type:        {p.get('type', '')}\n"
            f"  confidence:  {candidate.confidence:.3f}\n"
        )
        return self.render_prompt(candidate, prediction_block=block)

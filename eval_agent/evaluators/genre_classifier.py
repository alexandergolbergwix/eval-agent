"""Genre Classifier (multi-label) evaluator."""

from __future__ import annotations

from typing import Any, Iterable

from eval_agent.evaluators._base import Candidate, Evaluator
from eval_agent.ingest import marc_extract, ner_results


class GenreClassifierEvaluator(Evaluator):
    id = "genre_classifier"
    sub_types = [
        "Piyyutim", "Poetry", "Illustrated works (Manuscript)",
        "Personal correspondence", "Censored manuscripts",
        "Autograph manuscripts", "Records (Documents)", "Bibliographies",
    ]
    rubric_name = "genre_classifier.md"
    marc_field_keys = [
        "title", "variant_titles", "notes", "genres", "subjects",
        "is_anthology", "has_decoration",
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
        for g in ner_results.get_ml_genres(ner_record):
            conf = float(g.get("confidence", 0.0))
            if conf < threshold:
                continue
            label = str(g.get("label") or g.get("genre") or "")
            yield Candidate(
                record_id=rid,
                evaluator_id=self.id,
                sub_type=label,
                payload=dict(g),
                confidence=conf,
                marc_context=ctx,
            )

    def build_prompt(self, candidate: Candidate) -> str:
        p = candidate.payload
        block = (
            f"Model: Genre Classifier (multi-label)\n"
            f"Prediction:\n"
            f"  genre:       {p.get('label', p.get('genre', ''))}\n"
            f"  confidence:  {candidate.confidence:.3f}\n"
            f"Notes for the judge: the 'genres' field below is gold "
            f"MARC 655 (when present). Empty 'genres' field does NOT "
            f"automatically mean the prediction is wrong — this model "
            f"is designed to fill the 31% of records missing MARC 655.\n"
        )
        return self.render_prompt(candidate, prediction_block=block)

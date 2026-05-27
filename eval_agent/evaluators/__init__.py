"""Registry of all known evaluators.

Each entry maps the evaluator's canonical ``id`` to its class. Adding
a new evaluator is one line here + one module under this directory.
"""

from eval_agent.evaluators._base import Candidate, Evaluator, Verdict
from eval_agent.evaluators.authority import AuthorityEvaluator
from eval_agent.evaluators.contents_ner import ContentsNERevaluator
from eval_agent.evaluators.genre_classifier import GenreClassifierEvaluator
from eval_agent.evaluators.person_ner import PersonNERevaluator
from eval_agent.evaluators.provenance_ner import ProvenanceNERevaluator

# ``marc500_colophon`` was removed 2026-05-23 after the conf>=0.90
# eval-agent run showed 76% of its high-confidence predictions failing
# (6% strict / 16% lenient precision overall). The classifier head
# was too noisy to ship; weights are kept on disk locally for future
# revisits but are no longer bundled, surfaced, or evaluated.

REGISTRY: dict[str, type[Evaluator]] = {
    "person_ner": PersonNERevaluator,
    "provenance_ner": ProvenanceNERevaluator,
    "contents_ner": ContentsNERevaluator,
    "genre_classifier": GenreClassifierEvaluator,
    # Stage-3 authority resolution (Mazal / VIAF / Wikidata / KIMA).
    # Reads authority_enriched.json, not ner_results.json.
    "authority": AuthorityEvaluator,
}

# Evaluators that read authority_enriched.json instead of ner_results.json.
AUTHORITY_EVALUATORS: frozenset[str] = frozenset({"authority"})


def build(evaluator_id: str) -> Evaluator:
    cls = REGISTRY.get(evaluator_id)
    if cls is None:
        raise KeyError(
            f"unknown evaluator: {evaluator_id!r} "
            f"(known: {sorted(REGISTRY)})"
        )
    return cls()


def all_evaluators() -> list[Evaluator]:
    return [cls() for cls in REGISTRY.values()]


__all__ = [
    "Candidate", "Evaluator", "Verdict", "REGISTRY", "build", "all_evaluators",
]

"""Registry of all known evaluators.

Each entry maps the evaluator's canonical ``id`` to its class. Adding
a new evaluator is one line here + one module under this directory.
"""

from eval_agent.evaluators._base import Candidate, Evaluator, Verdict
from eval_agent.evaluators.contents_ner import ContentsNERevaluator
from eval_agent.evaluators.genre_classifier import GenreClassifierEvaluator
from eval_agent.evaluators.marc500_colophon import Marc500ColophonEvaluator
from eval_agent.evaluators.person_ner import PersonNERevaluator
from eval_agent.evaluators.provenance_ner import ProvenanceNERevaluator

REGISTRY: dict[str, type[Evaluator]] = {
    "person_ner": PersonNERevaluator,
    "provenance_ner": ProvenanceNERevaluator,
    "contents_ner": ContentsNERevaluator,
    "genre_classifier": GenreClassifierEvaluator,
    "marc500_colophon": Marc500ColophonEvaluator,
}


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

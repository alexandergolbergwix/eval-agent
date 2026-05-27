"""Tests for the authority (Stage-3) evaluator + ingest + discover."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_agent.evaluators import REGISTRY, build
from eval_agent.evaluators.authority import AuthorityEvaluator
from eval_agent.ingest import authority_results, pipeline_run


def _authority_record() -> dict:
    return {
        "_control_number": "990001",
        "title": "ספר התשובות",
        "authors": ["קארו, יוסף בן אפרים"],
        "marc_authority_matches": [
            {
                "name": "קארו, יוסף בן אפרים",
                "role": "author",
                "field": "100/110/111",
                "mazal_id": "987007",
                "viaf_uri": "https://viaf.org/viaf/179101",
                "wikidata_qid": "Q179101",
                "confidence": "high",
                "source": "cross_source",
                "sources": ["mazal", "viaf"],
                "source_count": 2,
                "matched": 1,
            },
            {
                "name": "צפת",
                "role": "subject",
                "field": "651",
                "match_type": "place",
                "wikidata_qid": "Q204193",
                "confidence": "low",
                "matched": 1,
            },
        ],
        "kima_places": {"Jerusalem (Israel)": "https://www.wikidata.org/entity/Q1218"},
    }


class TestRegistry:
    def test_authority_registered(self) -> None:
        assert "authority" in REGISTRY
        assert isinstance(build("authority"), AuthorityEvaluator)


class TestIngest:
    def test_get_matches(self) -> None:
        rec = _authority_record()
        assert len(authority_results.get_matches(rec)) == 2

    def test_confidence_enum_mapping(self) -> None:
        assert authority_results.get_confidence({"confidence": "high"}) == 0.9
        assert authority_results.get_confidence({"confidence": "low"}) == 0.3
        # numeric passthrough
        assert authority_results.get_confidence({"confidence": 0.75}) == 0.75
        # fallback to matched flag
        assert authority_results.get_confidence({"matched": 1}) == 0.85
        assert authority_results.get_confidence({"matched": 0}) == 0.0

    def test_get_places(self) -> None:
        rec = _authority_record()
        assert "Jerusalem (Israel)" in authority_results.get_places(rec)


class TestExtractCandidates:
    def test_judges_all_resolved_matches_regardless_of_threshold(self) -> None:
        # Authority verification reviews every resolved authority decision
        # — the threshold does NOT gate (the uncertain medium/low matches
        # are exactly what a curator wants a second opinion on). The
        # fixture has 2 resolved MARC matches (person + place) + 1 KIMA
        # place = 3 candidates, even at a high threshold.
        ev = AuthorityEvaluator()
        cands = list(ev.extract_candidates(
            ner_record=_authority_record(), marc_record={}, threshold=0.9,
        ))
        assert len(cands) == 3
        assert {c.sub_type for c in cands} == {"person", "place"}
        person = next(c for c in cands if c.sub_type == "person")
        assert person.evaluator_id == "authority"
        assert person.record_id == "990001"
        assert person.payload["mazal_id"] == "987007"

    def test_skips_unmatched_rows(self) -> None:
        rec = {
            "_control_number": "990002",
            "marc_authority_matches": [
                {"name": "פלוני", "role": "author", "field": "100",
                 "confidence": "low", "matched": 0},  # no id → skipped
            ],
        }
        ev = AuthorityEvaluator()
        cands = list(ev.extract_candidates(
            ner_record=rec, marc_record={}, threshold=0.0,
        ))
        assert cands == []

    def test_includes_enriched_ner_entities(self) -> None:
        rec = {
            "_control_number": "990003",
            "entities": [
                {"person": "משה", "role": "TRANSLATOR", "source": "person_ner",
                 "wikidata_qid": "Q9077", "model_confidence": 0.83},
                {"person": "unmatched", "role": "AUTHOR", "source": "person_ner"},
            ],
        }
        ev = AuthorityEvaluator()
        cands = list(ev.extract_candidates(
            ner_record=rec, marc_record={}, threshold=0.9,
        ))
        assert len(cands) == 1
        assert cands[0].payload["name"] == "משה"
        assert cands[0].payload["wikidata_qid"] == "Q9077"

    def test_build_prompt_mentions_authority_ids(self) -> None:
        ev = AuthorityEvaluator()
        cand = next(iter(ev.extract_candidates(
            ner_record=_authority_record(), marc_record={}, threshold=0.5,
        )))
        prompt = ev.build_prompt(cand)
        assert "Mazal 987007" in prompt
        assert "VIAF" in prompt
        assert "קארו" in prompt


class TestDiscover:
    def _write(self, d: Path, *, marc: bool, ner: bool, authority: bool) -> None:
        if marc:
            (d / "marc_extracted.json").write_text("[]")
        if ner:
            (d / "ner_results.json").write_text("[]")
        if authority:
            (d / "authority_enriched.json").write_text("[]")

    def test_authority_only_dir_discovers(self, tmp_path: Path) -> None:
        self._write(tmp_path, marc=True, ner=False, authority=True)
        run = pipeline_run.discover(tmp_path)
        assert run.authority_results is not None
        assert run.ner_results is None

    def test_ner_only_dir_still_discovers(self, tmp_path: Path) -> None:
        self._write(tmp_path, marc=True, ner=True, authority=False)
        run = pipeline_run.discover(tmp_path)
        assert run.ner_results is not None
        assert run.authority_results is None

    def test_both_present(self, tmp_path: Path) -> None:
        self._write(tmp_path, marc=True, ner=True, authority=True)
        run = pipeline_run.discover(tmp_path)
        assert run.ner_results is not None and run.authority_results is not None

    def test_neither_raises(self, tmp_path: Path) -> None:
        self._write(tmp_path, marc=True, ner=False, authority=False)
        with pytest.raises(FileNotFoundError):
            pipeline_run.discover(tmp_path)

    def test_missing_marc_raises(self, tmp_path: Path) -> None:
        self._write(tmp_path, marc=False, ner=False, authority=True)
        with pytest.raises(FileNotFoundError):
            pipeline_run.discover(tmp_path)

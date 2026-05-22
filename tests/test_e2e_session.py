"""End-to-end Session tests with mocked Gemini.

Each test exercises the full Worker lifecycle (startup → execute →
checkpoint → finalize) against the fixture pipeline output, but
replaces the Gemini judge with a deterministic ``MockJudge``. No
network calls happen — yet every step of the agent flow is exercised.

Scenarios:
  1. Happy path: all 5 evaluators, MockJudge returns valid verdicts,
     all artefacts produced (results.jsonl, summary.csv, report.md,
     manifest.json), progress.md appended, cache populated.
  2. Dry run: candidate counts printed, judge never called, no run
     directory created.
  3. Cache hit: re-run with same judge id reuses cached verdicts,
     judge is NOT called again.
  4. Evaluator subset: --evaluators person_ner skips the other 4.
  5. Judge error: when MockJudge returns an error, the verdict is
     recorded with the error message, the run still completes.
  6. Report regeneration: ``eval-agent report --run <id>`` rebuilds
     report.md from results.jsonl without re-judging.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from eval_agent.orchestration.session import Session, SessionConfig
from tests.conftest import MockJudge


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _config(
    pipeline_output: Path,
    *,
    evaluators: list[str] | None = None,
    threshold: float = 0.85,
    dry_run: bool = False,
    judge_model: str = "mock-judge-v1",
) -> SessionConfig:
    return SessionConfig(
        pipeline_output=pipeline_output,
        threshold=threshold,
        rpm=999,
        parallel=2,
        judge_model=judge_model,
        evaluators=evaluators or [
            "person_ner", "provenance_ner", "contents_ner",
            "genre_classifier", "marc500_colophon",
        ],
        api_key="dummy-not-used-by-mock",
        dry_run=dry_run,
    )


def _run_session(
    config: SessionConfig,
    judge: MockJudge,
    state_paths: dict[str, Path],
) -> tuple[Session, list[Any], Path | None]:
    session = Session(config, judge=judge, **state_paths)
    session.startup()
    verdicts = session.execute()
    run_dir: Path | None = None
    if verdicts:
        run_dir = session.checkpoint(verdicts)
        session.finalize()
    return session, verdicts, run_dir


# ──────────────────────────────────────────────────────────────────────────────
# Scenario 1 — happy path
# ──────────────────────────────────────────────────────────────────────────────


def test_e2e_happy_path_all_evaluators(
    pipeline_output: Path, mock_judge: MockJudge, state_paths: dict[str, Path],
) -> None:
    config = _config(pipeline_output)
    session, verdicts, run_dir = _run_session(config, mock_judge, state_paths)

    # 5 person_ner candidates ≥ 0.85? Fixture has 2 (yosef + chezkia at conf=0.85)
    # 2 provenance, 2 contents (FOLIO+WORK), 1 work_author at 0.88, 3 ml_genres ≥ 0.85
    # (Piyyutim 0.92, Autograph 0.87, Illustrated 0.88), 2 ml_colophon sentences.
    # Let the actual count come from the fixture — assert structure not exact n.
    assert verdicts, "expected at least some verdicts"
    assert run_dir is not None

    # 1. Judge was called once per cache miss
    assert len(mock_judge.calls) == len(verdicts)

    # 2. Run directory has all 4 artefacts
    assert (run_dir / "results.jsonl").is_file()
    assert (run_dir / "summary.csv").is_file()
    assert (run_dir / "report.md").is_file()
    assert (run_dir / "manifest.json").is_file()

    # 3. results.jsonl has one row per verdict, all schema-shaped
    lines = (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(verdicts)
    for line in lines:
        rec = json.loads(line)
        assert rec["schema_version"] == 1
        assert rec["judge_id"] == "mock-judge-v1"
        assert rec["evaluator_id"] in {
            "person_ner", "provenance_ner", "contents_ner",
            "genre_classifier", "marc500_colophon",
        }
        for k in ("name_ok", "type_ok", "role_ok", "overall", "reasoning"):
            assert k in rec["verdict"]
        assert rec["cache_key"]

    # 4. manifest.json captures run config + stats
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["config"]["judge_model"] == "mock-judge-v1"
    assert manifest["stats"]["candidates_total"] == len(verdicts)

    # 5. progress.md was appended
    assert state_paths["progress_path"].is_file()
    prog = state_paths["progress_path"].read_text(encoding="utf-8")
    assert session._run_id in prog
    assert "mock-judge-v1" in prog

    # 6. Cache file was created + has one row per verdict
    cache_lines = state_paths["cache_path"].read_text(encoding="utf-8").splitlines()
    assert len(cache_lines) == len(verdicts)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario 2 — dry run never calls the judge
# ──────────────────────────────────────────────────────────────────────────────


def test_e2e_dry_run_no_judge_calls(
    pipeline_output: Path, mock_judge: MockJudge, state_paths: dict[str, Path],
) -> None:
    config = _config(pipeline_output, dry_run=True)
    session, verdicts, run_dir = _run_session(config, mock_judge, state_paths)

    assert verdicts == []
    assert run_dir is None
    assert len(mock_judge.calls) == 0
    # No run directory should have been created
    assert not (state_paths["runs_dir"] / session._run_id).exists()


# ──────────────────────────────────────────────────────────────────────────────
# Scenario 3 — cache hit on second run
# ──────────────────────────────────────────────────────────────────────────────


def test_e2e_cache_hit_skips_judge(
    pipeline_output: Path, state_paths: dict[str, Path],
) -> None:
    # First run populates the cache
    first_judge = MockJudge(strategy="always_full")
    config = _config(pipeline_output)
    _, first_verdicts, _ = _run_session(config, first_judge, state_paths)
    n = len(first_verdicts)
    assert n > 0
    assert len(first_judge.calls) == n

    # Second run with a fresh MockJudge but the SAME judge id + cache.
    # Cache key is sha256(judge_id || prompt); identical → 0 calls.
    second_judge = MockJudge(strategy="always_fail")  # would fail if reached
    config2 = _config(pipeline_output)  # same model id
    _, second_verdicts, run_dir2 = _run_session(config2, second_judge, state_paths)

    assert len(second_verdicts) == n
    assert len(second_judge.calls) == 0, "cache should have absorbed all calls"
    # Reused verdicts must match the first-run "always_full" payload
    for v in second_verdicts:
        assert v.overall == "full", "cached first-run verdict should survive"


# ──────────────────────────────────────────────────────────────────────────────
# Scenario 4 — evaluator subset
# ──────────────────────────────────────────────────────────────────────────────


def test_e2e_evaluator_subset_only_runs_selected(
    pipeline_output: Path, mock_judge: MockJudge, state_paths: dict[str, Path],
) -> None:
    config = _config(pipeline_output, evaluators=["person_ner"])
    _, verdicts, run_dir = _run_session(config, mock_judge, state_paths)

    assert verdicts, "expected person_ner candidates above 0.85 in fixture"
    # Every verdict must belong to person_ner
    for v in verdicts:
        assert v.evaluator_id == "person_ner"

    # The other 4 evaluators contribute zero rows to summary.csv
    csv_text = (run_dir / "summary.csv").read_text(encoding="utf-8")
    for other in ("provenance_ner", "contents_ner", "genre_classifier", "marc500_colophon"):
        assert other not in csv_text


# ──────────────────────────────────────────────────────────────────────────────
# Scenario 5 — judge error path
# ──────────────────────────────────────────────────────────────────────────────


def test_e2e_judge_error_recorded_run_completes(
    pipeline_output: Path, state_paths: dict[str, Path],
) -> None:
    # Inject error on every prompt that mentions "Provenance NER"
    failing_judge = MockJudge(
        strategy="always_full",
        error_on_substring="Provenance NER",
    )
    config = _config(pipeline_output, evaluators=["person_ner", "provenance_ner"])
    _, verdicts, run_dir = _run_session(config, failing_judge, state_paths)

    assert run_dir is not None
    error_verdicts = [v for v in verdicts if v.error]
    ok_verdicts = [v for v in verdicts if not v.error]

    # provenance_ner verdicts should all be errors; person_ner should all be OK
    assert error_verdicts, "expected at least one error verdict from provenance_ner"
    for v in error_verdicts:
        assert v.evaluator_id == "provenance_ner"
        assert "INJECTED_ERROR" in (v.error or "")
    for v in ok_verdicts:
        assert v.evaluator_id == "person_ner"
        assert v.overall == "full"

    # results.jsonl preserves the error
    lines = (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
    errored_rows = [json.loads(l) for l in lines if json.loads(l).get("error")]
    assert errored_rows
    for row in errored_rows:
        assert row["evaluator_id"] == "provenance_ner"


# ──────────────────────────────────────────────────────────────────────────────
# Scenario 6 — report regeneration reads results.jsonl
# ──────────────────────────────────────────────────────────────────────────────


def test_e2e_report_regeneration(
    pipeline_output: Path, mock_judge: MockJudge, state_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(pipeline_output)
    session, verdicts, run_dir = _run_session(config, mock_judge, state_paths)
    assert run_dir is not None
    original_md = (run_dir / "report.md").read_text(encoding="utf-8")

    # Delete report.md and have the CLI report subcommand recreate it.
    # `_cmd_report` reads from the canonical STATE_DIR, so we monkeypatch.
    (run_dir / "report.md").unlink()

    from eval_agent import cli
    monkeypatch.setattr(cli, "STATE_DIR", state_paths["runs_dir"].parent)
    args = argparse.Namespace(cmd="report", run=session._run_id)
    rc = cli._cmd_report(args)
    assert rc == 0

    rebuilt = (run_dir / "report.md").read_text(encoding="utf-8")
    # Headers + metric table should still be present
    assert "Per (evaluator, sub-type) precision" in rebuilt
    assert "mock-judge-v1" in rebuilt
    # The "(regenerated)" marker should be in the title
    assert "regenerated" in rebuilt

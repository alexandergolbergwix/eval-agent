"""Phase 2 e2e tests — TDD.

These tests describe the four Phase 2 harness primitives BEFORE the
implementations exist. Running this file should produce FAILING tests
which become passing once Phase 2 ships:

  1. ``eval-agent verify`` — real cache + schema + fixtures integrity
     (not just the trivial schema-validates check in Phase 0).
  2. ``eval-agent diff`` — cross-run precision-regression detector.
  3. ``eval-agent recover`` — rebuild the verdict cache and state
     scaffolding from existing ``state/runs/*/results.jsonl``.
  4. ``SelfVerifier`` — the 5% re-judge consistency loop, plus the
     ``Session.run_self_verify`` integration that triggers it and
     writes ``self_verify.json`` into the run directory.

Each test uses MockJudge / fixture pipeline output so the suite stays
hermetic (no network). The mocks are intentionally minimal — the
tests are documentation for the Phase 2 module contracts.
"""

from __future__ import annotations

import json
from dataclasses import is_dataclass
from pathlib import Path
from typing import Any

import pytest

from eval_agent.orchestration.session import Session, SessionConfig
from tests.conftest import MockJudge


# ──────────────────────────────────────────────────────────────────────────────
# Helpers shared with the Phase 1 e2e suite
# ──────────────────────────────────────────────────────────────────────────────


def _config(
    pipeline_output: Path,
    *,
    evaluators: list[str] | None = None,
    judge_model: str = "mock-judge-v1",
) -> SessionConfig:
    return SessionConfig(
        pipeline_output=pipeline_output,
        threshold=0.85,
        rpm=999,
        parallel=2,
        judge_model=judge_model,
        evaluators=evaluators or [
            "person_ner", "provenance_ner", "contents_ner",
            "genre_classifier", "marc500_colophon",
        ],
        api_key="dummy",
        dry_run=False,
    )


def _seed_one_run(
    pipeline_output: Path,
    state_paths: dict[str, Path],
    *,
    judge: MockJudge,
) -> tuple[Session, Path]:
    """Run a single session end-to-end and return (session, run_dir)."""
    session = Session(_config(pipeline_output), judge=judge, **state_paths)
    session.startup()
    verdicts = session.execute()
    run_dir = session.checkpoint(verdicts)
    session.finalize()
    return session, run_dir


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 · Test group 1 — verify command (cache + schemas + fixtures)
# ══════════════════════════════════════════════════════════════════════════════


class TestPhase2Verify:
    """``eval-agent verify`` should be a real session-startup gate, not
    just a schema syntactic check.

    Contract (Phase 2):
        - exit 0 if cache (each row parses + verdict matches schema),
          schema file is valid, and fixture pipeline output parses.
        - exit non-zero with a clear error message otherwise.
        - structured result is also exposed as
          ``eval_agent.orchestration.verify.run_verify()`` returning
          a ``VerifyReport`` for programmatic callers.
    """

    def test_verify_module_exists(self) -> None:
        """`eval_agent.orchestration.verify` should be importable."""
        from eval_agent.orchestration import verify  # noqa: F401

    def test_verify_passes_on_clean_state(
        self, pipeline_output: Path, mock_judge: MockJudge,
        state_paths: dict[str, Path],
    ) -> None:
        _seed_one_run(pipeline_output, state_paths, judge=mock_judge)
        from eval_agent.orchestration import verify
        report = verify.run_verify(
            cache_path=state_paths["cache_path"],
            schemas_dir=Path(__file__).resolve().parents[1] / "config" / "schemas",
        )
        assert report.passed, f"expected pass, got failures: {report.failures}"
        assert report.cache_rows_checked > 0

    def test_verify_fails_on_corrupt_cache_line(
        self, pipeline_output: Path, mock_judge: MockJudge,
        state_paths: dict[str, Path],
    ) -> None:
        _seed_one_run(pipeline_output, state_paths, judge=mock_judge)
        # Inject a malformed line
        with state_paths["cache_path"].open("a", encoding="utf-8") as f:
            f.write("{this is not valid json\n")
        from eval_agent.orchestration import verify
        report = verify.run_verify(
            cache_path=state_paths["cache_path"],
            schemas_dir=Path(__file__).resolve().parents[1] / "config" / "schemas",
        )
        assert not report.passed
        assert any("malformed" in f.lower() or "parse" in f.lower() for f in report.failures)

    def test_verify_fails_on_invalid_verdict_against_schema(
        self, pipeline_output: Path, mock_judge: MockJudge,
        state_paths: dict[str, Path],
    ) -> None:
        _seed_one_run(pipeline_output, state_paths, judge=mock_judge)
        # Inject an entry whose verdict.name_ok is not in the enum
        bad = {
            "key": "0" * 64,
            "judge_id": "mock-judge-v1",
            "verdict": {
                "name_ok": "SOMETHING_INVALID",
                "type_ok": "yes",
                "role_ok": "n/a",
                "overall": "fail",
                "reasoning": "schema violation injection",
            },
        }
        with state_paths["cache_path"].open("a", encoding="utf-8") as f:
            f.write(json.dumps(bad) + "\n")
        from eval_agent.orchestration import verify
        report = verify.run_verify(
            cache_path=state_paths["cache_path"],
            schemas_dir=Path(__file__).resolve().parents[1] / "config" / "schemas",
        )
        assert not report.passed
        assert any("schema" in f.lower() for f in report.failures)

    def test_verify_cli_exits_nonzero_on_failure(
        self, pipeline_output: Path, mock_judge: MockJudge,
        state_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_one_run(pipeline_output, state_paths, judge=mock_judge)
        with state_paths["cache_path"].open("a", encoding="utf-8") as f:
            f.write("not-json\n")

        # CLI verify must check the cache too — point it at our tmp dirs
        from eval_agent import cli
        from eval_agent.orchestration import session as session_mod
        monkeypatch.setattr(cli, "STATE_DIR", state_paths["cache_path"].parent.parent)
        monkeypatch.setattr(session_mod, "CACHE_PATH", state_paths["cache_path"])

        rc = cli.main(["verify"])
        assert rc != 0


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 · Test group 2 — diff command (cross-run regression detection)
# ══════════════════════════════════════════════════════════════════════════════


class TestPhase2Diff:
    """``eval-agent diff --from <ts1> --to <ts2>`` should:

        - return a structured ``RunDiff`` listing every (evaluator,
          sub_type) feature with from/to precision + delta.
        - classify each feature as "regressed" | "improved" | "stable"
          | "new" | "gone".
        - write a Markdown report.
        - exit 1 when at least one feature regressed; 0 otherwise.

    The diff layer is the cross-run reasoning step of the memory
    hierarchy (semantic memory ↔ episodic memory comparison).
    """

    def test_diff_module_exists(self) -> None:
        from eval_agent.report import diff_runs  # noqa: F401

    def test_diff_no_changes_between_identical_runs(
        self, pipeline_output: Path, mock_judge: MockJudge,
        state_paths: dict[str, Path],
    ) -> None:
        # Two runs with identical MockJudge ⇒ cache makes second a no-op
        _, run_a = _seed_one_run(pipeline_output, state_paths, judge=mock_judge)
        # Second judge with same id ⇒ cache hits ⇒ identical verdicts
        _, run_b = _seed_one_run(
            pipeline_output, state_paths,
            judge=MockJudge(id="mock-judge-v1", strategy="always_fail"),
        )
        from eval_agent.report import diff_runs as dr
        d = dr.diff_runs(from_run_dir=run_a, to_run_dir=run_b)
        assert d.n_regressed == 0
        assert d.n_improved == 0
        for f in d.features:
            assert f.verdict in {"stable", "new", "gone"}

    def test_diff_detects_regression(
        self, pipeline_output: Path, state_paths: dict[str, Path],
    ) -> None:
        # First run: all full (high precision)
        _, run_a = _seed_one_run(
            pipeline_output, state_paths,
            judge=MockJudge(id="judge-a", strategy="always_full"),
        )
        # Second run: change judge id so cache misses, all fail
        _, run_b = _seed_one_run(
            pipeline_output, state_paths,
            judge=MockJudge(id="judge-b", strategy="always_fail"),
        )

        from eval_agent.report import diff_runs as dr
        d = dr.diff_runs(from_run_dir=run_a, to_run_dir=run_b)
        assert d.n_regressed > 0
        regressed = [f for f in d.features if f.verdict == "regressed"]
        assert regressed
        for f in regressed:
            assert f.delta < 0
            assert f.from_precision is not None
            assert f.to_precision is not None

    def test_diff_writes_markdown_report(
        self, pipeline_output: Path, state_paths: dict[str, Path], tmp_path: Path,
    ) -> None:
        _, run_a = _seed_one_run(
            pipeline_output, state_paths,
            judge=MockJudge(id="judge-a", strategy="always_full"),
        )
        _, run_b = _seed_one_run(
            pipeline_output, state_paths,
            judge=MockJudge(id="judge-b", strategy="by_keyword"),
        )
        from eval_agent.report import diff_runs as dr
        d = dr.diff_runs(from_run_dir=run_a, to_run_dir=run_b)
        out = tmp_path / "diff.md"
        dr.write_diff_markdown(d, out)
        text = out.read_text(encoding="utf-8")
        assert "from" in text.lower() and "to" in text.lower()
        # Should mention at least one evaluator id
        assert any(ev in text for ev in (
            "person_ner", "provenance_ner", "contents_ner",
            "genre_classifier", "marc500_colophon",
        ))

    def test_diff_cli_exit_codes(
        self, pipeline_output: Path, state_paths: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, run_a = _seed_one_run(
            pipeline_output, state_paths,
            judge=MockJudge(id="judge-a", strategy="always_full"),
        )
        _, run_b = _seed_one_run(
            pipeline_output, state_paths,
            judge=MockJudge(id="judge-b", strategy="always_fail"),
        )
        # Point CLI at our tmp runs dir
        from eval_agent import cli
        monkeypatch.setattr(cli, "STATE_DIR", state_paths["runs_dir"].parent)

        rc = cli.main(["diff", "--from", run_a.name, "--to", run_b.name])
        assert rc == 1, "regression must produce non-zero exit"

        rc_stable = cli.main(["diff", "--from", run_a.name, "--to", run_a.name])
        assert rc_stable == 0, "identical runs must exit 0"


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 · Test group 3 — recover command (rebuild state from cache + runs/)
# ══════════════════════════════════════════════════════════════════════════════


class TestPhase2Recover:
    """``eval-agent recover`` should rebuild the verdict cache from
    ``state/runs/*/results.jsonl`` when the cache file is missing or
    corrupt — without re-judging anything.

    Bootstraps ``feature_list.json`` + ``progress.md`` if they're
    missing too.
    """

    def test_recover_module_exists(self) -> None:
        from eval_agent.orchestration import recover  # noqa: F401

    def test_recover_rebuilds_cache_from_runs(
        self, pipeline_output: Path, mock_judge: MockJudge,
        state_paths: dict[str, Path],
    ) -> None:
        _seed_one_run(pipeline_output, state_paths, judge=mock_judge)
        cache_lines_before = len(
            state_paths["cache_path"].read_text(encoding="utf-8").splitlines()
        )
        # Wipe the cache; recover should rebuild it from runs/
        state_paths["cache_path"].unlink()
        assert not state_paths["cache_path"].exists()

        from eval_agent.orchestration import recover
        report = recover.recover(state_dir=state_paths["cache_path"].parent.parent)
        assert report.cache_rebuilt
        assert report.cache_entries_recovered == cache_lines_before
        assert state_paths["cache_path"].exists()
        # Every recovered cache row must have a valid SHA-256 key + judge_id
        for line in state_paths["cache_path"].read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            assert len(rec["key"]) == 64
            assert rec["judge_id"]
            assert "verdict" in rec

    def test_recover_is_idempotent(
        self, pipeline_output: Path, mock_judge: MockJudge,
        state_paths: dict[str, Path],
    ) -> None:
        _seed_one_run(pipeline_output, state_paths, judge=mock_judge)
        state_paths["cache_path"].unlink()
        from eval_agent.orchestration import recover
        r1 = recover.recover(state_dir=state_paths["cache_path"].parent.parent)
        size1 = state_paths["cache_path"].stat().st_size
        r2 = recover.recover(state_dir=state_paths["cache_path"].parent.parent)
        size2 = state_paths["cache_path"].stat().st_size
        # No-op second run: no duplicate entries, same byte count.
        assert size1 == size2
        assert r2.cache_entries_recovered == r1.cache_entries_recovered


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 · Test group 4 — self_verify (5% re-judge consistency)
# ══════════════════════════════════════════════════════════════════════════════


class TestPhase2SelfVerify:
    """The 5% re-judge sanity loop.

    Contract:
        - Samples a configurable fraction of verdicts (default 5%).
        - Re-asks the judge with a fresh cache key (e.g. by appending
          a salt to the prompt). The cache must NOT short-circuit.
        - Compares ``overall`` between the original and re-judge.
        - Returns a ``SelfVerifyResult`` with ``agreement_rate`` and
          ``passed`` (= rate >= floor, default 0.95).
        - Session.run_self_verify writes ``self_verify.json`` into the
          run directory and appends a one-liner to progress.md.
    """

    def test_self_verify_module_exists(self) -> None:
        from eval_agent.orchestration import self_verify  # noqa: F401

    def test_self_verify_result_dataclass_shape(self) -> None:
        from eval_agent.orchestration import self_verify
        # Smoke: dataclass with the expected fields exists
        cls = getattr(self_verify, "SelfVerifyResult")
        assert is_dataclass(cls)
        fields = {f for f in cls.__dataclass_fields__}
        for required in ("sample_size", "agreements", "disagreements",
                          "agreement_rate", "passed"):
            assert required in fields

    def test_self_verify_passes_when_judge_is_consistent(
        self, pipeline_output: Path, state_paths: dict[str, Path],
    ) -> None:
        """A judge that always returns the same verdict for the same
        candidate must produce 100% agreement on re-judge."""
        judge = MockJudge(id="mock-judge-v1", strategy="always_full")
        session = Session(_config(pipeline_output), judge=judge, **state_paths)
        session.startup()
        verdicts = session.execute()
        run_dir = session.checkpoint(verdicts)

        from eval_agent.orchestration.self_verify import SelfVerifier
        # Re-judge a deterministic 100% sample so we don't deal with RNG
        verifier = SelfVerifier(sample_rate=1.0, agreement_floor=0.95)
        result = verifier.run(verdicts, judge=judge, run_dir=run_dir)
        assert result.agreement_rate == 1.0
        assert result.passed is True

    def test_self_verify_fails_when_judge_flips(
        self, pipeline_output: Path, state_paths: dict[str, Path],
    ) -> None:
        """If we re-judge with a different strategy, agreement drops."""
        first = MockJudge(id="judge-flip", strategy="always_full")
        session = Session(_config(pipeline_output), judge=first, **state_paths)
        session.startup()
        verdicts = session.execute()
        run_dir = session.checkpoint(verdicts)

        # Use a DIFFERENT strategy for re-judging so disagreement is guaranteed
        flipped = MockJudge(id="judge-flip", strategy="always_fail")
        from eval_agent.orchestration.self_verify import SelfVerifier
        verifier = SelfVerifier(sample_rate=1.0, agreement_floor=0.95)
        result = verifier.run(verdicts, judge=flipped, run_dir=run_dir)
        assert result.agreement_rate < 0.5
        assert result.passed is False

    def test_self_verify_writes_artifact_into_run_dir(
        self, pipeline_output: Path, mock_judge: MockJudge,
        state_paths: dict[str, Path],
    ) -> None:
        session = Session(_config(pipeline_output), judge=mock_judge, **state_paths)
        session.startup()
        verdicts = session.execute()
        run_dir = session.checkpoint(verdicts)

        from eval_agent.orchestration.self_verify import SelfVerifier
        verifier = SelfVerifier(sample_rate=1.0)
        result = verifier.run(verdicts, judge=mock_judge, run_dir=run_dir)
        artifact = run_dir / "self_verify.json"
        assert artifact.is_file()
        rec = json.loads(artifact.read_text(encoding="utf-8"))
        assert rec["sample_size"] == result.sample_size
        assert rec["agreement_rate"] == result.agreement_rate
        assert rec["passed"] == result.passed
        assert "disagreements" in rec


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 · Test group 5 — feature_list.json status updates
# ══════════════════════════════════════════════════════════════════════════════


class TestPhase2FeatureListUpdates:
    """After every run, the Session should mutate ``feature_list.json``
    in-place to reflect the latest precision per (evaluator, sub_type):

        - bump ``attempts`` by 1
        - update ``last_run`` to the run id
        - update ``last_precision``
        - flip ``passes`` to True iff precision >= configured floor

    Hard invariant: features are NEVER removed, even if they had zero
    candidates in this run.
    """

    def test_feature_list_path_helper_exists(self) -> None:
        from eval_agent.orchestration import feature_list
        # Phase 2 adds an ``update_status_from_run`` function
        assert hasattr(feature_list, "update_status_from_run")

    def test_feature_list_updates_passes_flag(
        self, pipeline_output: Path, state_paths: dict[str, Path],
        tmp_path: Path,
    ) -> None:
        """After a successful run, features should be marked
        passes=True if their precision >= floor."""
        # Bootstrap a feature_list.json under tmp_path
        from eval_agent.orchestration import feature_list as fl

        # Inject the tmp path so we don't touch the real one
        fl_path = tmp_path / "feature_list.json"
        original_features = fl.bootstrap.__wrapped__ if hasattr(fl.bootstrap, "__wrapped__") else None

        # Run a high-precision session
        judge = MockJudge(id="mock-judge-v1", strategy="always_full")
        session = Session(_config(pipeline_output), judge=judge, **state_paths)
        session.startup()
        verdicts = session.execute()
        run_dir = session.checkpoint(verdicts)
        session.finalize()

        # Update feature_list from the run
        # First ensure a clean feature_list.json under tmp_path
        bootstrap_path = state_paths["runs_dir"].parent / "feature_list.json"
        fl_payload = {
            "version": 1,
            "features": [
                {"id": f"{v.evaluator_id}.{v.sub_type}",
                 "evaluator": v.evaluator_id,
                 "sub_type": v.sub_type,
                 "threshold": 0.85,
                 "status": {"passes": False, "attempts": 0,
                             "last_run": None, "last_precision": None,
                             "notes": ""}}
                for v in verdicts
            ],
        }
        bootstrap_path.write_text(json.dumps(fl_payload, indent=2), encoding="utf-8")

        fl.update_status_from_run(
            feature_list_path=bootstrap_path,
            run_dir=run_dir,
            precision_floor=0.80,
        )
        updated = json.loads(bootstrap_path.read_text(encoding="utf-8"))
        # At least one feature should now be passing (always_full ⇒ 100%)
        passing = [f for f in updated["features"] if f["status"]["passes"]]
        assert passing
        for f in passing:
            assert f["status"]["last_run"] == run_dir.name
            assert f["status"]["last_precision"] is not None
            assert f["status"]["attempts"] >= 1

    def test_feature_list_never_drops_entries(
        self, pipeline_output: Path, mock_judge: MockJudge,
        state_paths: dict[str, Path], tmp_path: Path,
    ) -> None:
        """Even a feature with zero candidates this run must remain
        in the file with its previous status preserved."""
        from eval_agent.orchestration import feature_list as fl
        bootstrap_path = state_paths["runs_dir"].parent / "feature_list.json"
        # Seed with one feature that the fixture will NEVER produce
        # candidates for (person_ner.CENSOR — no censored persons in fixture)
        seed = {
            "version": 1,
            "features": [
                {"id": "person_ner.CENSOR", "evaluator": "person_ner",
                 "sub_type": "CENSOR", "threshold": 0.85,
                 "status": {"passes": False, "attempts": 3,
                             "last_run": "old-run-id", "last_precision": 0.4,
                             "notes": "previous run"}},
                {"id": "contents_ner.FOLIO", "evaluator": "contents_ner",
                 "sub_type": "FOLIO", "threshold": 0.85,
                 "status": {"passes": False, "attempts": 0,
                             "last_run": None, "last_precision": None,
                             "notes": ""}},
            ],
        }
        bootstrap_path.write_text(json.dumps(seed, indent=2), encoding="utf-8")

        session = Session(_config(pipeline_output), judge=mock_judge, **state_paths)
        session.startup()
        verdicts = session.execute()
        run_dir = session.checkpoint(verdicts)

        fl.update_status_from_run(
            feature_list_path=bootstrap_path,
            run_dir=run_dir,
            precision_floor=0.80,
        )
        updated = json.loads(bootstrap_path.read_text(encoding="utf-8"))
        ids = {f["id"] for f in updated["features"]}
        # Both features must still be there
        assert "person_ner.CENSOR" in ids
        assert "contents_ner.FOLIO" in ids
        # The unchanged feature's status fields are preserved
        censor = next(f for f in updated["features"] if f["id"] == "person_ner.CENSOR")
        assert censor["status"]["attempts"] == 3
        assert censor["status"]["last_run"] == "old-run-id"
        assert censor["status"]["last_precision"] == 0.4

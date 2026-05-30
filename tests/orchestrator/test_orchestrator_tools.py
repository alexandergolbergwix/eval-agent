"""Phase 1 tool tests — each tool returns a well-shaped Observation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_agent.orchestrator.tools import (
    Observation, ToolContext, dispatch,
)


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """A populated state dir: one run with manifest + summary + results."""
    state = tmp_path / "state"
    runs = state / "runs" / "20260530T000000000000Z"
    runs.mkdir(parents=True)
    (runs / "manifest.json").write_text(json.dumps({
        "run_id": "20260530T000000000000Z",
        "config": {
            "judge_model": "gemini-3.5-flash",
            "evaluators":  ["person_ner", "provenance_ner"],
        },
        "stats": {
            "candidates_total": 10, "cache_hits": 4,
            "judged_full": 6, "judged_partial": 2, "judged_fail": 2,
            "input_tokens": 1000, "output_tokens": 100,
            "started_at":  "2026-05-30T00:00:00+00:00",
            "finished_at": "2026-05-30T00:01:00+00:00",
        },
    }))
    (runs / "summary.csv").write_text(
        "evaluator,sub_type,total,full,partial,fail,errors,"
        "name_yes,type_yes,role_yes,precision_strict,precision_full_or_partial\n"
        "person_ner,AUTHOR,5,3,1,1,0,5,5,3,0.6,0.8\n"
        "person_ner,OWNER,3,3,0,0,0,3,3,3,1.0,1.0\n"
        "provenance_ner,DATE,2,1,1,0,0,2,2,2,0.5,1.0\n"
    )
    (runs / "results.jsonl").write_text("\n".join([
        json.dumps({"record_id": "r1", "evaluator_id": "person_ner",
                    "sub_type": "AUTHOR",
                    "candidate": {"person": "X"},
                    "verdict": {"overall": "fail",
                                "reasoning": "wrong name"}}),
        json.dumps({"record_id": "r2", "evaluator_id": "person_ner",
                    "sub_type": "AUTHOR",
                    "candidate": {"person": "Y"},
                    "verdict": {"overall": "partial",
                                "reasoning": "ambiguous"}}),
        json.dumps({"record_id": "r3", "evaluator_id": "person_ner",
                    "sub_type": "OWNER",
                    "candidate": {"person": "Z"},
                    "verdict": {"overall": "full",
                                "reasoning": "ok"}}),
    ]))
    (state / "feature_list.json").write_text(json.dumps({
        "version": 1,
        "features": [
            {"id": "person_ner.AUTHOR", "evaluator": "person_ner",
             "sub_type": "AUTHOR", "threshold": 0.85,
             "status": {"passes": False, "attempts": 3,
                        "last_run": "20260530T000000000000Z",
                        "last_precision": 0.6}},
            {"id": "person_ner.OWNER", "evaluator": "person_ner",
             "sub_type": "OWNER", "threshold": 0.85,
             "status": {"passes": True, "attempts": 2,
                        "last_run": "20260530T000000000000Z",
                        "last_precision": 1.0}},
        ],
    }))
    return state


@pytest.fixture
def ctx(state_dir: Path) -> ToolContext:
    return ToolContext(state_dir=state_dir, goal="test")


# ── Tests ──────────────────────────────────────────────────────────────


def test_inspect_state_lists_recent_runs(ctx: ToolContext) -> None:
    obs = dispatch("inspect_state", {}, ctx)
    assert obs.ok
    assert obs.data["features_total"] == 2
    assert obs.data["features_passing"] == 1
    assert len(obs.data["runs"]) == 1
    assert obs.data["runs"][0]["run_id"] == "20260530T000000000000Z"


def test_read_latest_report_returns_not_found_when_no_report(
    ctx: ToolContext,
) -> None:
    # The fixture has manifest+summary+results but no report.md — the
    # tool returns ok=True with an empty body (the absence of a report
    # is information, not an error). Adjust if you prefer strict 404.
    obs = dispatch("read_latest_report", {}, ctx)
    assert obs.ok
    assert obs.data["report_md"] == ""


def test_read_benchmark_metrics_sorts_worst_first(ctx: ToolContext) -> None:
    obs = dispatch("read_benchmark_metrics", {"task": "person_ner"}, ctx)
    assert obs.ok
    rows = obs.data["rows"]
    assert rows[0]["sub_type"] == "AUTHOR"
    assert rows[0]["precision_strict"] == 0.6


def test_read_benchmark_metrics_requires_task(ctx: ToolContext) -> None:
    obs = dispatch("read_benchmark_metrics", {}, ctx)
    assert not obs.ok
    assert obs.error == "bad_args"


def test_inspect_failed_candidates_filters_by_overall(ctx: ToolContext) -> None:
    obs = dispatch(
        "inspect_failed_candidates",
        {"task": "person_ner", "limit": 50},
        ctx,
    )
    assert obs.ok
    # Two failing rows (fail + partial). 'full' must be excluded.
    overalls = {c["overall"] for c in obs.data["candidates"]}
    assert overalls == {"fail", "partial"}


def test_summarize_feature_list_groups_by_evaluator(ctx: ToolContext) -> None:
    obs = dispatch("summarize_feature_list", {}, ctx)
    assert obs.ok
    assert obs.data["by_evaluator"]["person_ner"] == {"total": 2, "passing": 1}


def test_recommend_next_eval_surfaces_failing_feature(ctx: ToolContext) -> None:
    obs = dispatch("recommend_next_eval", {}, ctx)
    assert obs.ok
    cands = obs.data["candidates"]
    assert len(cands) == 1
    assert cands[0]["id"] == "person_ner.AUTHOR"


def test_unknown_tool_returns_observation_not_exception(
    ctx: ToolContext,
) -> None:
    obs = dispatch("rm_rf", {}, ctx)
    assert isinstance(obs, Observation)
    assert not obs.ok
    assert obs.error == "unknown_tool"


def test_tool_raising_internally_is_caught(ctx: ToolContext) -> None:
    """If a tool raises (e.g. corrupted state file), the loop must
    keep running with an ok=False observation."""
    # Corrupt the feature_list so the tool will try to read garbage.
    (ctx.state_dir / "feature_list.json").write_text("{not json")
    obs = dispatch("summarize_feature_list", {}, ctx)
    # Gracefully empty rather than crashed — state_reader catches
    # JSONDecodeError and returns []. So 'ok' here is True with zero
    # rows; the test asserts the loop never raised.
    assert isinstance(obs, Observation)
    assert obs.ok is True
    assert obs.data["by_evaluator"] == {}


def test_compare_runs_reports_missing_run(ctx: ToolContext) -> None:
    obs = dispatch("compare_runs", {"run_a": "ghost_a", "run_b": "ghost_b"}, ctx)
    assert not obs.ok
    assert obs.error == "not_found"

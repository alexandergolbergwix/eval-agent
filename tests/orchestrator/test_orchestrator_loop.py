"""End-to-end tests for the Phase 1 orchestrator loop.

These cover the contract the web bridge depends on:
- happy-path session writes trace + decisions + final report.
- malformed LLM turn aborts the session cleanly.
- step budget is enforced.
- on_step callback fires for every event.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_agent.orchestrator import (
    Budget, MODE_PLAN_ONLY, Orchestrator, StubJudge, run_session,
)


@pytest.fixture
def empty_state(tmp_path: Path) -> Path:
    """Empty state dir — no runs, no feature_list. Forces tools to
    return graceful 'not found' observations the loop must tolerate."""
    return tmp_path / "state"


def _final_turn(summary: str = "done") -> dict:
    return {
        "thought_summary": "wrap up",
        "final": True,
        "final_report": {
            "summary": summary, "recommended_next_steps": [],
            "risks": [], "commands": [], "evidence_paths": [],
        },
    }


def _action(tool: str, args: dict | None = None) -> dict:
    return {
        "thought_summary": f"call {tool}",
        "final": False,
        "action": {"tool": tool, "args": args or {}},
    }


def test_happy_path_writes_session_artifacts(empty_state: Path) -> None:
    judge = StubJudge(script=[
        _action("inspect_state"),
        _final_turn("nothing to recommend"),
    ])
    result = run_session(
        judge=judge, goal="smoke", state_dir=empty_state,
        budget=Budget(max_steps=3, max_seconds=5),
    )
    assert result.outcome == "final"
    assert result.final is not None
    assert result.final.summary == "nothing to recommend"
    # session_dir is under state/orchestrator/sessions/<ts>/
    assert result.session_dir.parent == empty_state / "orchestrator" / "sessions"
    # Every promised artifact materialised.
    assert (result.session_dir / "trace.jsonl").exists()
    assert (result.session_dir / "decisions.jsonl").exists()
    assert (result.session_dir / "final_report.md").exists()


def test_trace_contains_every_event_kind(empty_state: Path) -> None:
    judge = StubJudge(script=[
        _action("inspect_state"),
        _action("nope_not_a_tool"),         # triggers policy.refuse
        _final_turn(),
    ])
    result = run_session(
        judge=judge, goal="smoke", state_dir=empty_state,
        budget=Budget(max_steps=5, max_seconds=5),
    )
    events = [
        json.loads(line) for line in
        (result.session_dir / "trace.jsonl").read_text().splitlines() if line.strip()
    ]
    kinds = [e["type"] for e in events]
    # Order matters: start → llm turn → tool dispatch+result → llm turn → refuse →
    # llm turn → final → end.
    assert kinds[0] == "session.start"
    assert "llm.turn" in kinds
    assert "tool.dispatch" in kinds
    assert "tool.result" in kinds
    assert "policy.refuse" in kinds
    assert "session.final" in kinds
    assert kinds[-1] == "session.end"


def test_step_budget_caps_session(empty_state: Path) -> None:
    """A bottomless action script must terminate at max_steps."""
    judge = StubJudge(script=[_action("inspect_state")] * 10)
    result = run_session(
        judge=judge, goal="bottomless", state_dir=empty_state,
        budget=Budget(max_steps=3, max_seconds=5),
    )
    assert result.outcome == "step_budget"
    assert result.steps_used == 3
    # No final_report committed → incomplete report goes out instead.
    body = (result.session_dir / "final_report.md").read_text()
    assert "incomplete" in body.lower()


def test_on_step_fires_for_every_event(empty_state: Path) -> None:
    events: list[dict] = []
    judge = StubJudge(script=[_action("inspect_state"), _final_turn()])
    run_session(
        judge=judge, goal="emit", state_dir=empty_state,
        budget=Budget(max_steps=3, max_seconds=5),
        on_step=events.append,
    )
    types = [e["type"] for e in events]
    assert "session.start" in types
    assert "tool.result" in types
    assert "session.end"  in types


def test_malformed_llm_turn_aborts_cleanly(empty_state: Path) -> None:
    """A turn missing required fields must not crash the loop."""
    bad = {"final": False}  # missing thought_summary + action
    judge = StubJudge(script=[bad])
    result = run_session(
        judge=judge, goal="malformed", state_dir=empty_state,
        budget=Budget(max_steps=3, max_seconds=5),
    )
    assert result.outcome == "parse_error"
    assert result.final is None
    # Trace should record the parse error.
    body = (result.session_dir / "trace.jsonl").read_text()
    assert "llm.parse_error" in body


def test_pathological_refusal_loop_breaks_after_three(empty_state: Path) -> None:
    judge = StubJudge(script=[_action("disallowed")] * 10)
    result = run_session(
        judge=judge, goal="pathological", state_dir=empty_state,
        budget=Budget(max_steps=20, max_seconds=5),
    )
    assert result.outcome == "no_progress"
    # Steps used should be small — we bail out after 3 refusals.
    assert result.steps_used <= 3


def test_mode_default_is_plan_only(empty_state: Path) -> None:
    judge = StubJudge(script=[_final_turn()])
    result = run_session(
        judge=judge, goal="default-mode", state_dir=empty_state,
        budget=Budget(max_steps=3, max_seconds=5),
    )
    assert result.outcome == "final"
    # Snapshot the allowlist via the trace.
    start = json.loads((result.session_dir / "trace.jsonl").read_text().splitlines()[0])
    assert start["mode"] == MODE_PLAN_ONLY
    assert "inspect_state" in start["allowlist"]
    # Phase 2/4 modes pre-declared but empty.
    assert "run_eval_agent" not in start["allowlist"]

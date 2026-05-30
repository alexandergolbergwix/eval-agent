"""Policy-layer tests — what the orchestrator may and may not do."""

from __future__ import annotations

import time

import pytest

from eval_agent.orchestrator.policy import (
    ALLOW_BY_MODE, Budget, MODE_AUTONOMOUS, MODE_PLAN_ONLY, MODE_SUPERVISED,
    Refusal, allow, build_policy, charge,
)


def test_plan_only_allowlist_is_seven_read_only_tools() -> None:
    p = build_policy(mode=MODE_PLAN_ONLY)
    assert p.allowlist == ALLOW_BY_MODE[MODE_PLAN_ONLY]
    assert "inspect_state" in p.allowlist
    assert "read_latest_report" in p.allowlist
    # Phase-2+ tools must NOT leak into plan-only.
    assert "run_eval_agent" not in p.allowlist
    assert "propose_prompt_patch" not in p.allowlist


def test_supervised_and_autonomous_are_empty_in_phase_one() -> None:
    """Phase 2/4 modes are pre-declared but ship empty allowlists.

    Lifting Phase 2 will add to ALLOW_BY_MODE; until then, calling
    `--supervised` must produce a policy that refuses every tool.
    """
    assert ALLOW_BY_MODE[MODE_SUPERVISED] == frozenset()
    assert ALLOW_BY_MODE[MODE_AUTONOMOUS] == frozenset()


def test_explicit_allowlist_can_narrow_but_not_widen() -> None:
    # A widening attempt is silently dropped.
    p = build_policy(
        mode=MODE_PLAN_ONLY,
        explicit_allowlist=["inspect_state", "run_eval_agent"],
    )
    assert p.allowlist == frozenset({"inspect_state"})


def test_allow_returns_none_for_a_whitelisted_call() -> None:
    p = build_policy(mode=MODE_PLAN_ONLY)
    assert allow(p, "inspect_state", {}) is None


def test_allow_refuses_unknown_tool() -> None:
    p = build_policy(mode=MODE_PLAN_ONLY)
    r = allow(p, "rm_rf", {})
    assert isinstance(r, Refusal)
    assert r.reason == "tool_not_allowed"


def test_step_budget_refusal() -> None:
    p = build_policy(mode=MODE_PLAN_ONLY, budget=Budget(max_steps=1))
    charge(p, steps=1)
    r = allow(p, "inspect_state", {})
    assert isinstance(r, Refusal)
    assert r.reason == "step_budget_exhausted"


def test_wallclock_budget_refusal_kicks_in_after_max_seconds() -> None:
    p = build_policy(mode=MODE_PLAN_ONLY,
                      budget=Budget(max_steps=10, max_seconds=0))
    # Already past the cap because max_seconds=0.
    time.sleep(0.01)
    r = allow(p, "inspect_state", {})
    assert isinstance(r, Refusal)
    assert r.reason == "wallclock_budget_exhausted"


def test_forbidden_model_arg_is_refused() -> None:
    p = build_policy(mode=MODE_PLAN_ONLY)
    r = allow(p, "inspect_state", {"model": "gemini-1.5-flash"})
    assert isinstance(r, Refusal)
    assert r.reason == "forbidden_model"


def test_modern_judge_model_passes() -> None:
    p = build_policy(mode=MODE_PLAN_ONLY)
    assert allow(p, "inspect_state", {"model": "gemini-3.5-flash"}) is None


def test_empty_tool_name_is_refused() -> None:
    p = build_policy(mode=MODE_PLAN_ONLY)
    r = allow(p, "", {})
    assert isinstance(r, Refusal)
    assert r.reason == "empty_tool"

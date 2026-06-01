"""Safety policy for LLM-chosen orchestrator actions.

The LLM may propose an operation, but this module is the authority on
whether that operation is reachable in the current mode. The default
``plan_only`` mode is read-only. The user must explicitly select
``supervised`` or ``autonomous`` before the orchestrator can run
eval-agent commands or write proposal artifacts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

MODE_PLAN_ONLY = "plan_only"
MODE_SUPERVISED = "supervised"
MODE_AUTONOMOUS = "autonomous"


READ_ONLY_TOOLS = frozenset({
    "inspect_state",
    "read_latest_report",
    "read_benchmark_metrics",
    "compare_runs",
    "inspect_failed_candidates",
    "summarize_feature_list",
    "recommend_next_eval",
})

EXECUTION_TOOLS = frozenset({
    "run_eval_agent",
    "regenerate_report",
})

PROPOSAL_TOOLS = frozenset({
    "write_plan_note",
    "create_experiment_manifest",
})

ALL_TOOLS = READ_ONLY_TOOLS | EXECUTION_TOOLS | PROPOSAL_TOOLS

ALLOW_BY_MODE: dict[str, frozenset[str]] = {
    MODE_PLAN_ONLY: READ_ONLY_TOOLS,
    MODE_SUPERVISED: READ_ONLY_TOOLS | EXECUTION_TOOLS | PROPOSAL_TOOLS,
    MODE_AUTONOMOUS: ALL_TOOLS,
}


@dataclass(frozen=True)
class Budget:
    """Hard limits for one orchestrator session."""

    max_steps: int = 12
    max_seconds: int = 180
    max_usd: float = 0.10


@dataclass(frozen=True)
class Refusal:
    """Reason an action was refused before tool execution."""

    reason: str
    detail: str


@dataclass
class Policy:
    """Mode-specific action allowlist."""

    mode: str
    allowlist: frozenset[str] = field(default_factory=frozenset)
    budget: Budget = field(default_factory=Budget)
    started_at: float = field(default_factory=time.time)
    steps_used: int = 0
    usd_used: float = 0.0

    def validate(self, action: dict[str, Any]) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for an LLM action."""
        if bool(action.get("final")):
            return True, "final report"
        raw = action.get("action")
        if not isinstance(raw, dict):
            return False, "missing action object"
        tool = str(raw.get("tool") or "")
        refusal = allow(self, tool, raw.get("args") or {})
        if refusal is not None:
            return False, refusal.detail
        return True, "allowed"


def build_policy(
    *,
    mode: str = MODE_PLAN_ONLY,
    explicit_allowlist: list[str] | None = None,
    budget: Budget | None = None,
) -> Policy:
    """Build a policy, optionally narrowing its mode allowlist.

    An explicit allowlist can only remove tools; it can never widen the
    mode's built-in permissions.
    """
    mode_key = mode.replace("-", "_")
    base = ALLOW_BY_MODE.get(mode_key, frozenset())
    if explicit_allowlist is not None:
        base = base.intersection(explicit_allowlist)
    return Policy(mode=mode_key, allowlist=frozenset(base), budget=budget or Budget())


def allow(policy: Policy, tool: str, args: dict[str, Any]) -> Refusal | None:
    """Return a refusal for a tool call, or ``None`` when allowed."""
    if not tool:
        return Refusal("empty_tool", "action.tool is required")
    if tool not in policy.allowlist:
        return Refusal("tool_not_allowed", f"{tool!r} is not allowed in {policy.mode}")
    if policy.steps_used >= policy.budget.max_steps:
        return Refusal("step_budget_exhausted", "orchestrator step budget is exhausted")
    if (time.time() - policy.started_at) >= policy.budget.max_seconds:
        return Refusal("wallclock_budget_exhausted", "orchestrator wall-clock budget is exhausted")
    if policy.usd_used >= policy.budget.max_usd:
        return Refusal("usd_budget_exhausted", "orchestrator cost budget is exhausted")
    requested_model = args.get("model")
    if isinstance(requested_model, str) and not requested_model.startswith("gemini-3."):
        return Refusal("forbidden_model", "orchestrator may not downgrade below Gemini 3.x")
    return None


def charge(policy: Policy, *, steps: int = 0, usd: float = 0.0) -> None:
    """Charge work against the session budget."""
    policy.steps_used += steps
    policy.usd_used += usd


__all__ = [
    "ALLOW_BY_MODE",
    "ALL_TOOLS",
    "Budget",
    "EXECUTION_TOOLS",
    "MODE_AUTONOMOUS",
    "MODE_PLAN_ONLY",
    "MODE_SUPERVISED",
    "Policy",
    "PROPOSAL_TOOLS",
    "READ_ONLY_TOOLS",
    "Refusal",
    "allow",
    "build_policy",
    "charge",
]

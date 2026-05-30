"""Safety policy for the orchestrator.

The policy module is the **only** layer that authorises a tool call.
Tools themselves never check authorisation; they trust the loop did
the gating. That keeps the policy auditable in one place — diff the
file when you change what the LLM can do.

Three concerns:

1. **Allowlist by mode.** Phase 1 ships only the ``"plan_only"`` mode
   (read-only tools). Modes for later phases (``"supervised"``,
   ``"autonomous"``) are pre-declared with empty allowlists so the
   schema is stable but no extra tools become reachable.

2. **Budgets.** Hard caps on steps, wall-clock seconds, and USD spend.
   The loop polls these on every iteration; exceeding any cap forces
   an immediate ``Final`` with a ``risks`` note.

3. **Doctrine.** Tiny enforcement of the project's metric and judge
   policies — refuse to switch the default Gemini model silently,
   refuse to treat candidate rates as F1, etc. These are checked
   structurally: an LLM action that names a forbidden model is
   rejected before the tool runs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


# Modes the policy understands. Phase 1 = plan-only. Phase 2-4 phases
# add to ALLOW_BY_MODE without changing the schema; everything outside
# the listed allowlist remains refused.
MODE_PLAN_ONLY    = "plan_only"
MODE_SUPERVISED   = "supervised"
MODE_AUTONOMOUS   = "autonomous"


# Default per-mode allowlist. The CLI / web bridge can pass an
# explicit narrower list (e.g. drop ``compare_runs`` when comparing
# is meaningless), but it cannot widen — extra names are silently
# dropped at policy build time.
ALLOW_BY_MODE: dict[str, frozenset[str]] = {
    MODE_PLAN_ONLY: frozenset({
        "inspect_state",
        "read_latest_report",
        "read_benchmark_metrics",
        "compare_runs",
        "inspect_failed_candidates",
        "summarize_feature_list",
        "recommend_next_eval",
    }),
    MODE_SUPERVISED: frozenset(),    # Phase 2 will populate this
    MODE_AUTONOMOUS: frozenset(),    # Phase 4 will populate this
}


# Doctrinal invariants — refuse anything that would silently violate
# them. Each rule maps a tool-name + arg-shape to a structural check.
_FORBIDDEN_MODELS_IN_ARGS: tuple[str, ...] = (
    "gemini-1.0", "gemini-1.5",                   # too old
    # Future-proofing: when we DO want to switch the default judge model
    # this list should be updated AND the switch surfaced in the run
    # manifest. Rule 55 forbids silent swaps.
)


@dataclass
class Budget:
    """Hard budget caps for one session."""

    max_steps:        int = 12
    max_seconds:      int = 180
    max_usd:          float = 0.10
    # Used by the supervised mode in Phase 2; in plan-only every
    # allowed tool is implicitly approved.
    require_approval: bool = False


@dataclass
class Policy:
    """Per-session policy snapshot. Built once by the loop."""

    mode:        str
    allowlist:   frozenset[str]
    budget:      Budget
    started_at:  float = field(default_factory=time.time)
    # Telemetry the loop pokes into; the policy reads it to decide
    # when a budget cap kicks in.
    steps_used:  int   = 0
    usd_used:    float = 0.0


# ── Construction ───────────────────────────────────────────────────────


def build_policy(
    *,
    mode: str = MODE_PLAN_ONLY,
    explicit_allowlist: list[str] | None = None,
    budget: Budget | None = None,
) -> Policy:
    """Build a policy for a session.

    *explicit_allowlist* can NARROW but never widen the mode default.
    Names not in the mode default are silently dropped.
    """
    default = ALLOW_BY_MODE.get(mode, frozenset())
    if explicit_allowlist is not None:
        narrow = frozenset(explicit_allowlist) & default
    else:
        narrow = default
    return Policy(
        mode=mode,
        allowlist=narrow,
        budget=budget or Budget(),
    )


# ── Authorisation ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Refusal:
    """Carries a refusal reason back to the loop. Mirrors the shape of an
    Observation so the loop / trace can treat the two uniformly."""

    reason: str
    detail: str = ""


def allow(policy: Policy, tool: str, args: dict[str, Any]) -> Refusal | None:
    """Return None when the tool may run, or a Refusal when it must not.

    Pure function — no side effects, no time charges. The loop charges
    steps/usd separately on a successful dispatch.
    """
    if not tool:
        return Refusal(reason="empty_tool")
    if tool not in policy.allowlist:
        return Refusal(
            reason="tool_not_allowed",
            detail=(
                f"tool {tool!r} is not on the {policy.mode!r} allowlist "
                f"({sorted(policy.allowlist)})"
            ),
        )

    # Step + USD budget caps.
    if policy.steps_used >= policy.budget.max_steps:
        return Refusal(
            reason="step_budget_exhausted",
            detail=f"used {policy.steps_used} of {policy.budget.max_steps} steps",
        )
    elapsed = time.time() - policy.started_at
    if elapsed >= policy.budget.max_seconds:
        return Refusal(
            reason="wallclock_budget_exhausted",
            detail=f"used {elapsed:.0f}s of {policy.budget.max_seconds}s",
        )
    if policy.usd_used >= policy.budget.max_usd:
        return Refusal(
            reason="usd_budget_exhausted",
            detail=f"spent ${policy.usd_used:.4f} of ${policy.budget.max_usd:.4f}",
        )

    # Doctrinal arg-shape checks. Trivial for Phase 1; reserved for
    # future-phase tools that take model ids as args.
    model_arg = str(args.get("model") or args.get("judge_model") or "")
    if model_arg and any(model_arg.startswith(bad) for bad in _FORBIDDEN_MODELS_IN_ARGS):
        return Refusal(
            reason="forbidden_model",
            detail=f"model {model_arg!r} is below the project's supported floor",
        )

    return None


def charge(policy: Policy, *, steps: int = 1, usd: float = 0.0) -> None:
    """Bookkeeping the loop calls after a successful dispatch."""
    policy.steps_used += steps
    policy.usd_used   += usd


__all__ = [
    "ALLOW_BY_MODE",
    "Budget",
    "MODE_AUTONOMOUS",
    "MODE_PLAN_ONLY",
    "MODE_SUPERVISED",
    "Policy",
    "Refusal",
    "allow",
    "build_policy",
    "charge",
]

"""LLM orchestrator for eval-agent.

The orchestrator is intentionally separate from the candidate-level judge:
it decides which evaluation operation to run next, while Python validates
and executes only allowlisted tools.

Phase 1 (this milestone) ships read-only tools, a strict JSON action
schema, a hard policy layer, and a session trace persisted to
``state/orchestrator/sessions/<ts>/``. Later phases (supervised
execution, proposal-only mutation, autonomous experiments) plug into
the same loop by widening the policy allowlist — the loop, tools, and
trace shapes are stable from here.
"""

from eval_agent.orchestrator.loop import (
    LoopResult, Orchestrator, StubJudge, run_session,
)
from eval_agent.orchestrator.policy import (
    Budget, MODE_AUTONOMOUS, MODE_PLAN_ONLY, MODE_SUPERVISED, Policy,
    build_policy,
)
from eval_agent.orchestrator.schemas import (
    ACTION_SCHEMA, Action, AllowedTool, Final, parse_turn,
)
from eval_agent.orchestrator.tools import (
    Observation, REGISTRY, TOOL_SPECS, ToolContext, dispatch,
)

__all__ = [
    "ACTION_SCHEMA", "Action", "AllowedTool", "Budget",
    "Final", "LoopResult", "MODE_AUTONOMOUS", "MODE_PLAN_ONLY",
    "MODE_SUPERVISED", "Observation", "Orchestrator", "Policy",
    "REGISTRY", "StubJudge", "TOOL_SPECS", "ToolContext",
    "build_policy", "dispatch", "parse_turn", "run_session",
]

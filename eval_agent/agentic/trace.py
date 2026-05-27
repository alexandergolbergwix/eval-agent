"""Per-candidate trace of the agentic loop — the audit record of agency."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceStep:
    """One step of the loop: a tool call + its observation, or the verdict turn."""

    step: int
    tool: str | None              # None on the final verdict turn
    args: dict[str, Any]
    observation: str | None       # tool result; None on the verdict turn
    note: str | None = None       # optional model text alongside the call

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "tool": self.tool,
            "args": self.args,
            "observation": self.observation,
            "note": self.note,
        }


@dataclass
class Trace:
    """Full record of how one candidate was judged agentically."""

    record_id: str
    evaluator_id: str
    sub_type: str
    steps: list[TraceStep] = field(default_factory=list)
    final_model: str | None = None
    escalated: bool = False

    def add(
        self,
        *,
        tool: str | None,
        args: dict[str, Any] | None = None,
        observation: str | None = None,
        note: str | None = None,
    ) -> None:
        self.steps.append(
            TraceStep(
                step=len(self.steps) + 1,
                tool=tool,
                args=dict(args or {}),
                observation=observation,
                note=note,
            )
        )

    def tools_used(self) -> list[str]:
        return [s.tool for s in self.steps if s.tool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "evaluator_id": self.evaluator_id,
            "sub_type": self.sub_type,
            "final_model": self.final_model,
            "escalated": self.escalated,
            "tools_used": self.tools_used(),
            "steps": [s.to_dict() for s in self.steps],
        }


__all__ = ["Trace", "TraceStep"]

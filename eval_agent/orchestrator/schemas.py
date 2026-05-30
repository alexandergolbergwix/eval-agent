"""Schemas for the LLM orchestrator action protocol.

The orchestrator LLM emits strict JSON per ``ACTION_SCHEMA`` on every
turn. Two shapes share one schema:

* **Action turn** — ``{thought_summary, final: false, action: {tool, args}}``.
* **Final turn** — ``{thought_summary, final: true, final_report: {...}}``.

Phase 1 ships a fixed allowlist of read-only tools (``ALLOWED_TOOLS``).
Later phases extend the list; the policy layer (``policy.py``) is the
authority on which tools are reachable in a given session — never the
schema.

A small dataclass mirror (``Action``, ``Final``) is provided so the
loop, the tests, and the future web bridge can talk in Python types
without re-parsing the dict shape on every step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AllowedTool(str, Enum):
    """Tools available to the orchestrator in Phase 1.

    Listed here as the schema-level vocabulary the LLM is told about.
    The policy module (``policy.allow``) is what actually authorises a
    call at run time, and it can be narrower than this (e.g. plan-only
    mode disables every tool with a side effect — though in Phase 1
    none have side effects yet).
    """

    INSPECT_STATE              = "inspect_state"
    READ_LATEST_REPORT         = "read_latest_report"
    READ_BENCHMARK_METRICS     = "read_benchmark_metrics"
    COMPARE_RUNS               = "compare_runs"
    INSPECT_FAILED_CANDIDATES  = "inspect_failed_candidates"
    SUMMARIZE_FEATURE_LIST     = "summarize_feature_list"
    RECOMMEND_NEXT_EVAL        = "recommend_next_eval"


@dataclass(frozen=True)
class Action:
    """One non-final turn — the LLM wants to call exactly one tool."""

    thought_summary: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Final:
    """The LLM's final report — no more tool calls."""

    thought_summary: str
    summary: str
    recommended_next_steps: list[str] = field(default_factory=list)
    risks:                  list[str] = field(default_factory=list)
    commands:               list[str] = field(default_factory=list)
    evidence_paths:         list[str] = field(default_factory=list)


def parse_turn(raw: dict[str, Any]) -> Action | Final:
    """Coerce an LLM JSON object into ``Action`` or ``Final``.

    Raises ``ValueError`` when the shape is malformed. The loop catches
    that and asks the LLM to retry with the schema reminder; we never
    silently accept a half-formed turn.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object, got {type(raw).__name__}")
    thought = str(raw.get("thought_summary") or "").strip()
    if not thought:
        raise ValueError("thought_summary is required and must be non-empty")

    if raw.get("final"):
        report = raw.get("final_report") or {}
        if not isinstance(report, dict):
            raise ValueError("final_report must be an object")
        summary = str(report.get("summary") or "").strip()
        if not summary:
            raise ValueError("final_report.summary is required")
        def _str_list(key: str) -> list[str]:
            vs = report.get(key) or []
            if not isinstance(vs, list):
                raise ValueError(f"final_report.{key} must be a list of strings")
            return [str(x) for x in vs]
        return Final(
            thought_summary=thought,
            summary=summary,
            recommended_next_steps=_str_list("recommended_next_steps"),
            risks=_str_list("risks"),
            commands=_str_list("commands"),
            evidence_paths=_str_list("evidence_paths"),
        )

    act = raw.get("action") or {}
    if not isinstance(act, dict):
        raise ValueError("action must be an object")
    tool = str(act.get("tool") or "").strip()
    args = act.get("args") or {}
    if not tool:
        raise ValueError("action.tool is required")
    if not isinstance(args, dict):
        raise ValueError("action.args must be an object")
    return Action(thought_summary=thought, tool=tool, args=args)


ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought_summary": {
            "type": "string",
            "description": "Brief reason for the next action. No hidden chain-of-thought.",
        },
        "final": {
            "type": "boolean",
            "description": "Set true only when no more tool calls are needed.",
        },
        "action": {
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "args": {"type": "object"},
            },
            "required": ["tool", "args"],
        },
        "final_report": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "recommended_next_steps": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "risks": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "evidence_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                "summary",
                "recommended_next_steps",
                "risks",
                "commands",
                "evidence_paths",
            ],
        },
    },
    "required": ["thought_summary", "final"],
}


def is_final_action(action: dict[str, Any]) -> bool:
    """Return true when the LLM produced a final report action."""
    return bool(action.get("final"))


__all__ = [
    "ACTION_SCHEMA",
    "AllowedTool",
    "Action",
    "Final",
    "is_final_action",
    "parse_turn",
]

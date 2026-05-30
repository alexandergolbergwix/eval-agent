"""Prompt builders for the eval-agent orchestrator."""

from __future__ import annotations

from typing import Any


SYSTEM_PROMPT = """You are the eval-agent orchestrator for the MHM Pipeline.

You are not the candidate-level judge. Your job is to decide the next
evaluation operation, call one safe tool at a time, observe the result, and
eventually produce a concise final report.

Hard rules:
- Return only JSON matching the provided schema.
- Use strict benchmark F1 for model-accuracy claims.
- Treat eval-agent candidate acceptance rates as audit/triage signals only.
- Do not request arbitrary shell commands.
- Do not delete or rewrite cache/state/history.
- Keep Gemini defaults on gemini-3.5-flash unless an explicit comparison is
  requested.
- Prefer evidence paths and reproducible commands in the final report.
"""


def build_prompt(
    *,
    goal: str,
    mode: str,
    state_summary: str,
    tools: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> str:
    """Return the user prompt for one orchestrator turn."""
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Mode: {mode}\n"
        f"Goal: {goal}\n\n"
        "Current state summary:\n"
        f"{state_summary}\n\n"
        "Available tools:\n"
        f"{_render_tools(tools)}\n\n"
        "Previous observations:\n"
        f"{_render_observations(observations)}\n\n"
        "Respond with either one tool action or final=true with final_report."
    )


def _render_tools(tools: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for tool in tools:
        args = ", ".join(tool.get("args", []))
        lines.append(f"- {tool['name']}({args}): {tool['description']}")
    return "\n".join(lines)


def _render_observations(observations: list[dict[str, Any]]) -> str:
    if not observations:
        return "(none yet)"
    rendered: list[str] = []
    for idx, obs in enumerate(observations[-8:], start=1):
        rendered.append(
            f"{idx}. tool={obs.get('tool')} ok={obs.get('ok')} "
            f"summary={obs.get('summary')}"
        )
    return "\n".join(rendered)


__all__ = ["build_prompt", "SYSTEM_PROMPT"]

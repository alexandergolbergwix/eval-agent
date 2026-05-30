"""The orchestrator action loop.

One turn per iteration:

  1. Render a compact prompt (state summary + tool catalog + observation
     window) for the Judge.
  2. Ask the Judge for one strict-JSON turn.
  3. Parse it. Malformed → retry up to ``max_parse_retries`` times with
     a schema reminder.
  4. If final → write report, emit ``session.final`` + ``session.end``,
     return.
  5. Else dispatch the action through policy → tool → trace.
  6. Append the observation to the window, charge the policy budget,
     loop.

Two Judges are supported:

* :class:`StubJudge` — deterministic, scripts the next turn from a
  pre-set list. Used by tests + the ``--no-llm`` CLI path. No network.
* The real Gemini judge from :mod:`eval_agent.client.gemini_client`.
  Injected via :func:`run_session` when the caller has an API key.

This separation keeps the loop testable without secrets and lets the
web bridge integration-test the full orchestration code path against
StubJudge in CI.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from eval_agent.orchestrator import policy as pol
from eval_agent.orchestrator import prompts, state_reader, tools, trace
from eval_agent.orchestrator.schemas import (
    Action, Final, parse_turn,
)


# Anything that, given a string prompt, returns the LLM's raw JSON
# object (as a dict). Real Gemini wraps a HTTP call; StubJudge returns
# a pre-scripted list. The loop only needs this surface.
LLMFn = Callable[[str], dict[str, Any]]


# A live-step emitter — invoked on every action / observation / refusal
# so the web bridge can stream them via SSE. Default is a no-op.
StepEmitter = Callable[[dict[str, Any]], None]


@dataclass
class LoopResult:
    """What the loop returns to its caller."""

    session_dir:  Path
    final:        Final | None
    outcome:      str       # "final" | "step_budget" | "wallclock_budget" | "usd_budget" | "no_progress"
    steps_used:   int
    usd_used:     float
    wall_seconds: float
    decisions:    list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Orchestrator:
    """Owns one session: judge + policy + tools + trace + observation window."""

    judge:           LLMFn
    state_dir:       Path
    goal:            str
    mode:            str = pol.MODE_PLAN_ONLY
    budget:          pol.Budget = field(default_factory=pol.Budget)
    allowlist:       list[str] | None = None
    on_step:         StepEmitter | None = None
    max_parse_retries: int = 2

    def run(self) -> LoopResult:
        policy = pol.build_policy(
            mode=self.mode,
            explicit_allowlist=self.allowlist,
            budget=self.budget,
        )
        session_dir = trace.new_session_dir(self.state_dir)
        writer = trace.TraceWriter(session_dir)
        session_id = session_dir.name

        start_event = writer.session_start(
            session_id=session_id, mode=self.mode, goal=self.goal,
            allowlist=sorted(policy.allowlist),
            budget={
                "max_steps":   policy.budget.max_steps,
                "max_seconds": policy.budget.max_seconds,
                "max_usd":     policy.budget.max_usd,
            },
        )
        self._emit(start_event)

        ctx = tools.ToolContext(state_dir=self.state_dir, goal=self.goal)
        state_summary = state_reader.compact_state_summary(self.state_dir)
        observation_window: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        final: Final | None = None
        outcome = "no_progress"
        started = time.time()

        while True:
            # Pre-check step budget so we never even render a prompt when
            # exhausted; the policy.allow() check is the second line of
            # defence in case a tool would otherwise be authorised.
            if policy.steps_used >= policy.budget.max_steps:
                outcome = "step_budget"
                break
            if (time.time() - policy.started_at) >= policy.budget.max_seconds:
                outcome = "wallclock_budget"
                break

            prompt = prompts.build_prompt(
                goal=self.goal,
                mode=self.mode,
                state_summary=state_summary,
                tools=[t for t in tools.TOOL_SPECS if t["name"] in policy.allowlist],
                observations=observation_window,
            )
            try:
                raw = self.judge(prompt)
            except Exception as exc:  # noqa: BLE001 — let the trace record it
                ev = writer.event("llm.error",
                                  error=f"{type(exc).__name__}: {exc}")
                self._emit(ev)
                outcome = "llm_error"
                break

            turn = self._parse_with_retry(raw, writer)
            if turn is None:
                outcome = "parse_error"
                break

            if isinstance(turn, Final):
                ev = writer.session_final(final={
                    "summary":                turn.summary,
                    "recommended_next_steps": turn.recommended_next_steps,
                    "risks":                  turn.risks,
                    "commands":               turn.commands,
                    "evidence_paths":         turn.evidence_paths,
                })
                self._emit(ev)
                decisions.append({"kind": "final",
                                  "thought_summary": turn.thought_summary})
                final = turn
                outcome = "final"
                break

            # ── Action turn ────────────────────────────────────────────
            refusal = pol.allow(policy, turn.tool, turn.args)
            if refusal is not None:
                ev = writer.policy_refuse(
                    tool=turn.tool, args=turn.args,
                    reason=refusal.reason, detail=refusal.detail,
                )
                self._emit(ev)
                observation_window.append({
                    "tool":    turn.tool,
                    "ok":      False,
                    "summary": f"refused: {refusal.reason}",
                    "error":   refusal.reason,
                })
                # Refusals don't charge a step — they encourage the LLM
                # to pick a different tool on the next turn — but they
                # DO charge a parse-retry slot to avoid pathological
                # loops on the same wrong tool. Cap re-asks.
                if _last_n_refusals_match(observation_window, n=3):
                    outcome = "no_progress"
                    break
                continue

            ev = writer.tool_dispatch(tool=turn.tool, args=turn.args)
            self._emit(ev)
            obs = tools.dispatch(turn.tool, turn.args, ctx)
            ev = writer.tool_result(
                tool=obs.tool, ok=obs.ok, summary=obs.summary,
                data=obs.data, error=obs.error,
            )
            self._emit(ev)
            pol.charge(policy, steps=1, usd=0.0)
            observation_window.append({
                "tool":    obs.tool,
                "ok":      obs.ok,
                "summary": obs.summary,
                "error":   obs.error,
            })
            decisions.append({
                "kind":            "action",
                "thought_summary": turn.thought_summary,
                "tool":            turn.tool,
                "args":            turn.args,
                "ok":              obs.ok,
                "summary":         obs.summary,
            })

        wall = time.time() - started
        end_event = writer.session_end(
            outcome=outcome,
            steps_used=policy.steps_used,
            usd_used=policy.usd_used,
            wall_seconds=wall,
        )
        self._emit(end_event)
        trace.write_decisions(session_dir, decisions)
        if final is not None:
            trace.write_final_report(session_dir, _render_final_md(self.goal, final, outcome))
        else:
            trace.write_final_report(
                session_dir,
                _render_incomplete_md(self.goal, outcome, observation_window),
            )
        return LoopResult(
            session_dir=session_dir, final=final, outcome=outcome,
            steps_used=policy.steps_used,
            usd_used=policy.usd_used,
            wall_seconds=wall,
            decisions=decisions,
        )

    # ── Internals ─────────────────────────────────────────────────────

    def _parse_with_retry(
        self, raw: dict[str, Any], writer: trace.TraceWriter,
    ) -> Action | Final | None:
        for attempt in range(self.max_parse_retries + 1):
            try:
                parsed = parse_turn(raw)
            except ValueError as exc:
                ev = writer.event(
                    "llm.parse_error", attempt=attempt,
                    error=str(exc), raw=raw,
                )
                self._emit(ev)
                if attempt == self.max_parse_retries:
                    return None
                # No retry-with-correction in Phase 1 because StubJudge
                # is deterministic and the real GeminiJudge already
                # enforces ACTION_SCHEMA via responseSchema. If both
                # fail we fall out — better than infinite-looping.
                return None
            kind = "final" if isinstance(parsed, Final) else "action"
            writer.llm_turn(
                raw=raw, parsed_kind=kind,
                thought_summary=parsed.thought_summary,
            )
            return parsed
        return None

    def _emit(self, ev: dict[str, Any]) -> None:
        if self.on_step is not None:
            try:
                self.on_step(ev)
            except Exception:  # noqa: BLE001 — never let the UI break the loop
                pass


# ── Stub judge for tests + plumbing demos ──────────────────────────────


@dataclass
class StubJudge:
    """A deterministic, scriptable Judge for tests / dry-runs.

    Pass a list of dicts (matching ACTION_SCHEMA) to ``script``. Each
    call returns the next one in order; running off the end returns a
    forced ``final`` so tests don't hang.
    """

    script: list[dict[str, Any]] = field(default_factory=list)
    _i:     int                  = 0

    def __call__(self, prompt: str) -> dict[str, Any]:
        if self._i >= len(self.script):
            return _forced_final("Stub script exhausted")
        out = self.script[self._i]
        self._i += 1
        return out


def _forced_final(reason: str) -> dict[str, Any]:
    return {
        "thought_summary": reason,
        "final": True,
        "final_report": {
            "summary": reason,
            "recommended_next_steps": [],
            "risks": ["forced final — stub script exhausted"],
            "commands": [],
            "evidence_paths": [],
        },
    }


# ── Public entry point used by the CLI + the web bridge ────────────────


def run_session(
    *,
    judge: LLMFn,
    goal: str,
    state_dir: Path | None = None,
    mode: str = pol.MODE_PLAN_ONLY,
    budget: pol.Budget | None = None,
    allowlist: list[str] | None = None,
    on_step: StepEmitter | None = None,
) -> LoopResult:
    """Run one orchestrator session and return its result + session dir."""
    sd = state_dir or state_reader.default_state_dir()
    orch = Orchestrator(
        judge=judge, state_dir=sd, goal=goal, mode=mode,
        budget=budget or pol.Budget(), allowlist=allowlist, on_step=on_step,
    )
    return orch.run()


# ── Final-report rendering ─────────────────────────────────────────────


def _render_final_md(goal: str, final: Final, outcome: str) -> str:
    lines = [
        "# Orchestrator session — final report",
        "",
        f"**Goal:** {goal}",
        f"**Outcome:** `{outcome}`",
        "",
        "## Summary",
        "",
        final.summary,
        "",
    ]
    if final.recommended_next_steps:
        lines += ["## Recommended next steps", ""]
        lines += [f"- {s}" for s in final.recommended_next_steps]
        lines += [""]
    if final.commands:
        lines += ["## Commands", "", "```bash"]
        lines += final.commands
        lines += ["```", ""]
    if final.risks:
        lines += ["## Risks", ""]
        lines += [f"- {r}" for r in final.risks]
        lines += [""]
    if final.evidence_paths:
        lines += ["## Evidence", ""]
        lines += [f"- `{p}`" for p in final.evidence_paths]
        lines += [""]
    lines += [
        "## Doctrine reminders",
        "",
        "- Strict gold F1 is the model-accuracy metric (Rule 56).",
        "- Eval-agent candidate acceptance rate is audit/triage only.",
        "- `gemini-3.5-flash` is the judge default (Rule 55).",
    ]
    return "\n".join(lines)


def _render_incomplete_md(
    goal: str, outcome: str, observations: list[dict[str, Any]],
) -> str:
    lines = [
        "# Orchestrator session — incomplete",
        "",
        f"**Goal:** {goal}",
        f"**Outcome:** `{outcome}`",
        "",
        "Session ended before the LLM committed to a final report.",
        "Trace events and observations are below; rerun with a higher "
        "step budget or a less ambiguous goal.",
        "",
        "## Observations seen",
        "",
    ]
    for o in observations:
        lines.append(f"- `{o.get('tool')}` ok={o.get('ok')} — {o.get('summary')}")
    return "\n".join(lines) if observations else "\n".join(lines + ["(none)"])


# ── Heuristics ─────────────────────────────────────────────────────────


def _last_n_refusals_match(window: Iterable[dict[str, Any]], *, n: int) -> bool:
    """Return true when the last *n* observations were all policy refusals.

    Used by the loop to bail out of a "model keeps trying the same
    disallowed tool" pathology instead of running until the step cap.
    """
    last = list(window)[-n:]
    if len(last) < n:
        return False
    return all((o.get("ok") is False and
                str(o.get("error") or "").startswith(("tool_not_allowed",
                                                      "step_budget_",
                                                      "wallclock_budget_",
                                                      "usd_budget_")))
               for o in last)


__all__ = [
    "LLMFn",
    "LoopResult",
    "Orchestrator",
    "StubJudge",
    "run_session",
]

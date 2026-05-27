"""The agentic judging loop — a ReAct tool-loop over the Gemini judge.

Unlike the linear ``GeminiJudge.judge()`` single-shot, this lets the model
choose, per candidate, which evidence to gather (via tools) before it
commits a verdict, and escalates to a stronger model once when it stays
uncertain. Every step is recorded in a ``Trace`` for audit.

The loop is model-driven: tools are declared, the model decides whether to
call one or answer immediately. Trivial candidates cost a single turn.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable

from eval_agent.agentic.tools import ToolContext, ToolRegistry
from eval_agent.agentic.trace import Trace
from eval_agent.logging_setup import get_logger

if TYPE_CHECKING:
    from eval_agent.client.authority_client import AuthorityClient
    from eval_agent.client.gemini_client import GeminiJudge
    from eval_agent.evaluators._base import Candidate, Evaluator, Verdict

log = get_logger("eval_agent.agentic")

TokenSink = Callable[[int, int], None]

_ESCALATE_NUDGE = (
    "Your verdict was uncertain. If more evidence would help, call a tool to "
    "gather it; otherwise give a firmer verdict now. Return only the JSON verdict."
)
_FORCE_FINAL = (
    "You have used your evidence budget. Answer now with the JSON verdict only "
    "— no more tool calls. If you still cannot tell, set overall to 'abstain'."
)


class AgenticJudge:
    """Run the tool-loop for one candidate and return a verdict + trace."""

    def __init__(
        self,
        *,
        judge: "GeminiJudge",
        registry: ToolRegistry,
        marc_index: dict[str, dict[str, Any]],
        ner_index: dict[str, dict[str, Any]],
        agent_system_prompt: str,
        authority: "AuthorityClient | None" = None,
        max_steps: int = 6,
        escalate_model: str | None = None,
        escalate_on: tuple[str, ...] = ("abstain", "partial"),
        max_obs_chars: int = 4000,
    ) -> None:
        self._judge = judge
        self._registry = registry
        self._marc_index = marc_index
        self._ner_index = ner_index
        self._system = agent_system_prompt
        self._authority = authority
        self._max_steps = max(1, int(max_steps))
        self._escalate_model = escalate_model or None
        self._escalate_on = tuple(escalate_on)
        self._max_obs_chars = max_obs_chars

    def run(
        self,
        evaluator: "Evaluator",
        candidate: "Candidate",
        *,
        token_sink: TokenSink | None = None,
    ) -> tuple["Verdict", Trace]:
        ctx = ToolContext(
            record_id=candidate.record_id,
            marc_index=self._marc_index,
            ner_index=self._ner_index,
            authority=self._authority,
            max_chars=self._max_obs_chars,
        )
        trace = Trace(
            record_id=candidate.record_id,
            evaluator_id=candidate.evaluator_id,
            sub_type=candidate.sub_type,
        )
        tools = self._registry.declarations()
        contents: list[dict[str, Any]] = [
            _user_turn(self._system + "\n\n" + evaluator.build_prompt(candidate))
        ]
        model = self._judge.id
        escalated = False

        for _ in range(self._max_steps):
            resp = self._judge.generate_with_tools(
                contents=contents, tools=tools, model=model,
            )
            if token_sink is not None:
                token_sink(resp.input_tokens, resp.output_tokens)

            if resp.function_calls:
                contents.append(_model_function_call_turn(resp.function_calls))
                for call in resp.function_calls:
                    obs = self._registry.dispatch(call.name, call.args, ctx)
                    trace.add(tool=call.name, args=call.args, observation=obs)
                    contents.append(_function_response_turn(call.name, obs))
                continue

            if resp.error and resp.verdict is None:
                trace.add(tool=None, note=f"error: {resp.error}")
                break  # fall through to forced final

            verdict_dict = resp.verdict if resp.verdict is not None else {}
            overall = str(verdict_dict.get("overall", "")).lower()
            if (
                overall in self._escalate_on
                and not escalated
                and self._escalate_model
            ):
                escalated = True
                model = self._escalate_model
                contents.append(_model_text_turn(json.dumps(verdict_dict, ensure_ascii=False)))
                contents.append(_user_turn(_ESCALATE_NUDGE))
                trace.add(tool=None, note=f"escalate -> {model}")
                continue

            trace.final_model = model
            trace.escalated = escalated
            trace.add(tool=None, note="verdict")
            return self._verdict(evaluator, candidate, verdict_dict), trace

        # Budget exhausted or transport error — force a no-tools final verdict.
        contents.append(_user_turn(_FORCE_FINAL))
        final = self._judge.generate_with_tools(contents=contents, tools=[], model=model)
        if token_sink is not None:
            token_sink(final.input_tokens, final.output_tokens)
        trace.final_model = model
        trace.escalated = escalated
        trace.add(tool=None, note="forced-final")
        verdict_dict = final.verdict if final.verdict is not None else {}
        v = self._verdict(evaluator, candidate, verdict_dict)
        if final.error and not verdict_dict:
            v.error = final.error
        return v, trace

    def _verdict(
        self, evaluator: "Evaluator", candidate: "Candidate", raw: dict[str, Any],
    ) -> "Verdict":
        v = evaluator.parse_verdict(raw or None, candidate)
        v.judge_id = self._judge.id
        return v


# ── Gemini content-turn builders ────────────────────────────────────────


def _user_turn(text: str) -> dict[str, Any]:
    return {"role": "user", "parts": [{"text": text}]}


def _model_text_turn(text: str) -> dict[str, Any]:
    return {"role": "model", "parts": [{"text": text}]}


def _model_function_call_turn(calls: list[Any]) -> dict[str, Any]:
    return {
        "role": "model",
        "parts": [{"functionCall": {"name": c.name, "args": c.args}} for c in calls],
    }


def _function_response_turn(name: str, observation: str) -> dict[str, Any]:
    # v1beta generativelanguage: the functionResponse turn is carried in a
    # Content with role "user" (verify against the live API in smoke #4).
    return {
        "role": "user",
        "parts": [{"functionResponse": {"name": name, "response": {"result": observation}}}],
    }


__all__ = ["AgenticJudge"]

"""Tests for the AgenticJudge ReAct loop (scripted Gemini, no network)."""

from __future__ import annotations

from typing import Any

from eval_agent.agentic.loop import AgenticJudge
from eval_agent.agentic.tools import ToolRegistry
from eval_agent.client.gemini_client import ToolCall, ToolTurnResponse
from eval_agent.evaluators._base import Candidate, Verdict


class _StubEvaluator:
    """Minimal evaluator: build_prompt + parse_verdict, no MARC plumbing."""

    id = "person_ner"

    def build_prompt(self, candidate: Candidate) -> str:
        return f"judge: {candidate.payload}"

    def parse_verdict(self, raw: dict[str, Any] | None, candidate: Candidate) -> Verdict:
        v = Verdict(
            record_id=candidate.record_id,
            evaluator_id=candidate.evaluator_id,
            sub_type=candidate.sub_type,
            candidate_payload=dict(candidate.payload),
            confidence=candidate.confidence,
        )
        if raw:
            v.overall = str(raw.get("overall", "fail"))
            v.name_ok = str(raw.get("name_ok", "no"))
        return v


class _ScriptedGemini:
    """Returns queued ToolTurnResponses in order; records the models used."""

    id = "gemini-3.5-flash"

    def __init__(self, turns: list[ToolTurnResponse]) -> None:
        self._turns = list(turns)
        self.models_used: list[str] = []
        self.calls = 0

    def generate_with_tools(self, *, contents, tools, model=None, timeout=120):  # noqa: ANN001
        self.calls += 1
        self.models_used.append(model or self.id)
        if self._turns:
            return self._turns.pop(0)
        return ToolTurnResponse(function_calls=[], verdict={"overall": "fail"},
                                raw_text=None, error=None)


def _candidate() -> Candidate:
    return Candidate(
        record_id="990001", evaluator_id="person_ner", sub_type="AUTHOR",
        payload={"text": "קארו"}, confidence=0.85,
    )


def _agent(gemini: Any, **kw: Any) -> AgenticJudge:
    marc = {"990001": {"_control_number": "990001", "title": "x", "notes": ["f.mrc", "note"]}}
    ner = {"990001": {"_control_number": "990001", "entities": []}}
    return AgenticJudge(
        judge=gemini,
        registry=ToolRegistry(["fetch_marc_field", "expand_note"]),
        marc_index=marc, ner_index=ner,
        agent_system_prompt="SYSTEM",
        max_steps=kw.get("max_steps", 6),
        escalate_model=kw.get("escalate_model"),
        escalate_on=kw.get("escalate_on", ("abstain", "partial")),
    )


def test_immediate_verdict_no_tools() -> None:
    gem = _ScriptedGemini([
        ToolTurnResponse(function_calls=[], verdict={"overall": "full", "name_ok": "yes"},
                         raw_text=None, error=None),
    ])
    v, trace = _agent(gem).run(_StubEvaluator(), _candidate())
    assert v.overall == "full"
    assert trace.tools_used() == []      # answered without tools
    assert gem.calls == 1


def test_two_tool_calls_then_verdict() -> None:
    gem = _ScriptedGemini([
        ToolTurnResponse(function_calls=[ToolCall("fetch_marc_field", {"field": "title"})],
                         verdict=None, raw_text=None, error=None),
        ToolTurnResponse(function_calls=[ToolCall("expand_note", {})],
                         verdict=None, raw_text=None, error=None),
        ToolTurnResponse(function_calls=[], verdict={"overall": "full"},
                         raw_text=None, error=None),
    ])
    v, trace = _agent(gem).run(_StubEvaluator(), _candidate())
    assert v.overall == "full"
    assert trace.tools_used() == ["fetch_marc_field", "expand_note"]
    assert gem.calls == 3


def test_escalation_on_abstain() -> None:
    gem = _ScriptedGemini([
        ToolTurnResponse(function_calls=[], verdict={"overall": "abstain"},
                         raw_text=None, error=None),   # tier-1 uncertain
        ToolTurnResponse(function_calls=[], verdict={"overall": "full"},
                         raw_text=None, error=None),   # after escalation
    ])
    agent = _agent(gem, escalate_model="gemini-3.1-pro-preview")
    v, trace = agent.run(_StubEvaluator(), _candidate())
    assert v.overall == "full"
    assert trace.escalated is True
    assert trace.final_model == "gemini-3.1-pro-preview"
    # first call on tier-1, second on the escalated model
    assert gem.models_used == ["gemini-3.5-flash", "gemini-3.1-pro-preview"]


def test_escalation_happens_at_most_once() -> None:
    gem = _ScriptedGemini([
        ToolTurnResponse(function_calls=[], verdict={"overall": "abstain"}, raw_text=None, error=None),
        ToolTurnResponse(function_calls=[], verdict={"overall": "abstain"}, raw_text=None, error=None),
        ToolTurnResponse(function_calls=[], verdict={"overall": "partial"}, raw_text=None, error=None),
    ])
    agent = _agent(gem, escalate_model="gemini-3.1-pro-preview", max_steps=6)
    v, trace = agent.run(_StubEvaluator(), _candidate())
    # escalated once; the second abstain is accepted (no re-escalation)
    assert trace.escalated is True
    assert gem.models_used.count("gemini-3.1-pro-preview") >= 1
    assert v.overall == "abstain"


def test_budget_exhaustion_forces_final() -> None:
    # Always asks for a tool → never answers within budget → forced final.
    loop_turns = [
        ToolTurnResponse(function_calls=[ToolCall("expand_note", {})],
                         verdict=None, raw_text=None, error=None)
        for _ in range(10)
    ]
    gem = _ScriptedGemini(loop_turns)
    agent = _agent(gem, max_steps=3)
    v, trace = agent.run(_StubEvaluator(), _candidate())
    # 3 loop steps + 1 forced-final call
    assert gem.calls == 4
    assert any(s.note == "forced-final" for s in trace.steps)
    assert isinstance(v, Verdict)

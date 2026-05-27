"""Tests for GeminiJudge.generate_with_tools (mocked HTTP)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from eval_agent.client.gemini_client import GeminiJudge
from eval_agent.client.rate_limiter import RateLimiter


def _judge(model: str = "gemini-3.5-flash") -> GeminiJudge:
    return GeminiJudge(model=model, api_key="k", rate_limiter=RateLimiter(1000))


def _patch_post(monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]) -> dict[str, Any]:
    """Patch GeminiJudge._post to return *response*; capture the payload + url."""
    captured: dict[str, Any] = {}

    def fake_post(self, payload, *, url, timeout):  # noqa: ANN001
        captured["payload"] = payload
        captured["url"] = url
        return response

    monkeypatch.setattr(GeminiJudge, "_post", fake_post)
    return captured


_DECLS = [{"functionDeclarations": [{"name": "fetch_marc_field", "parameters": {"type": "object"}}]}]


def test_single_function_call(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = {
        "candidates": [{"content": {"parts": [
            {"functionCall": {"name": "fetch_marc_field", "args": {"field": "title"}}}
        ]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4},
    }
    _patch_post(monkeypatch, resp)
    out = _judge().generate_with_tools(contents=[], tools=_DECLS)
    assert len(out.function_calls) == 1
    assert out.function_calls[0].name == "fetch_marc_field"
    assert out.function_calls[0].args == {"field": "title"}
    assert out.verdict is None
    assert out.input_tokens == 10 and out.output_tokens == 4


def test_two_function_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "expand_note", "args": {}}},
        {"functionCall": {"name": "list_record_entities", "args": {}}},
    ]}}], "usageMetadata": {}}
    _patch_post(monkeypatch, resp)
    out = _judge().generate_with_tools(contents=[], tools=_DECLS)
    assert [c.name for c in out.function_calls] == ["expand_note", "list_record_entities"]


def test_text_json_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    verdict = {"overall": "full", "name_ok": "yes"}
    resp = {"candidates": [{"content": {"parts": [
        {"text": json.dumps(verdict)}
    ]}}], "usageMetadata": {}}
    _patch_post(monkeypatch, resp)
    out = _judge().generate_with_tools(contents=[], tools=_DECLS)
    assert out.verdict == verdict
    assert out.function_calls == []


def test_http_error_surfaces_not_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self, payload, *, url, timeout):  # noqa: ANN001
        raise RuntimeError("HTTP 500: boom")
    monkeypatch.setattr(GeminiJudge, "_post", boom)
    out = _judge().generate_with_tools(contents=[], tools=_DECLS)
    assert out.error is not None and "boom" in out.error
    assert out.function_calls == [] and out.verdict is None


def test_model_override_in_url_and_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _patch_post(monkeypatch, {"candidates": [{"content": {"parts": [{"text": "{}"}]}}],
                                    "usageMetadata": {}})
    _judge(model="gemini-3.5-flash").generate_with_tools(
        contents=[], tools=_DECLS, model="gemini-3.1-pro-preview",
    )
    assert "gemini-3.1-pro-preview" in cap["url"]
    # 3.x thinking config uses thinkingLevel
    assert cap["payload"]["generationConfig"].get("thinkingConfig") == {"thinkingLevel": "low"}
    # tools + AUTO mode present
    assert cap["payload"]["tools"] == _DECLS
    assert cap["payload"]["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"


def test_judge_method_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linear judge() still works through the refactored _post."""
    resp = {"candidates": [{"content": {"parts": [{"text": '{"overall":"full"}'}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2}}
    _patch_post(monkeypatch, resp)
    r = _judge().judge(prompt="p", schema={"type": "object", "properties": {}})
    assert r.verdict == {"overall": "full"}
    assert r.input_tokens == 3 and r.output_tokens == 2

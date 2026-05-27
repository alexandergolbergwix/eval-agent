"""Gemini judge — implements the ``Judge`` interface for Gemini 3.x.

REST surface: ``generativelanguage.googleapis.com/v1beta`` with
``x-goog-api-key`` header. Lifts the proven shape from the parent
pipeline's evaluation script:

- Flat 2.x-style structured-output config (``responseMimeType`` +
  ``responseSchema`` directly under ``generationConfig``). The newer
  ``responseFormat.text.schema`` form advertised by Gemini 3 docs
  is not yet implemented by v1beta as of 2026-05.
- ``thinkingLevel: "low"`` for Gemini 3.x (replaces 2.x ``thinkingBudget``).
- Hard rate limit via injected ``RateLimiter``; retry-on-429 is the
  fallback, not the primary defence.

Token usage is reported in the response when available so the
orchestration layer can write cost telemetry into the run manifest.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from eval_agent.client.judge_interface import Judge, JudgeResponse
from eval_agent.client.rate_limiter import RateLimiter
from eval_agent.logging_setup import get_logger, redact, truncate

log = get_logger("eval_agent.gemini")

_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)


@dataclass
class ToolCall:
    """One function-call the model emitted in a tool-use turn."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolTurnResponse:
    """One round-trip of a multi-turn tool-use conversation.

    Either ``function_calls`` is non-empty (the model wants evidence) OR
    ``verdict`` / ``raw_text`` is set (the model answered). The caller owns
    the conversation history and loops.
    """

    function_calls: list[ToolCall]
    verdict: dict | None
    raw_text: str | None
    error: str | None
    input_tokens: int = 0
    output_tokens: int = 0


class GeminiJudge:
    """Concrete ``Judge`` for Gemini 3.x."""

    id: str

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        rate_limiter: RateLimiter,
        thinking_level: str = "low",
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
        top_p: float = 0.95,
        max_retries: int = 6,
        retry_base_seconds: int = 5,
    ) -> None:
        if not api_key:
            raise ValueError("Gemini API key required")
        self.id = model
        self._api_key = api_key
        self._rate_limiter = rate_limiter
        self._thinking_level = thinking_level
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._top_p = top_p
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        log.debug(
            "init model=%s api_key=%s thinking=%s max_out=%d temp=%s top_p=%s rpm_limiter=%r",
            model, redact(api_key), thinking_level, max_output_tokens,
            temperature, top_p, rate_limiter,
        )

    # ── Public API ────────────────────────────────────────────────────

    def judge(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        timeout: int = 120,
    ) -> JudgeResponse:
        """Send prompt + schema; return parsed verdict (or error)."""
        payload = self._payload(prompt, schema)
        log.debug(
            "judge.request model=%s prompt_chars=%d schema_keys=%s",
            self.id, len(prompt), sorted(payload["generationConfig"]["responseSchema"].keys()),
        )
        try:
            raw_text, in_tok, out_tok = self._call(payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            log.debug("judge.transport_error %s", truncate(str(exc), 400))
            return JudgeResponse(
                verdict=None, raw_text=None, error=str(exc), judge_id=self.id,
            )

        verdict, parse_err = self._parse(raw_text)
        if parse_err:
            log.debug("judge.parse_error err=%s raw=%s",
                      truncate(parse_err, 200), truncate(raw_text, 200))
        else:
            log.debug(
                "judge.response in_tok=%s out_tok=%s overall=%s",
                in_tok, out_tok,
                (verdict or {}).get("overall") if isinstance(verdict, dict) else None,
            )
        return JudgeResponse(
            verdict=verdict,
            raw_text=raw_text,
            error=parse_err,
            judge_id=self.id,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )

    # ── Internals ─────────────────────────────────────────────────────

    def _payload(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        gen_cfg: dict[str, Any] = {
            "temperature": self._temperature,
            "topP": self._top_p,
            "maxOutputTokens": self._max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": _sanitize_schema_for_gemini(schema),
        }
        thinking = _thinking_config_for(self.id, self._thinking_level)
        if thinking is not None:
            gen_cfg["thinkingConfig"] = thinking
        return {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": gen_cfg,
        }

    def _call(
        self, payload: dict[str, Any], *, timeout: int,
    ) -> tuple[str, int | None, int | None]:
        url = _ENDPOINT.format(model=self.id)
        data = self._post(payload, url=url, timeout=timeout)
        return _extract_text_and_usage(data)

    def _post(
        self, payload: dict[str, Any], *, url: str, timeout: int,
    ) -> dict[str, Any]:
        """HTTP POST with rate-limiting + retry/backoff; return parsed JSON.

        Shared by ``judge()`` (via ``_call``) and ``generate_with_tools``.
        Raises ``RuntimeError`` on exhausted retries / non-429 HTTP errors.
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(self._max_retries):
            # Rate-limiter blocks per-attempt so 429s become impossible
            # in steady state. Retries beyond the limiter are for
            # transient network issues only.
            self._rate_limiter.acquire()
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self._api_key,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="ignore")
                log.debug("http_error code=%d attempt=%d body=%s",
                          exc.code, attempt + 1, truncate(body_text, 300))
                if exc.code == 429 and attempt < self._max_retries - 1:
                    wait = self._retry_base_seconds * (2 ** attempt)
                    time.sleep(wait)
                    last_err = RuntimeError(f"HTTP 429 (retried after {wait}s): {body_text[:200]}")
                    continue
                raise RuntimeError(f"HTTP {exc.code}: {body_text[:500]}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                log.debug("transient_network attempt=%d exc=%s",
                          attempt + 1, truncate(str(exc), 200))
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_base_seconds * (2 ** attempt))
                    last_err = RuntimeError(f"transient network: {exc}")
                    continue
                raise RuntimeError(f"network error: {exc}") from exc
        # Defensive — loop should always exit via return or raise above
        raise RuntimeError(f"max retries exhausted: {last_err}")

    # ── Tool-use (agentic) surface ─────────────────────────────────────

    def generate_with_tools(
        self,
        *,
        contents: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
        model: str | None = None,
        timeout: int = 120,
    ) -> ToolTurnResponse:
        """One tool-use round-trip. Stateless — caller owns the history.

        Returns ``function_calls`` when the model wants tools, else a parsed
        ``verdict`` (when ``response_schema`` is set and the text is JSON) or
        ``raw_text``. Never raises — transport/parse failures surface in
        ``error``.

        Note: Gemini's v1beta does not reliably honour ``responseSchema`` and
        ``tools`` simultaneously, so when tools are present we do NOT send
        ``responseSchema``; the model answers with a JSON text part that we
        parse against the caller's intent. The agent system prompt instructs
        the exact verdict shape.
        """
        resolved = model or self.id
        url = _ENDPOINT.format(model=resolved)
        gen_cfg: dict[str, Any] = {
            "temperature": self._temperature,
            "topP": self._top_p,
            "maxOutputTokens": self._max_output_tokens,
        }
        thinking = _thinking_config_for(resolved, self._thinking_level)
        if thinking is not None:
            gen_cfg["thinkingConfig"] = thinking
        payload: dict[str, Any] = {
            "contents": contents,
            "tools": tools,
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
            "generationConfig": gen_cfg,
        }
        try:
            data = self._post(payload, url=url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            log.debug("tools.transport_error %s", truncate(str(exc), 400))
            return ToolTurnResponse(
                function_calls=[], verdict=None, raw_text=None, error=str(exc),
            )
        return _parse_tool_turn(data)

    def _parse(self, raw_text: str) -> tuple[dict[str, Any] | None, str | None]:
        text = raw_text.strip()
        # Defensive: strip code fences if Gemini ignored responseSchema
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                return None, f"PARSE_ERROR: response is not a JSON object: {text[:200]}"
            return parsed, None
        except json.JSONDecodeError as exc:
            return None, f"PARSE_ERROR: {exc}: {text[:200]}"


# ``thinkingConfig`` keys differ across Gemini generations:
#
#   3.x  →  thinkingLevel:  "low" | "high"
#   2.5  →  thinkingBudget: int (0 = no thinking, positive = budget in tokens)
#   2.0 and older → no thinking support, omit the block entirely
#
# Mis-applying these triggers HTTP 400 "Thinking level/budget is not supported
# for this model." Resolve from the model id at request-build time.

_THINKING_LEVEL_TO_BUDGET = {"low": 0, "medium": 1024, "high": 24576}


def _thinking_config_for(model_id: str, level: str) -> dict[str, Any] | None:
    name = model_id.lower()
    if name.startswith("gemini-3"):
        return {"thinkingLevel": level}
    if name.startswith("gemini-2.5"):
        return {"thinkingBudget": _THINKING_LEVEL_TO_BUDGET.get(level, 0)}
    # gemini-2.0 and older — no thinking support.
    return None


# Gemini's ``responseSchema`` accepts a small OpenAPI-style subset of JSON
# Schema. Draft-2020-12 keywords like ``$schema``, ``$id``,
# ``additionalProperties``, ``const``, ``pattern``, ``minimum``, ``maximum``
# cause HTTP 400 "Unknown name" errors. Strip them recursively before sending.
_GEMINI_UNSUPPORTED_KEYS = frozenset({
    "$schema", "$id", "$ref", "$defs", "definitions",
    "additionalProperties", "const", "pattern",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minItems", "maxItems", "minLength", "maxLength",
    "title", "examples",
})


def _sanitize_schema_for_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``schema`` with Gemini-incompatible keys removed."""
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in _GEMINI_UNSUPPORTED_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _sanitize_schema_for_gemini(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = _sanitize_schema_for_gemini(v)
        elif isinstance(v, dict):
            out[k] = _sanitize_schema_for_gemini(v)
        elif isinstance(v, list):
            out[k] = [_sanitize_schema_for_gemini(item) if isinstance(item, dict) else item
                      for item in v]
        else:
            out[k] = v
    return out


def _extract_text_and_usage(
    data: dict[str, Any],
) -> tuple[str, int | None, int | None]:
    """Pull the response text + token counts out of a Gemini response."""
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"no candidates in response: {data}")
    parts = candidates[0].get("content", {}).get("parts") or []
    if not parts:
        finish = candidates[0].get("finishReason", "?")
        raise RuntimeError(
            f"no parts in candidate (finishReason={finish}): {candidates[0]}"
        )
    text = parts[0].get("text", "")
    usage = data.get("usageMetadata") or {}
    return text, usage.get("promptTokenCount"), usage.get("candidatesTokenCount")


def _parse_tool_turn(data: dict[str, Any]) -> ToolTurnResponse:
    """Parse a tool-use Gemini response into function calls / verdict / text."""
    usage = data.get("usageMetadata") or {}
    in_tok = usage.get("promptTokenCount") or 0
    out_tok = usage.get("candidatesTokenCount") or 0
    candidates = data.get("candidates") or []
    if not candidates:
        return ToolTurnResponse(
            function_calls=[], verdict=None, raw_text=None,
            error=f"no candidates in response: {truncate(json.dumps(data), 300)}",
            input_tokens=in_tok, output_tokens=out_tok,
        )
    parts = candidates[0].get("content", {}).get("parts") or []
    calls: list[ToolCall] = []
    text_chunks: list[str] = []
    for part in parts:
        fc = part.get("functionCall")
        if isinstance(fc, dict) and fc.get("name"):
            args = fc.get("args")
            calls.append(ToolCall(name=str(fc["name"]), args=dict(args) if isinstance(args, dict) else {}))
            continue
        txt = part.get("text")
        if isinstance(txt, str) and txt.strip():
            text_chunks.append(txt)
    if calls:
        return ToolTurnResponse(
            function_calls=calls, verdict=None, raw_text=None, error=None,
            input_tokens=in_tok, output_tokens=out_tok,
        )
    raw = "\n".join(text_chunks).strip()
    verdict, _err = _parse_json_object(raw) if raw else (None, None)
    if not raw:
        finish = candidates[0].get("finishReason", "?")
        return ToolTurnResponse(
            function_calls=[], verdict=None, raw_text=None,
            error=f"no functionCall and no text (finishReason={finish})",
            input_tokens=in_tok, output_tokens=out_tok,
        )
    return ToolTurnResponse(
        function_calls=[], verdict=verdict, raw_text=raw, error=None,
        input_tokens=in_tok, output_tokens=out_tok,
    )


def _parse_json_object(raw_text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a JSON object out of model text, tolerating ```json fences."""
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"PARSE_ERROR: {exc}: {text[:200]}"
    if not isinstance(parsed, dict):
        return None, f"PARSE_ERROR: not a JSON object: {text[:200]}"
    return parsed, None

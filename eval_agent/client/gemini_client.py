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
from typing import Any

from eval_agent.client.judge_interface import Judge, JudgeResponse
from eval_agent.client.rate_limiter import RateLimiter

_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)


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
        try:
            raw_text, in_tok, out_tok = self._call(payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return JudgeResponse(
                verdict=None, raw_text=None, error=str(exc), judge_id=self.id,
            )

        verdict, parse_err = self._parse(raw_text)
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
        return {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self._temperature,
                "topP": self._top_p,
                "thinkingConfig": {"thinkingLevel": self._thinking_level},
                "maxOutputTokens": self._max_output_tokens,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }

    def _call(
        self, payload: dict[str, Any], *, timeout: int,
    ) -> tuple[str, int | None, int | None]:
        url = _ENDPOINT.format(model=self.id)
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
                    data = json.loads(resp.read().decode("utf-8"))
                return _extract_text_and_usage(data)
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="ignore")
                if exc.code == 429 and attempt < self._max_retries - 1:
                    wait = self._retry_base_seconds * (2 ** attempt)
                    time.sleep(wait)
                    last_err = RuntimeError(f"HTTP 429 (retried after {wait}s): {body_text[:200]}")
                    continue
                raise RuntimeError(f"HTTP {exc.code}: {body_text[:500]}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_base_seconds * (2 ** attempt))
                    last_err = RuntimeError(f"transient network: {exc}")
                    continue
                raise RuntimeError(f"network error: {exc}") from exc
        # Defensive — loop should always exit via return or raise above
        raise RuntimeError(f"max retries exhausted: {last_err}")

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

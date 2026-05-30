"""Thin adapter: wrap ``GeminiJudge.judge`` as the orchestrator's ``LLMFn``.

The orchestrator loop takes an ``LLMFn`` callable ``(prompt: str) -> dict``.
``GeminiJudge.judge(prompt=..., schema=...)`` is close to that shape but
returns a ``JudgeResponse`` and threads the schema as a keyword arg.
This adapter binds the schema (the orchestrator's ``ACTION_SCHEMA``) and
unwraps the response — keeping the loop ignorant of the Gemini client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from eval_agent.client.gemini_client import GeminiJudge
from eval_agent.client.rate_limiter import RateLimiter
from eval_agent.orchestrator.schemas import ACTION_SCHEMA


# Default judge model (Rule 55 — gemini-3.5-flash is the project default).
# Switching the default is allowed only through an explicit CLI flag so
# the orchestrator's choice is audited.
DEFAULT_JUDGE_MODEL = "gemini-3.5-flash"


@dataclass
class GeminiLLM:
    """Callable adapter from GeminiJudge → LLMFn."""

    judge: GeminiJudge
    # Telemetry the loop can read after run() (token accounting that
    # later phases will fold into USD budgets).
    input_tokens:  int = 0
    output_tokens: int = 0

    def __call__(self, prompt: str) -> dict[str, Any]:
        resp = self.judge.judge(prompt=prompt, schema=ACTION_SCHEMA, timeout=120)
        self.input_tokens  += resp.input_tokens or 0
        self.output_tokens += resp.output_tokens or 0
        if resp.verdict is None:
            # Surface the transport / parse error so the loop's
            # ``llm.error`` event has a useful message AND so the LLMFn
            # contract (return a dict) holds — we return a malformed
            # dict that ACTION_SCHEMA rejection will catch + abort.
            return {"thought_summary": f"gemini error: {resp.error}",
                    "final": False, "action": {"tool": "", "args": {}}}
        return resp.verdict


def build_gemini_llm(
    *,
    api_key: str,
    model: str = DEFAULT_JUDGE_MODEL,
    rpm: int = 60,
    thinking_level: str = "low",
) -> GeminiLLM:
    """Construct a ready-to-use Gemini LLMFn for the orchestrator."""
    limiter = RateLimiter(rpm=rpm)
    judge = GeminiJudge(
        model=model, api_key=api_key, rate_limiter=limiter,
        thinking_level=thinking_level,
    )
    return GeminiLLM(judge=judge)


__all__ = ["DEFAULT_JUDGE_MODEL", "GeminiLLM", "build_gemini_llm"]

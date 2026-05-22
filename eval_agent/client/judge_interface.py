"""Abstract Judge interface.

A Judge takes a prompt + a JSON Schema for the expected response and
returns the parsed verdict dict. Implementations:

- ``GeminiJudge`` — Google Gemini 3.x via the generativelanguage v1beta
  REST API.
- Future: ``ClaudeJudge``, ``OpenAIJudge`` — share this interface so
  the rest of the agent never knows which judge is running.

The interface intentionally keeps "what to ask" (the prompt) separate
from "what shape to return" (the schema). This lets the verdict
schema evolve independently of any specific evaluator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class JudgeResponse:
    """Result of a single judge call."""

    verdict: dict[str, Any] | None
    raw_text: str | None
    error: str | None
    judge_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class Judge(Protocol):
    """Anything that can render a verdict from a prompt + schema."""

    id: str

    def judge(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        timeout: int = 120,
    ) -> JudgeResponse:
        ...

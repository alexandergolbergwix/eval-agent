"""Shared pytest fixtures + MockJudge for e2e tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from eval_agent.client.judge_interface import Judge, JudgeResponse

FIXTURES = Path(__file__).parent / "fixtures"


# ──────────────────────────────────────────────────────────────────────────────
# Fixture: pipeline output dir (tmp copy of fixtures so tests don't share state)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def pipeline_output(tmp_path: Path) -> Path:
    """Copy fixture marc_extracted.json + ner_results.json into a tmp dir."""
    out = tmp_path / "pipeline_work"
    out.mkdir()
    shutil.copy(FIXTURES / "marc_extracted.json", out / "marc_extracted.json")
    shutil.copy(FIXTURES / "ner_results.json", out / "ner_results.json")
    return out


# ──────────────────────────────────────────────────────────────────────────────
# MockJudge — deterministic verdict generator + call recorder
# ──────────────────────────────────────────────────────────────────────────────


class MockJudge:
    """In-process Judge implementation for hermetic e2e tests.

    Records every ``judge`` call so tests can assert call counts +
    prompts. Returns deterministic verdicts driven by a configurable
    strategy:

    - ``"always_full"``    — every verdict is "full" / "yes" / "yes"
    - ``"always_fail"``    — every verdict is "fail" / "no" / "no"
    - ``"by_keyword"``     — looks for known-good keywords in the prompt
                              and returns "full" if found, else "fail"
    - ``"by_evaluator"``   — uses the evaluator_id (read out of the
                              prompt) to assign a deterministic pattern

    Optionally a ``response_override`` mapping ``{prompt_substring → dict}``
    can force specific verdicts for specific prompts.
    """

    def __init__(
        self,
        *,
        id: str = "mock-judge-v1",
        strategy: str = "by_keyword",
        response_override: dict[str, dict[str, Any]] | None = None,
        error_on_substring: str | None = None,
    ) -> None:
        self.id = id
        self._strategy = strategy
        self._overrides = response_override or {}
        self._error_on = error_on_substring
        self.calls: list[dict[str, Any]] = []

    def judge(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        timeout: int = 120,
    ) -> JudgeResponse:
        self.calls.append({
            "prompt": prompt,
            "schema": schema,
            "timeout": timeout,
        })

        # Error injection — simulate Gemini transient failures
        if self._error_on and self._error_on in prompt:
            return JudgeResponse(
                verdict=None, raw_text=None,
                error=f"INJECTED_ERROR: contains {self._error_on!r}",
                judge_id=self.id,
            )

        # Per-prompt override
        for needle, verdict in self._overrides.items():
            if needle in prompt:
                return JudgeResponse(
                    verdict=verdict, raw_text=json.dumps(verdict),
                    error=None, judge_id=self.id,
                    input_tokens=120, output_tokens=40,
                )

        verdict = self._verdict_for(prompt)
        return JudgeResponse(
            verdict=verdict, raw_text=json.dumps(verdict),
            error=None, judge_id=self.id,
            input_tokens=120, output_tokens=40,
        )

    def _verdict_for(self, prompt: str) -> dict[str, str]:
        if self._strategy == "always_full":
            return {"name_ok": "yes", "type_ok": "yes",
                    "role_ok": "yes" if "Person NER" in prompt else "n/a",
                    "overall": "full", "reasoning": "mock: always_full"}
        if self._strategy == "always_fail":
            return {"name_ok": "no", "type_ok": "no", "role_ok": "no",
                    "overall": "fail", "reasoning": "mock: always_fail"}
        if self._strategy == "by_evaluator":
            # Person NER → partial (name yes, role no), others → full
            if "Person NER" in prompt:
                return {"name_ok": "yes", "type_ok": "yes", "role_ok": "no",
                        "overall": "partial",
                        "reasoning": "mock: person ner role often wrong"}
            return {"name_ok": "yes", "type_ok": "yes", "role_ok": "n/a",
                    "overall": "full", "reasoning": "mock: by_evaluator full"}
        # Default: by_keyword — look at prompt for hints
        # Person NER prompt contains "Person NER"; provenance "Provenance NER"; etc.
        if "Person NER" in prompt and "TRANSCRIBER" in prompt:
            return {"name_ok": "yes", "type_ok": "yes", "role_ok": "yes",
                    "overall": "full", "reasoning": "mock: transcriber match"}
        if "Person NER" in prompt:
            return {"name_ok": "yes", "type_ok": "yes", "role_ok": "no",
                    "overall": "partial", "reasoning": "mock: role mismatch"}
        return {"name_ok": "yes", "type_ok": "yes",
                "role_ok": "n/a", "overall": "full",
                "reasoning": "mock: default-full"}


@pytest.fixture
def mock_judge() -> MockJudge:
    return MockJudge()


# ──────────────────────────────────────────────────────────────────────────────
# Fixture: isolated state dirs (cache, runs, progress) under tmp_path
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def state_paths(tmp_path: Path) -> dict[str, Path]:
    """Return cache_path + runs_dir + progress_path under tmp_path."""
    cache = tmp_path / "state" / "cache" / "verdict_cache.jsonl"
    runs = tmp_path / "state" / "runs"
    prog = tmp_path / "state" / "progress.md"
    # Ensure parent directories exist so tests can write peer files (e.g. feature_list.json)
    cache.parent.mkdir(parents=True, exist_ok=True)
    runs.mkdir(parents=True, exist_ok=True)
    return {"cache_path": cache, "runs_dir": runs, "progress_path": prog}

"""Linear-vs-gated parity: --linear reproduces the single-shot behaviour.

In gated agentic mode the tool-loop fires only on abstain/partial tier-1
verdicts. A MockJudge that never returns those (always_full / always_fail)
must therefore produce verdicts identical to linear mode — proving the
default agentic path doesn't perturb the easy cases.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from eval_agent.orchestration.session import Session, SessionConfig
from tests.conftest import MockJudge


def _config(pipeline_output: Path, *, mode: str) -> SessionConfig:
    return SessionConfig(
        pipeline_output=pipeline_output,
        threshold=0.85,
        rpm=999,
        parallel=1,
        judge_model="mock-judge-v1",
        evaluators=["person_ner", "provenance_ner", "contents_ner", "genre_classifier"],
        api_key="dummy",
        dry_run=False,
        mode=mode,
    )


def _overalls(pipeline_output: Path, state_paths: dict[str, Path], *, mode: str) -> dict:
    judge = MockJudge(id="mock-judge-v1", strategy="by_keyword")
    session = Session(_config(pipeline_output, mode=mode), judge=judge, **state_paths)
    session.startup()
    verdicts = session.execute()
    # Key by (record_id, evaluator_id, sub_type, candidate text) for a stable join.
    out = {}
    for v in verdicts:
        text = v.candidate_payload.get("person") or v.candidate_payload.get("text") or ""
        out[(v.record_id, v.evaluator_id, v.sub_type, text)] = v.overall
    return out


def test_linear_and_gated_agree_when_no_uncertainty(
    pipeline_output: Path, state_paths: dict[str, Path], tmp_path: Path,
) -> None:
    linear = _overalls(pipeline_output, _fresh_state(tmp_path, "lin"), mode="linear")
    gated = _overalls(pipeline_output, _fresh_state(tmp_path, "gat"), mode="agentic")
    assert linear == gated
    assert len(linear) > 0


def test_linear_verdicts_not_marked_agentic(
    pipeline_output: Path, state_paths: dict[str, Path],
) -> None:
    judge = MockJudge(id="mock-judge-v1", strategy="always_full")
    session = Session(_config(pipeline_output, mode="linear"), judge=judge, **state_paths)
    session.startup()
    verdicts = session.execute()
    assert verdicts and all(not v.agentic for v in verdicts)


def _fresh_state(tmp_path: Path, tag: str) -> dict[str, Path]:
    base = tmp_path / tag
    runs = base / "runs"
    runs.mkdir(parents=True)
    return {
        "cache_path": base / "cache" / "verdict_cache.jsonl",
        "runs_dir": runs,
        "progress_path": base / "progress.md",
    }

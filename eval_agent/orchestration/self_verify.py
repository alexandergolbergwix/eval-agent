"""5% re-judge consistency loop.

After a Worker session completes its main judging pass, the
``SelfVerifier`` re-asks the configured judge for a deterministic sample
of the already-rendered verdicts, then compares each new ``overall``
against the original. If agreement drops below the configured floor
(default 0.95), the session is treated as suspect.

The re-judge call appends a salt suffix to the prompt so the
on-disk verdict cache cannot short-circuit the call: every salted
prompt is a fresh cache key.

Artefact:
    ``<run_dir>/self_verify.json`` — flat dict shaped like
    ``SelfVerifyResult`` (plus ``run_id``) that callers can consume
    without re-importing the dataclass.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval_agent.client.judge_interface import Judge
from eval_agent.evaluators import REGISTRY
from eval_agent.evaluators._base import Candidate, Verdict

REPO_ROOT = Path(__file__).resolve().parents[2]
VERDICT_SCHEMA_PATH = REPO_ROOT / "config" / "schemas" / "verdict.v1.json"

_SALT_SUFFIX = "\n\n[self-verify pass]"


@dataclass
class SelfVerifyResult:
    """Outcome of one self-verify pass over a Worker run."""

    sample_size: int
    agreements: int
    disagreements: int
    agreement_rate: float
    passed: bool
    agreement_floor: float
    run_id: str


class SelfVerifier:
    """Re-judge a deterministic fraction of a session's verdicts."""

    def __init__(
        self,
        *,
        sample_rate: float = 0.05,
        agreement_floor: float = 0.95,
        seed: int = 1337,
    ) -> None:
        if not 0.0 < sample_rate <= 1.0:
            raise ValueError(
                f"sample_rate must be in (0, 1], got {sample_rate!r}"
            )
        if not 0.0 <= agreement_floor <= 1.0:
            raise ValueError(
                f"agreement_floor must be in [0, 1], got {agreement_floor!r}"
            )
        self._sample_rate = sample_rate
        self._agreement_floor = agreement_floor
        self._seed = seed

    # ── Public API ────────────────────────────────────────────────────

    def run(
        self,
        verdicts: list[Verdict],
        *,
        judge: Judge,
        run_dir: Path,
    ) -> SelfVerifyResult:
        run_id = run_dir.name
        schema = _load_schema()
        sample = self._sample(verdicts)

        agreements = 0
        disagreement_records: list[dict[str, Any]] = []

        for original in sample:
            redo_overall = self._rejudge(original, judge=judge, schema=schema)
            if redo_overall is not None and redo_overall == original.overall:
                agreements += 1
            else:
                disagreement_records.append({
                    "record_id": original.record_id,
                    "evaluator_id": original.evaluator_id,
                    "sub_type": original.sub_type,
                    "original_overall": original.overall,
                    "redo_overall": redo_overall,
                })

        sample_size = len(sample)
        disagreements = sample_size - agreements
        agreement_rate = (agreements / sample_size) if sample_size > 0 else 1.0
        passed = agreement_rate >= self._agreement_floor

        result = SelfVerifyResult(
            sample_size=sample_size,
            agreements=agreements,
            disagreements=disagreements,
            agreement_rate=agreement_rate,
            passed=passed,
            agreement_floor=self._agreement_floor,
            run_id=run_id,
        )

        self._write_artifact(
            run_dir=run_dir,
            result=result,
            disagreement_records=disagreement_records,
        )
        return result

    # ── Internals ─────────────────────────────────────────────────────

    def _sample(self, verdicts: list[Verdict]) -> list[Verdict]:
        if not verdicts:
            return []
        if self._sample_rate >= 1.0:
            return list(verdicts)
        k = max(1, int(round(len(verdicts) * self._sample_rate)))
        k = min(k, len(verdicts))
        rng = random.Random(self._seed)
        return rng.sample(verdicts, k)

    def _rejudge(
        self,
        verdict: Verdict,
        *,
        judge: Judge,
        schema: dict[str, Any],
    ) -> str | None:
        prompt = self._build_salted_prompt(verdict)
        if prompt is None:
            return None
        response = judge.judge(prompt=prompt, schema=schema)
        if response.error is not None or response.verdict is None:
            return None
        overall = response.verdict.get("overall")
        return str(overall) if overall is not None else None

    def _build_salted_prompt(self, verdict: Verdict) -> str | None:
        cls = REGISTRY.get(verdict.evaluator_id)
        if cls is None:
            return None
        evaluator = cls()
        candidate = Candidate(
            record_id=verdict.record_id,
            evaluator_id=verdict.evaluator_id,
            sub_type=verdict.sub_type,
            payload=dict(verdict.candidate_payload),
            confidence=verdict.confidence,
            marc_context={},
        )
        return evaluator.build_prompt(candidate) + _SALT_SUFFIX

    def _write_artifact(
        self,
        *,
        run_dir: Path,
        result: SelfVerifyResult,
        disagreement_records: list[dict[str, Any]],
    ) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "sample_size": result.sample_size,
            "agreements": result.agreements,
            "disagreements": result.disagreements,
            "agreement_rate": result.agreement_rate,
            "passed": result.passed,
            "agreement_floor": result.agreement_floor,
            "run_id": result.run_id,
            "sample_rate": self._sample_rate,
            "seed": self._seed,
            "disagreement_records": disagreement_records,
            "written_at": datetime.now(timezone.utc).isoformat(),
        }
        artifact = run_dir / "self_verify.json"
        artifact.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _load_schema() -> dict[str, Any]:
    return json.loads(VERDICT_SCHEMA_PATH.read_text(encoding="utf-8"))


__all__ = ["SelfVerifyResult", "SelfVerifier"]

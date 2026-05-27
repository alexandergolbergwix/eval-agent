"""5% re-judge consistency loop.

After a Worker session completes its main judging pass, the
``SelfVerifier`` re-asks the configured judge for a deterministic sample
of the already-rendered verdicts, then compares each new ``overall``
against the original. If agreement drops below the configured floor
(default 0.95), the session is treated as suspect.

The re-judge bypasses the verdict cache by invoking ``judge.judge(...)``
directly — the cache layer lives in ``Session._judge_one``, not in the
``Judge`` protocol implementations. The prompt is therefore identical
to the original judging pass; reaching the model again is what catches
non-determinism (temperature drift, server-side variance), not a
different input.

Sampling is stratified by (evaluator_id, sub_type) so a single
record with many candidates can't dominate the sample. Each bucket
contributes at least 1 verdict (when total verdicts ≥ number of
buckets); the largest buckets are trimmed first when the
rounded-up share overshoots the configured ``sample_rate``.

Artefact:
    ``<run_dir>/self_verify.json`` — flat dict shaped like
    ``SelfVerifyResult`` (plus ``run_id``) that callers can consume
    without re-importing the dataclass. Each disagreement record now
    also carries ``error`` (when the redo-call errored) so a low
    agreement rate can be triaged without re-running.
"""

from __future__ import annotations

import json
import math
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
        # Gate on the deterministic (linear / tier-1) verdicts only. Agentic
        # verdicts re-gather evidence on re-run, so re-judging them via the
        # single-shot judge would spuriously disagree — they must not fail
        # the run. Their count is recorded in the artifact for visibility.
        linear_verdicts = [v for v in verdicts if not getattr(v, "agentic", False)]
        self._agentic_excluded = len(verdicts) - len(linear_verdicts)
        sample = self._sample(linear_verdicts)

        agreements = 0
        disagreement_records: list[dict[str, Any]] = []

        for original in sample:
            redo_overall, redo_error = self._rejudge(original, judge=judge, schema=schema)
            if redo_overall is not None and redo_overall == original.overall:
                agreements += 1
            else:
                disagreement_records.append({
                    "record_id": original.record_id,
                    "evaluator_id": original.evaluator_id,
                    "sub_type": original.sub_type,
                    "original_overall": original.overall,
                    "redo_overall": redo_overall,
                    "error": redo_error,
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

        # Group by (evaluator_id, sub_type)
        buckets: dict[tuple[str, str], list[Verdict]] = {}
        for v in verdicts:
            buckets.setdefault((v.evaluator_id, v.sub_type), []).append(v)

        target_total = max(1, int(round(len(verdicts) * self._sample_rate)))
        rng = random.Random(self._seed)

        # Each bucket contributes ceil(target_total * bucket_share)
        # then we trim from the largest buckets if we overshoot target_total.
        n_total = len(verdicts)
        bucket_targets: dict[tuple[str, str], int] = {}
        for key, bucket in buckets.items():
            share = len(bucket) / n_total
            # ceil so every non-empty bucket gets at least 1
            bucket_targets[key] = max(1, math.ceil(share * target_total))

        # If sum overshoots, trim from buckets with the highest assigned count.
        while sum(bucket_targets.values()) > target_total:
            worst = max(bucket_targets, key=lambda k: bucket_targets[k])
            if bucket_targets[worst] <= 1:
                break  # never trim below 1
            bucket_targets[worst] -= 1

        # Stratified sample
        sample: list[Verdict] = []
        for key, bucket in buckets.items():
            k = min(bucket_targets[key], len(bucket))
            if k == len(bucket):
                sample.extend(bucket)
            else:
                sample.extend(rng.sample(bucket, k))

        return sample

    def _rejudge(
        self,
        verdict: Verdict,
        *,
        judge: Judge,
        schema: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        """Return ``(redo_overall, error)``; either may be ``None``."""
        prompt = self._build_prompt(verdict)
        if prompt is None:
            return None, f"no evaluator registered for {verdict.evaluator_id!r}"
        response = judge.judge(prompt=prompt, schema=schema)
        if response.error is not None:
            return None, response.error
        if response.verdict is None:
            return None, "judge returned no verdict"
        overall = response.verdict.get("overall")
        if overall is None:
            return None, "verdict missing 'overall' field"
        return str(overall), None

    def _build_prompt(self, verdict: Verdict) -> str | None:
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
        return evaluator.build_prompt(candidate)

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
            "agentic_excluded": getattr(self, "_agentic_excluded", 0),
            "disagreement_records": disagreement_records,
            "written_at": datetime.now(timezone.utc).isoformat(),
        }
        artifact = run_dir / "self_verify.json"
        artifact.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _load_schema() -> dict[str, Any]:
    """Return the inner verdict sub-schema (same slice the Session sends)."""
    full = json.loads(VERDICT_SCHEMA_PATH.read_text(encoding="utf-8"))
    return full.get("properties", {}).get("verdict", full)


__all__ = ["SelfVerifyResult", "SelfVerifier"]

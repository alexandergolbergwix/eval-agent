"""Per (evaluator, sub_type) confidence-threshold calibration.

Reads a run's ``results.jsonl`` and emits a YAML file listing the
**smallest confidence threshold** per (evaluator_id, sub_type) that
yields a target strict precision (default 0.90).

The MHM Pipeline consumes the YAML to gate its auto-approve UI: rows
currently auto-approved at 0.85 with bad precision get a higher
threshold so most auto-approvals are actually correct.

Strict precision at threshold ``t`` on a bucket is::

    p(t) = |rows where overall == "full" AND confidence >= t|
         / |rows where confidence >= t|

Rows with a truthy ``error`` are dropped (judge errored — no signal).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class BucketCalibration:
    evaluator_id: str
    sub_type: str
    n_total: int
    threshold: float
    precision_at_threshold: float
    n_above_threshold: int
    target_reached: bool
    notes: str


@dataclass(frozen=True)
class CalibrationReport:
    run_id: str
    target_precision: float
    floor_threshold: float
    buckets: list[BucketCalibration] = field(default_factory=list)


# ── core ─────────────────────────────────────────────────────────────────


def _load_results(results_path: Path) -> list[dict]:
    rows: list[dict] = []
    for raw in results_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        rows.append(json.loads(raw))
    return rows


def _bucket_key(row: dict) -> tuple[str, str]:
    return (str(row.get("evaluator_id") or ""), str(row.get("sub_type") or ""))


def _frange_inclusive(lo: float, hi: float, step: float) -> list[float]:
    """Inclusive [lo, hi] in ``step`` increments, robust to FP drift."""
    if step <= 0:
        raise ValueError("step must be > 0")
    out: list[float] = []
    n = int(round((hi - lo) / step))
    for i in range(n + 1):
        out.append(round(lo + i * step, 10))
    return out


def _calibrate_bucket(
    *,
    evaluator_id: str,
    sub_type: str,
    rows: list[dict],
    target_precision: float,
    floor_threshold: float,
    ceiling_threshold: float,
    step: float,
) -> BucketCalibration:
    """Find the smallest threshold yielding ``target_precision`` on a bucket."""
    # Drop errored rows — no signal.
    clean = [r for r in rows if not r.get("error")]
    n_total = len(clean)

    if n_total == 0:
        return BucketCalibration(
            evaluator_id=evaluator_id,
            sub_type=sub_type,
            n_total=0,
            threshold=floor_threshold,
            precision_at_threshold=1.0,
            n_above_threshold=0,
            target_reached=False,
            notes="no data — keeping floor",
        )

    confs: list[tuple[float, bool]] = []
    for r in clean:
        conf = float(r.get("confidence", 0.0))
        is_full = (r.get("verdict") or {}).get("overall") == "full"
        confs.append((conf, is_full))

    thresholds = _frange_inclusive(floor_threshold, ceiling_threshold, step)

    best_at_target: tuple[float, float, int] | None = None  # (t, p, n_above)
    best_overall: tuple[float, float, int] | None = None    # max precision w/ ≥1 above

    for t in thresholds:
        above = [(c, f) for (c, f) in confs if c >= t]
        n_above = len(above)
        if n_above == 0:
            continue
        n_full = sum(1 for _, f in above if f)
        prec = n_full / n_above

        if best_overall is None or prec > best_overall[1]:
            best_overall = (t, prec, n_above)

        if prec >= target_precision and best_at_target is None:
            best_at_target = (t, prec, n_above)
            # Smallest threshold by construction (ascending scan); stop.
            break

    if best_at_target is not None:
        t, prec, n_above = best_at_target
        return BucketCalibration(
            evaluator_id=evaluator_id,
            sub_type=sub_type,
            n_total=n_total,
            threshold=round(t, 10),
            precision_at_threshold=round(prec, 6),
            n_above_threshold=n_above,
            target_reached=True,
            notes="",
        )

    if best_overall is not None:
        t, prec, n_above = best_overall
        return BucketCalibration(
            evaluator_id=evaluator_id,
            sub_type=sub_type,
            n_total=n_total,
            threshold=round(t, 10),
            precision_at_threshold=round(prec, 6),
            n_above_threshold=n_above,
            target_reached=False,
            notes=(
                f"max precision reached at ceiling {ceiling_threshold:.2f} "
                f"({prec:.2f}); raising further would empty the bucket"
            ),
        )

    # Pathological: every threshold empties the bucket. Fall back to floor.
    return BucketCalibration(
        evaluator_id=evaluator_id,
        sub_type=sub_type,
        n_total=n_total,
        threshold=floor_threshold,
        precision_at_threshold=0.0,
        n_above_threshold=0,
        target_reached=False,
        notes="no threshold retains rows — keeping floor",
    )


def calibrate_from_run(
    *,
    run_dir: Path,
    target_precision: float = 0.90,
    floor_threshold: float = 0.85,
    ceiling_threshold: float = 0.99,
    step: float = 0.01,
) -> CalibrationReport:
    """Build a calibration report from ``run_dir/results.jsonl``."""
    results_path = run_dir / "results.jsonl"
    if not results_path.is_file():
        raise FileNotFoundError(f"results.jsonl not found under {run_dir}")

    rows = _load_results(results_path)

    by_bucket: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        by_bucket.setdefault(_bucket_key(row), []).append(row)

    buckets: list[BucketCalibration] = []
    for (evaluator_id, sub_type), bucket_rows in sorted(by_bucket.items()):
        buckets.append(_calibrate_bucket(
            evaluator_id=evaluator_id,
            sub_type=sub_type,
            rows=bucket_rows,
            target_precision=target_precision,
            floor_threshold=floor_threshold,
            ceiling_threshold=ceiling_threshold,
            step=step,
        ))

    return CalibrationReport(
        run_id=run_dir.name,
        target_precision=target_precision,
        floor_threshold=floor_threshold,
        buckets=buckets,
    )


# ── YAML writer ──────────────────────────────────────────────────────────


def write_yaml(report: CalibrationReport, out_path: Path) -> None:
    """Serialise ``report`` as human-readable + pipeline-consumable YAML."""
    import yaml  # noqa: PLC0415

    evaluators: dict[str, dict[str, dict[str, object]]] = {}
    for b in report.buckets:
        evaluators.setdefault(b.evaluator_id, {})[b.sub_type] = {
            "threshold": float(b.threshold),
            "precision": float(b.precision_at_threshold),
            "n_total": int(b.n_total),
            "n_above_threshold": int(b.n_above_threshold),
            "target_reached": bool(b.target_reached),
            "notes": b.notes,
        }

    payload = {
        "meta": {
            "run_id": report.run_id,
            "target_precision": float(report.target_precision),
            "floor_threshold": float(report.floor_threshold),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "evaluators": evaluators,
    }

    header = (
        f"# Auto-generated by eval-agent calibrate from run {report.run_id}\n"
        f"# Target strict precision: {report.target_precision:.2f}\n"
        f"# Floor threshold: {report.floor_threshold:.2f}\n\n"
    )
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + body, encoding="utf-8")


__all__ = [
    "BucketCalibration",
    "CalibrationReport",
    "calibrate_from_run",
    "write_yaml",
]

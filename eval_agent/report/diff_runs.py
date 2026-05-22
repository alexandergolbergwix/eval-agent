"""Cross-run precision diff — compare two ``results.jsonl`` files.

For each (evaluator_id, sub_type) feature found in either run, compute
precision = (verdicts with overall == "full") / (non-errored verdicts)
and classify the change as "regressed" | "improved" | "stable" | "new"
| "gone". The CLI ``eval-agent diff --from <a> --to <b>`` calls this
module and exits non-zero when any feature regressed.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


_REGRESSION_EPSILON = 0.001


@dataclass
class FeatureDiff:
    feature_id: str
    evaluator: str
    sub_type: str
    from_precision: float | None
    to_precision: float | None
    delta: float
    n_from: int
    n_to: int
    verdict: str  # "regressed" | "improved" | "stable" | "new" | "gone"


@dataclass
class RunDiff:
    from_run_id: str
    to_run_id: str
    features: list[FeatureDiff] = field(default_factory=list)
    n_regressed: int = 0
    n_improved: int = 0


def _load_precision(
    results_path: Path,
) -> dict[tuple[str, str], tuple[int, int]]:
    """Return ``{(evaluator, sub_type): (full_count, total_non_errored)}``."""
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"full": 0, "total": 0}
    )
    if not results_path.is_file():
        return {}
    for line in results_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("error"):
            continue
        evaluator = str(rec.get("evaluator_id", ""))
        sub_type = str(rec.get("sub_type") or "")
        verdict = rec.get("verdict") or {}
        overall = str(verdict.get("overall", "fail"))
        bucket = counts[(evaluator, sub_type)]
        bucket["total"] += 1
        if overall == "full":
            bucket["full"] += 1
    return {key: (b["full"], b["total"]) for key, b in counts.items()}


def _precision(full: int, total: int) -> float | None:
    if total <= 0:
        return None
    return full / total


def diff_runs(*, from_run_dir: Path, to_run_dir: Path) -> RunDiff:
    """Compare two run directories and return a structured ``RunDiff``."""
    from_counts = _load_precision(from_run_dir / "results.jsonl")
    to_counts = _load_precision(to_run_dir / "results.jsonl")

    all_keys = sorted(set(from_counts.keys()) | set(to_counts.keys()))
    features: list[FeatureDiff] = []
    n_regressed = 0
    n_improved = 0

    for key in all_keys:
        evaluator, sub_type = key
        feature_id = f"{evaluator}.{sub_type}" if sub_type else evaluator
        from_full, from_total = from_counts.get(key, (0, 0))
        to_full, to_total = to_counts.get(key, (0, 0))
        from_p = _precision(from_full, from_total)
        to_p = _precision(to_full, to_total)

        if from_p is None and to_p is not None:
            verdict = "new"
            delta = 0.0
        elif from_p is not None and to_p is None:
            verdict = "gone"
            delta = 0.0
        elif from_p is None and to_p is None:
            verdict = "stable"
            delta = 0.0
        else:
            assert from_p is not None and to_p is not None
            delta = to_p - from_p
            if delta < -_REGRESSION_EPSILON:
                verdict = "regressed"
                n_regressed += 1
            elif delta > _REGRESSION_EPSILON:
                verdict = "improved"
                n_improved += 1
            else:
                verdict = "stable"

        features.append(
            FeatureDiff(
                feature_id=feature_id,
                evaluator=evaluator,
                sub_type=sub_type,
                from_precision=from_p,
                to_precision=to_p,
                delta=delta,
                n_from=from_total,
                n_to=to_total,
                verdict=verdict,
            )
        )

    return RunDiff(
        from_run_id=from_run_dir.name,
        to_run_id=to_run_dir.name,
        features=features,
        n_regressed=n_regressed,
        n_improved=n_improved,
    )


def _fmt_precision(p: float | None) -> str:
    if p is None:
        return "—"
    return f"{p:.1%}"


def _fmt_delta(d: float, verdict: str) -> str:
    if verdict in {"new", "gone"}:
        return "—"
    sign = "+" if d > 0 else ""
    return f"{sign}{d:.1%}"


def write_diff_markdown(diff: RunDiff, out: Path) -> None:
    """Write a human-readable Markdown diff report.

    The report mentions both run ids ("from"/"to") and at least one
    evaluator id when features are present, so callers can quickly spot
    which model regressed.
    """
    lines = [
        f"# eval-agent diff: {diff.from_run_id} → {diff.to_run_id}",
        "",
        f"- From run: `{diff.from_run_id}`",
        f"- To run:   `{diff.to_run_id}`",
        f"- Features regressed: **{diff.n_regressed}**",
        f"- Features improved: **{diff.n_improved}**",
        f"- Features total: {len(diff.features)}",
        "",
        "## Per-feature precision delta",
        "",
        "| feature | from | to | delta | verdict |",
        "|---|---:|---:|---:|---|",
    ]

    verdict_order = {"regressed": 0, "gone": 1, "improved": 2, "new": 3, "stable": 4}
    sorted_features = sorted(
        diff.features,
        key=lambda f: (verdict_order.get(f.verdict, 99), f.feature_id),
    )
    for f in sorted_features:
        lines.append(
            f"| `{f.feature_id}` "
            f"| {_fmt_precision(f.from_precision)} (n={f.n_from}) "
            f"| {_fmt_precision(f.to_precision)} (n={f.n_to}) "
            f"| {_fmt_delta(f.delta, f.verdict)} "
            f"| {f.verdict} |"
        )

    if not diff.features:
        lines += ["", "_No features found in either run._"]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

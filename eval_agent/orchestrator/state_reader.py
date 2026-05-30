"""Read-only views into eval-agent state + pipeline outputs.

Tools never call this module directly with raw filesystem reads — they
go through these helpers so the orchestrator surfaces a consistent
view (and so future caches / async loads have one chokepoint).

Everything here is pure I/O + light projection. Nothing here mutates,
runs a benchmark, or calls a network. The policy module relies on that
invariant — if a future tool needs side effects, it goes through
``tools.py`` with a fresh policy entry, never through here.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Where eval-agent persists its state by default. Honoured in this order:
#  1. Caller-supplied ``state_dir`` argument (the CLI threads --state-dir here).
#  2. ``EVAL_AGENT_STATE_DIR`` environment variable.
#  3. ``<repo>/state`` (the in-tree default, used by tests + dev runs).
def default_state_dir() -> Path:
    import os
    env = os.environ.get("EVAL_AGENT_STATE_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parents[2]
    return here / "state"


@dataclass(frozen=True)
class RunSummary:
    """Lightweight view of one prior eval-agent run."""

    run_id: str
    path:   Path
    started_at:    str | None = None
    finished_at:   str | None = None
    judge_model:   str | None = None
    evaluators:    tuple[str, ...] = ()
    candidates:    int = 0
    cache_hits:    int = 0
    judged_full:   int = 0
    judged_partial:int = 0
    judged_fail:   int = 0
    input_tokens:  int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class FeatureRow:
    """One row from ``state/feature_list.json``."""

    id:           str
    evaluator:    str
    sub_type:     str
    threshold:    float
    passes:       bool
    attempts:     int
    last_run:     str | None
    last_precision: float | None
    notes:        str = ""


@dataclass(frozen=True)
class BenchmarkRow:
    """One ``summary.csv`` row from a run."""

    evaluator: str
    sub_type:  str
    total:     int
    full:      int
    partial:   int
    fail:      int
    errors:    int
    precision_strict:           float | None
    precision_full_or_partial:  float | None


@dataclass(frozen=True)
class FailedCandidate:
    """One ``results.jsonl`` row whose verdict is fail/partial/abstain."""

    record_id:    str
    evaluator_id: str
    sub_type:     str
    overall:      str
    candidate:    dict[str, Any]
    verdict:      dict[str, Any]
    cache_key:    str | None = None


# ── State-level reads ───────────────────────────────────────────────────


def list_runs(state_dir: Path | None = None) -> list[RunSummary]:
    """List every run dir under ``state/runs``, newest first.

    Skips dirs that lack a ``manifest.json`` — tests use those as
    "incomplete run" fixtures and we don't want to surface them.
    """
    base = (state_dir or default_state_dir()) / "runs"
    if not base.exists():
        return []
    out: list[RunSummary] = []
    for child in sorted(base.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        manifest = child / "manifest.json"
        if not manifest.exists():
            continue
        out.append(_run_summary_from(manifest))
    return out


def latest_run(
    state_dir: Path | None = None, *, evaluator: str | None = None,
) -> RunSummary | None:
    """Return the most recent run, optionally filtered to one containing
    a given evaluator id."""
    for r in list_runs(state_dir):
        if evaluator is None or evaluator in r.evaluators:
            return r
    return None


def read_run_report(run: RunSummary) -> str:
    """Return the markdown report for *run*, or an empty string if absent."""
    p = run.path / "report.md"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def read_benchmark_rows(run: RunSummary) -> list[BenchmarkRow]:
    p = run.path / "summary.csv"
    if not p.exists():
        return []
    rows: list[BenchmarkRow] = []
    with p.open(encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            rows.append(BenchmarkRow(
                evaluator=row.get("evaluator", ""),
                sub_type=row.get("sub_type", ""),
                total=_int(row.get("total")),
                full=_int(row.get("full")),
                partial=_int(row.get("partial")),
                fail=_int(row.get("fail")),
                errors=_int(row.get("errors")),
                precision_strict=_float(row.get("precision_strict")),
                precision_full_or_partial=_float(row.get("precision_full_or_partial")),
            ))
    return rows


def read_failed_candidates(
    run: RunSummary, *,
    evaluator: str | None = None,
    limit: int = 20,
    overall_in: tuple[str, ...] = ("fail", "partial", "abstain"),
) -> list[FailedCandidate]:
    """Stream ``results.jsonl``, return up to *limit* failing rows.

    Each row's ``candidate`` and ``verdict`` are passed through verbatim
    so the orchestrator can quote excerpts in its final report. We
    don't truncate fields here — that's the LLM's problem in the prompt.
    """
    p = run.path / "results.jsonl"
    if not p.exists():
        return []
    out: list[FailedCandidate] = []
    with p.open(encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evaluator is not None and row.get("evaluator_id") != evaluator:
                continue
            verdict = row.get("verdict") or {}
            overall = str(verdict.get("overall") or "").lower()
            if overall not in overall_in:
                continue
            out.append(FailedCandidate(
                record_id=str(row.get("record_id") or ""),
                evaluator_id=str(row.get("evaluator_id") or ""),
                sub_type=str(row.get("sub_type") or ""),
                overall=overall,
                candidate=row.get("candidate") or {},
                verdict=verdict,
                cache_key=row.get("cache_key"),
            ))
            if len(out) >= limit:
                break
    return out


def read_feature_list(state_dir: Path | None = None) -> list[FeatureRow]:
    p = (state_dir or default_state_dir()) / "feature_list.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    rows: list[FeatureRow] = []
    for feat in data.get("features") or []:
        status = feat.get("status") or {}
        rows.append(FeatureRow(
            id=str(feat.get("id") or ""),
            evaluator=str(feat.get("evaluator") or ""),
            sub_type=str(feat.get("sub_type") or ""),
            threshold=float(feat.get("threshold") or 0.0),
            passes=bool(status.get("passes")),
            attempts=int(status.get("attempts") or 0),
            last_run=status.get("last_run"),
            last_precision=_float(status.get("last_precision")),
            notes=str(status.get("notes") or ""),
        ))
    return rows


# ── Multi-run helpers ───────────────────────────────────────────────────


def find_run(run_id: str, state_dir: Path | None = None) -> RunSummary | None:
    base = (state_dir or default_state_dir()) / "runs" / run_id
    manifest = base / "manifest.json"
    if not manifest.exists():
        return None
    return _run_summary_from(manifest)


# ── State summary for the LLM prompt ────────────────────────────────────


def compact_state_summary(state_dir: Path | None = None) -> str:
    """Render a short, plain-text snapshot the LLM can use as ground truth.

    Kept compact deliberately — every additional line costs prompt
    tokens, and the orchestrator should ASK (via tools) for anything
    deeper. The summary covers: top 5 runs, top 5 features, and a
    one-line judge-model claim.
    """
    state_dir = state_dir or default_state_dir()
    runs = list_runs(state_dir)[:5]
    features = read_feature_list(state_dir)
    feat_short = [
        f"- {f.id}: precision={_fmt_pct(f.last_precision)} "
        f"attempts={f.attempts} passes={f.passes}"
        for f in features[:8]
    ]
    run_short = [
        f"- {r.run_id} ({r.judge_model or '?'}) — "
        f"{r.candidates} candidates, "
        f"{r.judged_full}/{r.judged_partial}/{r.judged_fail} (full/partial/fail), "
        f"tokens in={r.input_tokens} out={r.output_tokens}"
        for r in runs
    ]
    lines = [
        "judge_default: gemini-3.5-flash  (per Rule 55 — do not silently switch)",
        "metric_doctrine: strict gold F1 = model quality; "
        "eval-agent candidate rate = audit/triage only (Rule 56).",
        "",
        f"Latest runs ({len(runs)}):",
        *(run_short or ["  (none yet)"]),
        "",
        f"Features tracked ({len(features)}):",
        *(feat_short or ["  (none yet)"]),
    ]
    return "\n".join(lines)


# ── Internals ──────────────────────────────────────────────────────────


def _run_summary_from(manifest_path: Path) -> RunSummary:
    try:
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        m = {}
    stats = m.get("stats") or {}
    cfg   = m.get("config") or {}
    return RunSummary(
        run_id=str(m.get("run_id") or manifest_path.parent.name),
        path=manifest_path.parent,
        started_at=stats.get("started_at"),
        finished_at=stats.get("finished_at"),
        judge_model=cfg.get("judge_model"),
        evaluators=tuple(cfg.get("evaluators") or ()),
        candidates=_int(stats.get("candidates_total")),
        cache_hits=_int(stats.get("cache_hits")),
        judged_full=_int(stats.get("judged_full")),
        judged_partial=_int(stats.get("judged_partial")),
        judged_fail=_int(stats.get("judged_fail")),
        input_tokens=_int(stats.get("input_tokens")),
        output_tokens=_int(stats.get("output_tokens")),
    )


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _float(v: Any) -> float | None:
    if v in (None, "", "None"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%" if 0.0 <= v <= 1.0 else f"{v:.2f}"


__all__ = [
    "BenchmarkRow",
    "FailedCandidate",
    "FeatureRow",
    "RunSummary",
    "compact_state_summary",
    "default_state_dir",
    "find_run",
    "latest_run",
    "list_runs",
    "read_benchmark_rows",
    "read_failed_candidates",
    "read_feature_list",
    "read_run_report",
]

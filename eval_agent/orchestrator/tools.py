"""Phase 1 read-only tools for the orchestrator.

Each tool is a pure function ``(args: dict, ctx: ToolContext) -> Observation``
that:

1. Validates its arguments (returns an ``Observation`` with ``ok=False``
   and a ``summary`` describing the problem — never raises).
2. Reads state via :mod:`eval_agent.orchestrator.state_reader` (never
   touches the filesystem directly).
3. Returns a structured ``data`` payload + a short prose ``summary``
   the LLM uses without re-serialising the full payload.

The registry (``REGISTRY``) is the single allowlist of dispatchable
names. ``policy.allow()`` then gates ON that registry per session
(plan-only / supervised / autonomous) — the registry says what *exists*,
the policy says what's *callable now*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from eval_agent.orchestrator import state_reader as st
from eval_agent.orchestrator.schemas import AllowedTool


# ── Context + Observation ──────────────────────────────────────────────


@dataclass
class ToolContext:
    """Everything tools may consult. Built once per session by the loop."""

    state_dir: Path
    # Phase-2+ extensions will add: gemini_judge, pipeline_output_dir,
    # approval callback. Phase 1 stays read-only and needs only state_dir.
    goal: str = ""


@dataclass
class Observation:
    """One tool call's outcome.

    ``data`` is a JSON-serialisable dict the trace persists verbatim.
    ``summary`` is a short prose line the LLM sees in the next prompt
    (full ``data`` is too noisy to ship back in-band every turn).
    """

    ok:      bool
    tool:    str
    summary: str
    data:    dict[str, Any] = field(default_factory=dict)
    error:   str | None     = None


ToolFn = Callable[[dict[str, Any], ToolContext], Observation]


# ── Tools ──────────────────────────────────────────────────────────────


def _tool_inspect_state(args: dict[str, Any], ctx: ToolContext) -> Observation:
    """High-level state snapshot: latest 5 runs + feature_list summary."""
    runs = st.list_runs(ctx.state_dir)[:5]
    features = st.read_feature_list(ctx.state_dir)
    passing = sum(1 for f in features if f.passes)
    return Observation(
        ok=True,
        tool=AllowedTool.INSPECT_STATE.value,
        summary=(
            f"{len(runs)} recent runs; "
            f"{passing}/{len(features)} features passing."
        ),
        data={
            "runs": [_run_brief(r) for r in runs],
            "features_total": len(features),
            "features_passing": passing,
            "features_failing": len(features) - passing,
        },
    )


def _tool_read_latest_report(args: dict[str, Any], ctx: ToolContext) -> Observation:
    """Return the markdown report from the most recent run.

    Optional arg ``task`` (an evaluator id like ``person_ner``) filters
    to the most recent run that exercised that evaluator.
    """
    task = (args.get("task") or "").strip() or None
    run = st.latest_run(ctx.state_dir, evaluator=task)
    if run is None:
        msg = f"no run found for task={task!r}" if task else "no runs found"
        return Observation(ok=False, tool=AllowedTool.READ_LATEST_REPORT.value,
                            summary=msg, error="not_found")
    body = st.read_run_report(run)
    return Observation(
        ok=True,
        tool=AllowedTool.READ_LATEST_REPORT.value,
        summary=f"{run.run_id}: report.md ({len(body)} chars)",
        data={"run_id": run.run_id, "evaluators": list(run.evaluators),
              "report_md": body, "path": str(run.path / "report.md")},
    )


def _tool_read_benchmark_metrics(args: dict[str, Any], ctx: ToolContext) -> Observation:
    """Return ``summary.csv`` rows for an evaluator from the latest run.

    ``args.task`` (required) is the evaluator id (``person_ner`` etc.).
    """
    task = (args.get("task") or "").strip()
    if not task:
        return Observation(ok=False, tool=AllowedTool.READ_BENCHMARK_METRICS.value,
                            summary="missing required arg 'task'",
                            error="bad_args")
    run = st.latest_run(ctx.state_dir, evaluator=task)
    if run is None:
        return Observation(ok=False, tool=AllowedTool.READ_BENCHMARK_METRICS.value,
                            summary=f"no run found for task={task!r}",
                            error="not_found")
    rows = st.read_benchmark_rows(run)
    rows = [r for r in rows if r.evaluator == task]
    if not rows:
        return Observation(ok=False, tool=AllowedTool.READ_BENCHMARK_METRICS.value,
                            summary=f"{run.run_id} has no rows for {task}",
                            error="empty")
    # Sort by precision_strict ascending — surfaces the worst sub-types first.
    rows.sort(key=lambda r: r.precision_strict if r.precision_strict is not None else 1.0)
    return Observation(
        ok=True,
        tool=AllowedTool.READ_BENCHMARK_METRICS.value,
        summary=(
            f"{run.run_id} {task}: {len(rows)} sub_types; "
            f"worst precision_strict={rows[0].precision_strict}"
        ),
        data={
            "run_id": run.run_id,
            "evaluator": task,
            "rows": [_row_to_dict(r) for r in rows],
            # Doctrine reminder so the LLM doesn't quote candidate-rate
            # as model F1.
            "metric_doctrine": (
                "precision_strict is the eval-agent candidate acceptance "
                "rate (audit/triage). Use Person NER 100-record gold-set "
                "F1 from eval/gemini_benchmark/results for model accuracy."
            ),
        },
    )


def _tool_compare_runs(args: dict[str, Any], ctx: ToolContext) -> Observation:
    """Compare summary.csv rows between two runs (by run_id)."""
    run_a_id = (args.get("run_a") or "").strip()
    run_b_id = (args.get("run_b") or "").strip()
    if not run_a_id or not run_b_id:
        return Observation(ok=False, tool=AllowedTool.COMPARE_RUNS.value,
                            summary="both 'run_a' and 'run_b' are required",
                            error="bad_args")
    run_a = st.find_run(run_a_id, ctx.state_dir)
    run_b = st.find_run(run_b_id, ctx.state_dir)
    if run_a is None or run_b is None:
        missing = [x for x, r in [(run_a_id, run_a), (run_b_id, run_b)] if r is None]
        return Observation(ok=False, tool=AllowedTool.COMPARE_RUNS.value,
                            summary=f"run(s) not found: {', '.join(missing)}",
                            error="not_found")
    rows_a = {(r.evaluator, r.sub_type): r for r in st.read_benchmark_rows(run_a)}
    rows_b = {(r.evaluator, r.sub_type): r for r in st.read_benchmark_rows(run_b)}
    deltas = []
    for key in sorted(set(rows_a) | set(rows_b)):
        a = rows_a.get(key); b = rows_b.get(key)
        pa = a.precision_strict if a else None
        pb = b.precision_strict if b else None
        if pa is None and pb is None:
            continue
        deltas.append({
            "evaluator":          key[0],
            "sub_type":           key[1],
            "precision_strict_a": pa,
            "precision_strict_b": pb,
            "delta":              (pb - pa) if (pa is not None and pb is not None) else None,
        })
    deltas.sort(key=lambda d: (d["delta"] if d["delta"] is not None else 0.0))
    biggest = deltas[0] if deltas else None
    return Observation(
        ok=True,
        tool=AllowedTool.COMPARE_RUNS.value,
        summary=(
            f"{len(deltas)} sub_types compared; "
            f"largest regression: {biggest['evaluator']}.{biggest['sub_type']} "
            f"delta={biggest['delta']}" if biggest else f"{len(deltas)} sub_types compared"
        ),
        data={"run_a": run_a.run_id, "run_b": run_b.run_id, "deltas": deltas},
    )


def _tool_inspect_failed_candidates(args: dict[str, Any], ctx: ToolContext) -> Observation:
    """Surface fail/partial/abstain candidates from the latest run for a task."""
    task = (args.get("task") or "").strip()
    if not task:
        return Observation(ok=False, tool=AllowedTool.INSPECT_FAILED_CANDIDATES.value,
                            summary="missing required arg 'task'",
                            error="bad_args")
    try:
        limit = max(1, min(50, int(args.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    run = st.latest_run(ctx.state_dir, evaluator=task)
    if run is None:
        return Observation(ok=False, tool=AllowedTool.INSPECT_FAILED_CANDIDATES.value,
                            summary=f"no run for task={task!r}",
                            error="not_found")
    failures = st.read_failed_candidates(run, evaluator=task, limit=limit)
    by_overall: dict[str, int] = {}
    for f in failures:
        by_overall[f.overall] = by_overall.get(f.overall, 0) + 1
    return Observation(
        ok=True,
        tool=AllowedTool.INSPECT_FAILED_CANDIDATES.value,
        summary=(
            f"{run.run_id} {task}: {len(failures)} failing candidates; "
            f"{by_overall}"
        ),
        data={
            "run_id":  run.run_id,
            "task":    task,
            "limit":   limit,
            "by_overall": by_overall,
            "candidates": [
                {
                    "record_id":   f.record_id,
                    "sub_type":    f.sub_type,
                    "overall":     f.overall,
                    "candidate":   f.candidate,
                    "verdict":     f.verdict,
                }
                for f in failures
            ],
        },
    )


def _tool_summarize_feature_list(args: dict[str, Any], ctx: ToolContext) -> Observation:
    rows = st.read_feature_list(ctx.state_dir)
    grouped: dict[str, dict[str, int]] = {}
    for r in rows:
        g = grouped.setdefault(r.evaluator, {"total": 0, "passing": 0})
        g["total"] += 1
        if r.passes:
            g["passing"] += 1
    return Observation(
        ok=True,
        tool=AllowedTool.SUMMARIZE_FEATURE_LIST.value,
        summary=(
            f"{len(rows)} features across "
            f"{len(grouped)} evaluators"
        ),
        data={"by_evaluator": grouped, "rows": [
            {
                "id":        r.id,
                "evaluator": r.evaluator,
                "sub_type":  r.sub_type,
                "threshold": r.threshold,
                "passes":    r.passes,
                "attempts":  r.attempts,
                "last_run":  r.last_run,
                "last_precision": r.last_precision,
            }
            for r in rows
        ]},
    )


def _tool_recommend_next_eval(args: dict[str, Any], ctx: ToolContext) -> Observation:
    """Pure heuristic — surface the feature with the most attempts and
    lowest last_precision. The LLM is free to override; this is just
    a hint so the orchestrator doesn't have to invent priorities."""
    rows = [r for r in st.read_feature_list(ctx.state_dir) if not r.passes]
    if not rows:
        return Observation(ok=True, tool=AllowedTool.RECOMMEND_NEXT_EVAL.value,
                            summary="every tracked feature already passes",
                            data={"candidates": []})
    rows.sort(key=lambda r: (
        -(r.attempts or 0),
        r.last_precision if r.last_precision is not None else 1.0,
    ))
    top = rows[:5]
    return Observation(
        ok=True,
        tool=AllowedTool.RECOMMEND_NEXT_EVAL.value,
        summary=(
            f"top suggestion: {top[0].id} (attempts={top[0].attempts}, "
            f"last_precision={top[0].last_precision})"
        ),
        data={"candidates": [
            {
                "id":             r.id,
                "evaluator":      r.evaluator,
                "sub_type":       r.sub_type,
                "attempts":       r.attempts,
                "last_precision": r.last_precision,
                "rationale":      "high-attempts, low-precision feature first",
            }
            for r in top
        ]},
    )


# ── Registry ──────────────────────────────────────────────────────────


REGISTRY: dict[str, ToolFn] = {
    AllowedTool.INSPECT_STATE.value:              _tool_inspect_state,
    AllowedTool.READ_LATEST_REPORT.value:         _tool_read_latest_report,
    AllowedTool.READ_BENCHMARK_METRICS.value:     _tool_read_benchmark_metrics,
    AllowedTool.COMPARE_RUNS.value:               _tool_compare_runs,
    AllowedTool.INSPECT_FAILED_CANDIDATES.value:  _tool_inspect_failed_candidates,
    AllowedTool.SUMMARIZE_FEATURE_LIST.value:     _tool_summarize_feature_list,
    AllowedTool.RECOMMEND_NEXT_EVAL.value:        _tool_recommend_next_eval,
}


# Static documentation shown to the LLM in the prompt. Each entry is
# what the prompt renderer iterates over; the actual schema is enforced
# by the dispatcher when the tool runs (bad args → ok=False observation).
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": AllowedTool.INSPECT_STATE.value,
        "args": [],
        "description": "Snapshot of recent runs and feature_list passing counts.",
    },
    {
        "name": AllowedTool.READ_LATEST_REPORT.value,
        "args": ["task?"],
        "description": "Markdown report.md from the most recent run, optionally filtered to an evaluator.",
    },
    {
        "name": AllowedTool.READ_BENCHMARK_METRICS.value,
        "args": ["task"],
        "description": "summary.csv rows for an evaluator, sorted worst-first by precision_strict.",
    },
    {
        "name": AllowedTool.COMPARE_RUNS.value,
        "args": ["run_a", "run_b"],
        "description": "Per-sub_type precision_strict delta between two run ids.",
    },
    {
        "name": AllowedTool.INSPECT_FAILED_CANDIDATES.value,
        "args": ["task", "limit?"],
        "description": "Surface fail/partial/abstain candidates from the latest run for a task.",
    },
    {
        "name": AllowedTool.SUMMARIZE_FEATURE_LIST.value,
        "args": [],
        "description": "Roll-up of features tracked in state/feature_list.json.",
    },
    {
        "name": AllowedTool.RECOMMEND_NEXT_EVAL.value,
        "args": [],
        "description": "Heuristic suggestion of which feature to focus on next.",
    },
]


def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> Observation:
    """Run a tool. Unknown names produce ok=False; all exceptions are
    caught and surfaced as observations so the loop keeps running."""
    fn = REGISTRY.get(name)
    if fn is None:
        return Observation(
            ok=False, tool=name,
            summary=f"unknown tool {name!r}; allowed: {sorted(REGISTRY)}",
            error="unknown_tool",
        )
    try:
        return fn(args or {}, ctx)
    except Exception as exc:  # noqa: BLE001
        return Observation(
            ok=False, tool=name,
            summary=f"tool {name!r} raised {type(exc).__name__}: {exc}",
            error="raised",
        )


# ── Internals ──────────────────────────────────────────────────────────


def _run_brief(r: st.RunSummary) -> dict[str, Any]:
    return {
        "run_id":      r.run_id,
        "judge_model": r.judge_model,
        "evaluators":  list(r.evaluators),
        "candidates":  r.candidates,
        "cache_hits":  r.cache_hits,
        "judged":      {
            "full":    r.judged_full,
            "partial": r.judged_partial,
            "fail":    r.judged_fail,
        },
        "started_at":  r.started_at,
        "finished_at": r.finished_at,
    }


def _row_to_dict(r: st.BenchmarkRow) -> dict[str, Any]:
    return {
        "evaluator":                 r.evaluator,
        "sub_type":                  r.sub_type,
        "total":                     r.total,
        "full":                      r.full,
        "partial":                   r.partial,
        "fail":                      r.fail,
        "errors":                    r.errors,
        "precision_strict":          r.precision_strict,
        "precision_full_or_partial": r.precision_full_or_partial,
    }


__all__ = [
    "Observation",
    "REGISTRY",
    "TOOL_SPECS",
    "ToolContext",
    "ToolFn",
    "dispatch",
]

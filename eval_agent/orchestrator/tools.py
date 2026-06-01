"""Allowlisted tools for the LLM orchestrator."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval_agent.orchestrator.state_reader import (
    latest_child,
    load_json_if_exists,
    read_text_if_exists,
    summarize_state,
)


@dataclass
class ToolResult:
    """Result returned to the orchestrator LLM."""

    ok: bool
    summary: str
    data: dict[str, Any]


@dataclass
class Observation:
    """Normalized observation returned by a tool call."""

    tool: str
    ok: bool
    summary: str
    data: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class ToolContext:
    """Context available to allowlisted tools."""

    state_dir: Path
    goal: str
    pipeline_root: Path | None = None
    pipeline_output: Path | None = None
    mode: str = "plan_only"
    api_key: str = ""


class OrchestratorTools:
    """Dispatch allowlisted orchestrator tools."""

    def __init__(
        self,
        *,
        state_dir: Path,
        pipeline_root: Path | None,
        pipeline_output: Path | None,
        mode: str,
        api_key: str,
    ) -> None:
        self.state_dir = state_dir
        self.pipeline_root = pipeline_root
        self.pipeline_output = pipeline_output
        self.mode = mode
        self.api_key = api_key

    def specs(self) -> list[dict[str, Any]]:
        """Tool descriptions shown to the LLM."""
        return [
            {"name": "inspect_state", "args": [], "description": "Summarize eval-agent state, latest runs, feature_list, and progress."},
            {"name": "summarize_feature_list", "args": [], "description": "Read feature_list.json and summarize pass/fail evaluator buckets."},
            {"name": "read_latest_report", "args": ["run_id"], "description": "Read an eval-agent report.md. Omit run_id for latest."},
            {"name": "read_benchmark_metrics", "args": ["task"], "description": "Read latest Gemini benchmark metrics for a task such as person_ner."},
            {"name": "compare_runs", "args": ["from_run", "to_run"], "description": "Read two eval-agent summary.csv files and compare candidate-level precision buckets."},
            {"name": "inspect_failed_candidates", "args": ["task", "limit"], "description": "Show failed/partial candidate verdicts from latest benchmark or eval-agent run."},
            {"name": "recommend_next_eval", "args": ["task"], "description": "Produce deterministic next-evaluation suggestions from available metrics."},
            {"name": "run_eval_agent", "args": ["evaluators", "no_cache"], "description": "Run eval-agent on the configured pipeline output. Blocked in plan-only mode."},
            {"name": "regenerate_report", "args": ["run_id"], "description": "Regenerate report.md for an eval-agent run. Blocked in plan-only mode."},
            {"name": "write_plan_note", "args": ["title", "body"], "description": "Write a markdown note under state/orchestrator/proposals."},
            {"name": "create_experiment_manifest", "args": ["name", "body"], "description": "Write an experiment manifest under state/orchestrator/proposals."},
        ]

    def dispatch(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Execute one tool by name."""
        if tool == "inspect_state":
            return self._inspect_state()
        if tool == "summarize_feature_list":
            return self._summarize_feature_list()
        if tool == "read_latest_report":
            return self._read_latest_report(args)
        if tool == "read_benchmark_metrics":
            return self._read_benchmark_metrics(args)
        if tool == "compare_runs":
            return self._compare_runs(args)
        if tool == "inspect_failed_candidates":
            return self._inspect_failed_candidates(args)
        if tool == "recommend_next_eval":
            return self._recommend_next_eval(args)
        if tool == "run_eval_agent":
            return self._run_eval_agent(args)
        if tool == "regenerate_report":
            return self._regenerate_report(args)
        if tool == "write_plan_note":
            return self._write_proposal(args, suffix="plan.md")
        if tool == "create_experiment_manifest":
            return self._write_proposal(args, suffix="experiment.md")
        return ToolResult(False, f"unknown tool {tool!r}", {})

    def _inspect_state(self) -> ToolResult:
        summary = summarize_state(
            state_dir=self.state_dir,
            pipeline_root=self.pipeline_root,
        )
        return ToolResult(True, "state inspected", {"summary": summary})

    def _summarize_feature_list(self) -> ToolResult:
        path = self.state_dir / "feature_list.json"
        data = load_json_if_exists(path)
        features = data.get("features") if isinstance(data, dict) else None
        rows: list[dict[str, Any]] = []
        if isinstance(features, list):
            for feature in features:
                if isinstance(feature, dict):
                    rows.append({
                        "id": feature.get("id"),
                        "passes": feature.get("passes"),
                        "last_precision": feature.get("last_precision"),
                        "attempts": feature.get("attempts"),
                    })
        return ToolResult(
            True,
            f"feature_list rows={len(rows)}",
            {"path": str(path), "features": rows[:100]},
        )

    def _read_latest_report(self, args: dict[str, Any]) -> ToolResult:
        run_dir = self._resolve_run(args.get("run_id"))
        if run_dir is None:
            return ToolResult(False, "no eval-agent run found", {})
        report = read_text_if_exists(run_dir / "report.md", limit=8000)
        return ToolResult(True, f"read report {run_dir.name}", {
            "run_dir": str(run_dir),
            "report": report,
        })

    def _read_benchmark_metrics(self, args: dict[str, Any]) -> ToolResult:
        if self.pipeline_root is None:
            return ToolResult(False, "pipeline_root unavailable", {})
        task = str(args.get("task") or "person_ner")
        root = self.pipeline_root / "eval" / "gemini_benchmark" / "results" / task
        run = latest_child(root)
        if run is None:
            return ToolResult(False, f"no benchmark run for {task}", {"root": str(root)})
        metrics = load_json_if_exists(run / "metrics.json")
        summary = read_text_if_exists(run / "summary.md", limit=6000)
        person_report = read_text_if_exists(run / "person_ner_report.md", limit=6000)
        return ToolResult(True, f"read benchmark {task}/{run.name}", {
            "run_dir": str(run),
            "metrics": metrics,
            "summary": summary,
            "person_ner_report": person_report,
        })

    def _compare_runs(self, args: dict[str, Any]) -> ToolResult:
        from_run = str(args.get("from_run") or "")
        to_run = str(args.get("to_run") or args.get("to") or "")
        if not from_run or not to_run:
            return ToolResult(False, "from_run and to_run are required", {})
        left = self.state_dir / "runs" / from_run / "summary.csv"
        right = self.state_dir / "runs" / to_run / "summary.csv"
        if not left.is_file() or not right.is_file():
            return ToolResult(False, "one or both summary.csv files missing", {
                "from": str(left),
                "to": str(right),
            })
        lrows = _read_csv(left)
        rrows = _read_csv(right)
        return ToolResult(True, f"compared {from_run} -> {to_run}", {
            "from": lrows,
            "to": rrows,
        })

    def _inspect_failed_candidates(self, args: dict[str, Any]) -> ToolResult:
        limit = int(args.get("limit") or 20)
        task = str(args.get("task") or "person_ner")
        rows: list[dict[str, Any]] = []
        if self.pipeline_root is not None:
            root = self.pipeline_root / "eval" / "gemini_benchmark" / "results" / task
            run = latest_child(root)
            verdicts = run / "verdicts.jsonl" if run else None
            if verdicts is not None and verdicts.is_file():
                rows.extend(_read_failed_verdicts(verdicts, limit))
                return ToolResult(True, f"found {len(rows)} failed benchmark verdicts", {
                    "path": str(verdicts),
                    "rows": rows,
                })
        run_dir = self._resolve_run(None)
        verdicts = run_dir / "results.jsonl" if run_dir else None
        if verdicts is not None and verdicts.is_file():
            rows.extend(_read_failed_verdicts(verdicts, limit))
        return ToolResult(True, f"found {len(rows)} failed eval-agent verdicts", {
            "path": str(verdicts) if verdicts else "",
            "rows": rows,
        })

    def _recommend_next_eval(self, args: dict[str, Any]) -> ToolResult:
        task = str(args.get("task") or "person_ner")
        recommendations = [
            f"Read latest strict metrics for {task} before consulting eval-agent verdict rates.",
            "If strict F1 regressed, run trained-only benchmark with --no-eval-agent first.",
            "Use eval-agent failed candidates only to cluster qualitative error modes.",
            "Do not report candidate-level looks-right rate as model F1.",
        ]
        return ToolResult(True, "deterministic recommendations generated", {
            "task": task,
            "recommendations": recommendations,
        })

    def _run_eval_agent(self, args: dict[str, Any]) -> ToolResult:
        if self.mode in {"plan-only", "plan_only"}:
            return ToolResult(False, "run_eval_agent blocked in plan-only mode", {})
        if self.pipeline_output is None:
            return ToolResult(False, "pipeline_output is required to run eval-agent", {})
        evaluators = str(args.get("evaluators") or "all")
        cmd = [
            sys.executable,
            "-m", "eval_agent.cli",
            "run",
            "--pipeline-output", str(self.pipeline_output),
            "--state-dir", str(self.state_dir),
            "--evaluators", evaluators,
        ]
        if bool(args.get("no_cache")):
            cmd.append("--no-cache")
        env = dict(os.environ)
        if self.api_key:
            env["GEMINI_API_KEY"] = self.api_key
        proc = subprocess.run(
            cmd,
            cwd=str(Path(__file__).resolve().parents[2]),
            env=env,
            text=True,
            capture_output=True,
            timeout=60 * 60,
            check=False,
        )
        return ToolResult(proc.returncode == 0, f"run_eval_agent rc={proc.returncode}", {
            "cmd": cmd,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-2000:],
        })

    def _regenerate_report(self, args: dict[str, Any]) -> ToolResult:
        if self.mode in {"plan-only", "plan_only"}:
            return ToolResult(False, "regenerate_report blocked in plan-only mode", {})
        run_id = str(args.get("run_id") or "latest")
        cmd = [
            sys.executable,
            "-m", "eval_agent.cli",
            "report",
            "--run", run_id,
        ]
        env = dict(os.environ)
        env["EVAL_AGENT_STATE_DIR"] = str(self.state_dir)
        proc = subprocess.run(
            cmd,
            cwd=str(Path(__file__).resolve().parents[2]),
            env=env,
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        return ToolResult(proc.returncode == 0, f"regenerate_report rc={proc.returncode}", {
            "cmd": cmd,
            "stdout": proc.stdout[-2000:],
            "stderr": proc.stderr[-2000:],
        })

    def _write_proposal(self, args: dict[str, Any], *, suffix: str) -> ToolResult:
        title = str(args.get("title") or args.get("name") or "orchestrator-proposal")
        body = str(args.get("body") or "")
        safe_title = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in title).strip("-")
        proposals = self.state_dir / "orchestrator" / "proposals"
        proposals.mkdir(parents=True, exist_ok=True)
        path = proposals / f"{safe_title or 'proposal'}-{suffix}"
        path.write_text(body + "\n", encoding="utf-8")
        return ToolResult(True, f"wrote proposal {path.name}", {"path": str(path)})

    def _resolve_run(self, run_id: object | None) -> Path | None:
        runs = self.state_dir / "runs"
        if run_id:
            path = runs / str(run_id)
            return path if path.is_dir() else None
        return latest_child(runs)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _read_failed_verdicts(path: Path, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        verdict = rec.get("verdict")
        if isinstance(verdict, dict) and "verdict" in verdict:
            verdict = verdict.get("verdict")
        if not isinstance(verdict, dict):
            continue
        overall = str(verdict.get("overall") or "").lower()
        if overall in {"full", "pass"}:
            continue
        rows.append({
            "method": rec.get("method"),
            "record_id": rec.get("record_id") or (rec.get("verdict") or {}).get("record_id"),
            "evaluator_id": rec.get("evaluator_id") or (rec.get("verdict") or {}).get("evaluator_id"),
            "sub_type": rec.get("sub_type") or (rec.get("verdict") or {}).get("sub_type"),
            "candidate": rec.get("candidate") or (rec.get("verdict") or {}).get("candidate"),
            "overall": overall,
            "reasoning": verdict.get("reasoning"),
        })
        if len(rows) >= limit:
            break
    return rows


TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "inspect_state", "args": [], "description": "Summarize eval-agent state, recent runs, feature_list, and progress."},
    {"name": "summarize_feature_list", "args": [], "description": "Summarize feature pass/fail state by evaluator."},
    {"name": "read_latest_report", "args": ["run_id"], "description": "Read report.md for the latest or named eval-agent run."},
    {"name": "read_benchmark_metrics", "args": ["task"], "description": "Read strict/candidate metrics for a task from benchmark results or latest summary.csv."},
    {"name": "compare_runs", "args": ["run_a", "run_b"], "description": "Compare two eval-agent summary.csv files."},
    {"name": "inspect_failed_candidates", "args": ["task", "limit"], "description": "List failed or partial verdict candidates."},
    {"name": "recommend_next_eval", "args": [], "description": "Recommend the next deterministic evaluation step."},
    {"name": "run_eval_agent", "args": ["evaluators", "no_cache"], "description": "Run eval-agent on the configured pipeline output."},
    {"name": "regenerate_report", "args": ["run_id"], "description": "Regenerate report.md for a run."},
    {"name": "write_plan_note", "args": ["title", "body"], "description": "Write a markdown plan note."},
    {"name": "create_experiment_manifest", "args": ["name", "body"], "description": "Write an experiment manifest."},
]

REGISTRY = {spec["name"]: spec for spec in TOOL_SPECS}


def dispatch(tool: str, args: dict[str, Any], ctx: ToolContext) -> Observation:
    """Run one allowlisted tool and always return an observation."""
    try:
        return _dispatch(tool, args, ctx)
    except Exception as exc:  # noqa: BLE001
        return Observation(
            tool=tool,
            ok=False,
            summary=f"{tool} raised {type(exc).__name__}",
            data={},
            error="tool_error",
        )


def _dispatch(tool: str, args: dict[str, Any], ctx: ToolContext) -> Observation:
    if tool == "inspect_state":
        return _inspect_state(ctx)
    if tool == "summarize_feature_list":
        return _summarize_feature_list_obs(ctx)
    if tool == "read_latest_report":
        return _read_latest_report_obs(args, ctx)
    if tool == "read_benchmark_metrics":
        return _read_benchmark_metrics_obs(args, ctx)
    if tool == "compare_runs":
        return _compare_runs_obs(args, ctx)
    if tool == "inspect_failed_candidates":
        return _inspect_failed_candidates_obs(args, ctx)
    if tool == "recommend_next_eval":
        return _recommend_next_eval_obs(ctx)
    if tool == "run_eval_agent":
        result = OrchestratorTools(
            state_dir=ctx.state_dir,
            pipeline_root=ctx.pipeline_root,
            pipeline_output=ctx.pipeline_output,
            mode=ctx.mode,
            api_key=ctx.api_key,
        ).dispatch(tool, args)
        return Observation(tool, result.ok, result.summary, result.data, None if result.ok else "command_failed")
    if tool == "regenerate_report":
        result = OrchestratorTools(
            state_dir=ctx.state_dir,
            pipeline_root=ctx.pipeline_root,
            pipeline_output=ctx.pipeline_output,
            mode=ctx.mode,
            api_key=ctx.api_key,
        ).dispatch(tool, args)
        return Observation(tool, result.ok, result.summary, result.data, None if result.ok else "command_failed")
    if tool in {"write_plan_note", "create_experiment_manifest"}:
        result = OrchestratorTools(
            state_dir=ctx.state_dir,
            pipeline_root=ctx.pipeline_root,
            pipeline_output=ctx.pipeline_output,
            mode=ctx.mode,
            api_key=ctx.api_key,
        ).dispatch(tool, args)
        return Observation(tool, result.ok, result.summary, result.data, None if result.ok else "write_failed")
    return Observation(tool, False, f"unknown tool {tool!r}", {}, "unknown_tool")


def _features(state_dir: Path) -> list[dict[str, Any]]:
    data = load_json_if_exists(state_dir / "feature_list.json")
    rows = data.get("features") if isinstance(data, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _status(row: dict[str, Any]) -> dict[str, Any]:
    status = row.get("status")
    return status if isinstance(status, dict) else row


def _inspect_state(ctx: ToolContext) -> Observation:
    features = _features(ctx.state_dir)
    passing = sum(1 for row in features if bool(_status(row).get("passes")))
    runs_root = ctx.state_dir / "runs"
    runs: list[dict[str, Any]] = []
    if runs_root.is_dir():
        for run in sorted((p for p in runs_root.iterdir() if p.is_dir()), reverse=True)[:10]:
            manifest = load_json_if_exists(run / "manifest.json")
            runs.append({"run_id": run.name, "path": str(run), "manifest": manifest})
    return Observation(
        "inspect_state",
        True,
        f"state has {len(features)} features and {len(runs)} recent runs",
        {
            "state_dir": str(ctx.state_dir),
            "features_total": len(features),
            "features_passing": passing,
            "features_failing": len(features) - passing,
            "runs": runs,
        },
    )


def _summarize_feature_list_obs(ctx: ToolContext) -> Observation:
    by_evaluator: dict[str, dict[str, int]] = {}
    rows = []
    for row in _features(ctx.state_dir):
        evaluator = str(row.get("evaluator") or str(row.get("id") or "").split(".")[0] or "unknown")
        bucket = by_evaluator.setdefault(evaluator, {"total": 0, "passing": 0})
        bucket["total"] += 1
        status = _status(row)
        if bool(status.get("passes")):
            bucket["passing"] += 1
        rows.append({
            "id": row.get("id"),
            "evaluator": evaluator,
            "sub_type": row.get("sub_type"),
            "passes": status.get("passes"),
            "last_precision": status.get("last_precision"),
            "attempts": status.get("attempts"),
        })
    return Observation(
        "summarize_feature_list",
        True,
        f"summarized {len(rows)} features",
        {"features": rows, "by_evaluator": by_evaluator},
    )


def _resolve_run(state_dir: Path, run_id: object | None) -> Path | None:
    runs = state_dir / "runs"
    if run_id:
        path = runs / str(run_id)
        return path if path.is_dir() else None
    return latest_child(runs)


def _read_latest_report_obs(args: dict[str, Any], ctx: ToolContext) -> Observation:
    run = _resolve_run(ctx.state_dir, args.get("run_id"))
    if run is None:
        return Observation("read_latest_report", False, "no run found", {}, "not_found")
    report = run / "report.md"
    return Observation(
        "read_latest_report",
        True,
        f"read report for {run.name}",
        {"run_id": run.name, "run_dir": str(run), "report_md": report.read_text(encoding="utf-8") if report.is_file() else ""},
    )


def _read_benchmark_metrics_obs(args: dict[str, Any], ctx: ToolContext) -> Observation:
    task = args.get("task")
    if not task:
        return Observation("read_benchmark_metrics", False, "task is required", {}, "bad_args")
    rows: list[dict[str, Any]] = []
    if ctx.pipeline_root is not None:
        root = ctx.pipeline_root / "eval" / "gemini_benchmark" / "results" / str(task)
        run = latest_child(root)
        metrics = load_json_if_exists(run / "metrics.json") if run else {}
        if metrics:
            return Observation(
                "read_benchmark_metrics",
                True,
                f"read benchmark metrics for {task}",
                {"run_dir": str(run), "metrics": metrics},
            )
    run = latest_child(ctx.state_dir / "runs")
    summary_path = run / "summary.csv" if run else None
    if summary_path is not None and summary_path.is_file():
        for row in _read_csv(summary_path):
            if str(row.get("evaluator") or row.get("evaluator_id") or "") != str(task):
                continue
            row = dict(row)
            for key in ("precision_strict", "precision_full_or_partial"):
                if key in row:
                    try:
                        row[key] = float(row[key])
                    except ValueError:
                        pass
            rows.append(row)
    rows.sort(key=lambda r: float(r.get("precision_strict") or 0.0))
    return Observation(
        "read_benchmark_metrics",
        True,
        f"read {len(rows)} metric rows for {task}",
        {"rows": rows, "source": str(summary_path) if summary_path else ""},
    )


def _compare_runs_obs(args: dict[str, Any], ctx: ToolContext) -> Observation:
    run_a = args.get("run_a") or args.get("from_run")
    run_b = args.get("run_b") or args.get("to_run") or args.get("to")
    left = _resolve_run(ctx.state_dir, run_a)
    right = _resolve_run(ctx.state_dir, run_b)
    if left is None or right is None:
        return Observation("compare_runs", False, "one or both runs missing", {}, "not_found")
    return Observation(
        "compare_runs",
        True,
        f"compared {left.name} to {right.name}",
        {
            "run_a": left.name,
            "run_b": right.name,
            "summary_a": _read_csv(left / "summary.csv") if (left / "summary.csv").is_file() else [],
            "summary_b": _read_csv(right / "summary.csv") if (right / "summary.csv").is_file() else [],
        },
    )


def _inspect_failed_candidates_obs(args: dict[str, Any], ctx: ToolContext) -> Observation:
    limit = int(args.get("limit") or 20)
    run = latest_child(ctx.state_dir / "runs")
    path = run / "results.jsonl" if run else None
    candidates = _read_failed_verdicts(path, limit) if path is not None and path.is_file() else []
    return Observation(
        "inspect_failed_candidates",
        True,
        f"found {len(candidates)} failed/partial candidates",
        {"candidates": candidates, "path": str(path) if path else ""},
    )


def _recommend_next_eval_obs(ctx: ToolContext) -> Observation:
    candidates = []
    for row in _features(ctx.state_dir):
        status = _status(row)
        if not bool(status.get("passes")):
            candidates.append({
                "id": row.get("id"),
                "last_precision": status.get("last_precision"),
                "attempts": status.get("attempts"),
            })
    return Observation(
        "recommend_next_eval",
        True,
        f"recommended {len(candidates)} failing feature(s)",
        {
            "candidates": candidates,
            "guidance": [
                "Use strict benchmark metrics as the source of model F1.",
                "Use eval-agent candidate verdicts for qualitative error clustering.",
                "Run trained-only benchmark before asking the LLM judge to compare methods.",
            ],
        },
    )


__all__ = [
    "Observation",
    "OrchestratorTools",
    "REGISTRY",
    "TOOL_SPECS",
    "ToolContext",
    "ToolResult",
    "dispatch",
]

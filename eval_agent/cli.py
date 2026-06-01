"""eval-agent CLI entry point.

Phase 0 (bootstrap) implements only ``doctor`` and ``verify`` as
working subcommands so ``init.sh`` and ``make verify`` succeed.
Phases 1+ flesh out ``run``, ``report``, ``diff``, ``recover``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_state_dir() -> Path:
    """STATE_DIR resolution.

    Precedence (highest first):
      1. ``--state-dir`` CLI flag passed to the ``run`` subcommand (handled at run-time;
         monkey-patches ``sys.modules[__name__].STATE_DIR`` so all late-bind sites pick it up).
      2. ``EVAL_AGENT_STATE_DIR`` env var (used by the MHM Pipeline bundle to point at a
         writable per-user dir).
      3. ``REPO_ROOT / "state"`` — the in-tree default for ``make run``.
    """
    env = os.environ.get("EVAL_AGENT_STATE_DIR")
    if env:
        return Path(env)
    return REPO_ROOT / "state"


STATE_DIR = _resolve_state_dir()
CONFIG_DIR = REPO_ROOT / "config"
SCHEMAS_DIR = CONFIG_DIR / "schemas"


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """Health check — print a small status table and exit 0 if usable."""
    checks: list[tuple[str, str]] = []

    # Python version
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append(("python", py))

    # State directories
    checks.append(("state/", "ok" if STATE_DIR.is_dir() else "MISSING"))
    checks.append(("config/", "ok" if CONFIG_DIR.is_dir() else "MISSING"))
    checks.append(("config/schemas/verdict.v1.json",
                   "ok" if (SCHEMAS_DIR / "verdict.v1.json").is_file() else "MISSING"))

    # State files (informational — absent ones are bootstrapped lazily by init.sh)
    feat = STATE_DIR / "feature_list.json"
    checks.append(("state/feature_list.json",
                   "ok" if feat.is_file() else "not yet bootstrapped"))
    prog = STATE_DIR / "progress.md"
    checks.append(("state/progress.md",
                   "ok" if prog.is_file() else "not yet bootstrapped"))

    # API key (informational only — verify doesn't require it; run does)
    has_key = bool(os.environ.get("GEMINI_API_KEY"))
    checks.append(("GEMINI_API_KEY env",
                   "set" if has_key else "unset (run will prompt)"))

    # Print
    print("eval-agent doctor — health check")
    print("-" * 50)
    for name, status in checks:
        print(f"  {name:42s} {status}")
    missing = [c for c in checks if c[1] == "MISSING"]
    return 0 if not missing else 2


def _cmd_verify(_args: argparse.Namespace) -> int:
    """Session-startup pre-flight: schemas + cache integrity.

    Validates the verdict JSON Schema, then walks every line in the
    on-disk verdict cache to confirm each row parses and that the
    inner verdict object matches the schema. Returns non-zero on any
    failure.
    """
    from eval_agent.orchestration import session as session_mod  # noqa: PLC0415
    from eval_agent.orchestration.verify import run_verify  # noqa: PLC0415

    report = run_verify(
        cache_path=session_mod.CACHE_PATH,
        schemas_dir=SCHEMAS_DIR,
    )

    print(f"  schemas/verdict.v1.json   {'ok' if report.passed or not any('schema' in f and 'invalid' in f for f in report.failures) else 'FAIL'}")
    print(f"  state/                    {'ok' if STATE_DIR.is_dir() else 'MISSING'}")
    print(f"  config/                   {'ok' if CONFIG_DIR.is_dir() else 'MISSING'}")
    print(f"  cache rows checked        {report.cache_rows_checked}")

    if not report.passed:
        print("FAIL: verify failed")
        for f in report.failures:
            print(f"  - {f}")
        return 2

    print("verify ok")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """Execute one Worker session against a pipeline output directory."""
    if getattr(args, "state_dir", None) is not None:
        sys.modules[__name__].STATE_DIR = args.state_dir
        # Also propagate to the session module so its module-level
        # STATE_DIR / RUNS_DIR / CACHE_PATH / PROGRESS_PATH constants
        # (which Session() reads in its constructor) point at the
        # caller-supplied directory.
        from eval_agent.orchestration import session as session_mod  # noqa: PLC0415
        session_mod.STATE_DIR = args.state_dir
        session_mod.RUNS_DIR = args.state_dir / "runs"
        session_mod.CACHE_PATH = args.state_dir / "cache" / "verdict_cache.jsonl"
        session_mod.PROGRESS_PATH = args.state_dir / "progress.md"

    from eval_agent.orchestration.session import Session, SessionConfig, _load_defaults  # noqa: PLC0415
    from eval_agent import ui  # noqa: PLC0415

    defaults = _load_defaults()
    try:
        config = SessionConfig.from_args(args, defaults)
    except ValueError as exc:
        ui.error(str(exc))
        return 2

    session = Session(config)
    session.startup()
    try:
        verdicts = session.execute()
    except FileNotFoundError as exc:
        ui.error(str(exc))
        return 2

    if not verdicts and config.dry_run:
        return 0
    if not verdicts:
        ui.warn("no verdicts produced (no candidates above threshold)")
        return 0

    run_dir = session.checkpoint(verdicts)
    session.finalize()

    # ── Self-verify (5% re-judge) ──
    sv_summary = None
    if not getattr(args, "no_self_verify", False):
        try:
            from eval_agent.orchestration.self_verify import SelfVerifier  # noqa: PLC0415
            sv_cfg = defaults.get("self_verify", {})
            verifier = SelfVerifier(
                sample_rate=float(sv_cfg.get("sample_rate", 0.05)),
                agreement_floor=float(sv_cfg.get("agreement_floor", 0.95)),
            )
            sv_result = verifier.run(verdicts, judge=session._judge, run_dir=run_dir)
            tag = "PASS" if sv_result.passed else "FAIL"
            sv_summary = (f"{sv_result.agreements}/{sv_result.sample_size} agree "
                          f"({sv_result.agreement_rate:.0%}, {tag})")
        except Exception as exc:  # noqa: BLE001
            sv_summary = f"skipped ({exc})"

    # ── Feature-list ledger update ──
    fl_summary = None
    feature_list_path = STATE_DIR / "feature_list.json"
    if feature_list_path.is_file():
        try:
            from eval_agent.orchestration import feature_list as fl  # noqa: PLC0415
            pf_cfg = defaults.get("passes_floor", {})
            if isinstance(pf_cfg, dict):
                floor = float(pf_cfg.get("default", 0.80))
            else:
                floor = float(pf_cfg)
            fl.update_status_from_run(
                feature_list_path=feature_list_path, run_dir=run_dir, precision_floor=floor,
            )
            fl_summary = "updated"
        except Exception as exc:  # noqa: BLE001
            fl_summary = f"skipped ({exc})"

    # ── Final summary block ──
    ui.section("Summary")
    rows: list[tuple[str, object]] = [
        ("full", session.stats.judged_full),
        ("partial", session.stats.judged_partial),
        ("fail", session.stats.judged_fail),
        ("errors", session.stats.judged_error),
    ]
    if sv_summary:
        rows.append(("self-verify", sv_summary))
    if fl_summary:
        rows.append(("feature_list", fl_summary))
    ui.summary_table(rows)

    ui.section("Artefacts")
    ui.bullet(f"results.jsonl  {run_dir / 'results.jsonl'}")
    ui.bullet(f"summary.csv    {run_dir / 'summary.csv'}")
    ui.bullet(f"report.md      {run_dir / 'report.md'}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    """Regenerate report.md from a run's results.jsonl (no Gemini calls)."""
    import json  # noqa: PLC0415

    from eval_agent.evaluators._base import Verdict  # noqa: PLC0415
    from eval_agent.report.markdown_report import write_markdown  # noqa: PLC0415

    runs_dir = STATE_DIR / "runs"
    if not runs_dir.is_dir():
        print("No runs dir present.", file=sys.stderr)
        return 2
    if args.run == "latest":
        candidates = sorted(p.name for p in runs_dir.iterdir() if p.is_dir())
        if not candidates:
            print("No runs found.", file=sys.stderr)
            return 2
        run_id = candidates[-1]
    else:
        run_id = args.run
    run_dir = runs_dir / run_id
    results = run_dir / "results.jsonl"
    if not results.is_file():
        print(f"No results.jsonl under {run_dir}", file=sys.stderr)
        return 2

    verdicts = []
    for line in results.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        v = Verdict(
            record_id=rec.get("record_id", ""),
            evaluator_id=rec.get("evaluator_id", ""),
            sub_type=rec.get("sub_type") or "",
            candidate_payload=rec.get("candidate", {}),
            confidence=float(rec.get("confidence", 0.0)),
            name_ok=rec["verdict"].get("name_ok", "no"),
            type_ok=rec["verdict"].get("type_ok", "no"),
            role_ok=rec["verdict"].get("role_ok", "n/a"),
            overall=rec["verdict"].get("overall", "fail"),
            reasoning=rec["verdict"].get("reasoning", ""),
            error=rec.get("error"),
            judge_id=rec.get("judge_id", ""),
            cache_key=rec.get("cache_key", ""),
            judged_at=rec.get("judged_at", ""),
        )
        verdicts.append(v)

    out = run_dir / "report.md"
    write_markdown(
        out, verdicts,
        title=f"eval-agent run {run_id} (regenerated)",
        judge_id=verdicts[0].judge_id if verdicts else "",
    )
    print(f"Wrote {out}")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    """Compare two runs' results.jsonl and report precision regressions."""
    from eval_agent.report.diff_runs import diff_runs, write_diff_markdown  # noqa: PLC0415

    # Resolve STATE_DIR lazily so test monkeypatching of cli.STATE_DIR works.
    state_dir = sys.modules[__name__].STATE_DIR
    runs_dir = state_dir / "runs"
    if not runs_dir.is_dir():
        print(f"ERROR: runs dir missing at {runs_dir}", file=sys.stderr)
        return 2

    from_id = args.from_run
    to_id = args.to
    from_dir = runs_dir / from_id
    to_dir = runs_dir / to_id
    if not from_dir.is_dir():
        print(f"ERROR: from-run dir missing: {from_dir}", file=sys.stderr)
        return 2
    if not to_dir.is_dir():
        print(f"ERROR: to-run dir missing: {to_dir}", file=sys.stderr)
        return 2

    diff = diff_runs(from_run_dir=from_dir, to_run_dir=to_dir)
    out = from_dir / f"diff_to_{to_id}.md"
    write_diff_markdown(diff, out)

    print(f"eval-agent diff: {from_id} -> {to_id}")
    print(f"  features:   {len(diff.features)}")
    print(f"  regressed:  {diff.n_regressed}")
    print(f"  improved:   {diff.n_improved}")
    print(f"  report:     {out}")
    return 1 if diff.n_regressed > 0 else 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    """Compute per (evaluator, sub_type) confidence thresholds from a run."""
    from eval_agent import calibrate as calibrate_mod  # noqa: PLC0415
    from eval_agent import ui  # noqa: PLC0415

    # Resolve STATE_DIR lazily so test monkeypatching of cli.STATE_DIR works.
    state_dir = sys.modules[__name__].STATE_DIR
    runs_dir = state_dir / "runs"
    if not runs_dir.is_dir():
        print(f"ERROR: runs dir missing at {runs_dir}", file=sys.stderr)
        return 2

    if args.run == "latest":
        candidates = sorted(p.name for p in runs_dir.iterdir() if p.is_dir())
        if not candidates:
            print("No runs found.", file=sys.stderr)
            return 2
        run_id = candidates[-1]
    else:
        run_id = args.run

    run_dir = runs_dir / run_id
    if not run_dir.is_dir():
        print(f"ERROR: run dir missing: {run_dir}", file=sys.stderr)
        return 2

    try:
        report = calibrate_mod.calibrate_from_run(
            run_dir=run_dir,
            target_precision=args.target_precision,
            floor_threshold=args.floor,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    out_path = Path(args.out) if args.out else (run_dir / "per_sub_type_thresholds.yaml")
    calibrate_mod.write_yaml(report, out_path)

    ui.header(f"eval-agent calibrate · run {report.run_id}")
    ui.kv("target_precision", f"{report.target_precision:.2f}")
    ui.kv("floor_threshold", f"{report.floor_threshold:.2f}")
    ui.kv("buckets", len(report.buckets))
    ui.kv("output", out_path)

    ui.section("Per-bucket thresholds")
    rows: list[tuple[str, object]] = []
    for b in report.buckets:
        label = f"{b.evaluator_id}.{b.sub_type or '_'}"
        status = "ok" if b.target_reached else "below-target"
        value = (
            f"t={b.threshold:.2f}  p={b.precision_at_threshold:.2f}  "
            f"n={b.n_above_threshold}/{b.n_total}  [{status}]"
        )
        rows.append((label, value))
    ui.summary_table(rows)
    return 0


def _cmd_recover(_args: argparse.Namespace) -> int:
    """Rebuild verdict cache + bootstrap state files from ``state/runs/``."""
    from eval_agent.orchestration import recover as recover_mod  # noqa: PLC0415

    # Resolve STATE_DIR lazily so test monkeypatching of cli.STATE_DIR works.
    state_dir = sys.modules[__name__].STATE_DIR
    report = recover_mod.recover(state_dir=state_dir)
    print("eval-agent recover — rebuilding state")
    print(f"  state_dir:                  {state_dir}")
    print(f"  cache_rebuilt:              {report.cache_rebuilt}")
    print(f"  cache_entries_recovered:    {report.cache_entries_recovered}")
    print(f"  feature_list_bootstrapped:  {report.feature_list_bootstrapped}")
    print(f"  progress_md_bootstrapped:   {report.progress_md_bootstrapped}")
    return 0


def _cmd_orchestrate(args: argparse.Namespace) -> int:
    """LLM-orchestrator session (Phase 1 — read-only).

    Builds an :class:`Orchestrator` with the appropriate judge (real
    Gemini or the deterministic stub) and runs one session against the
    project state. Streams ``[TRACE]`` lines for live UI integrators
    (the MHM Pipeline web bridge tails them via SSE).
    """
    import json as _json   # local alias — keep top-level imports clean
    import os
    import sys as _sys

    from eval_agent.orchestrator import (  # noqa: PLC0415
        Budget, MODE_PLAN_ONLY, Orchestrator, StubJudge,
    )

    state_dir = _sys.modules[__name__].STATE_DIR
    if args.state_dir is not None:
        state_dir = Path(args.state_dir)

    # Pick a judge.
    if args.no_llm:
        # Useful for testing the loop + tools without a key. We emit
        # the smallest possible script: inspect_state → final.
        script = [
            {"thought_summary": "no-llm: kick off with state snapshot",
             "final": False, "action": {"tool": "inspect_state", "args": {}}},
            {"thought_summary": "no-llm: summarise feature list",
             "final": False, "action": {"tool": "summarize_feature_list", "args": {}}},
            {"thought_summary": "no-llm: wrap up with stub recommendation",
             "final": True,
             "final_report": {
                 "summary": "Stub session — Phase 1 plumbing smoke. No LLM "
                            "was queried; rerun without --no-llm for a real "
                            "recommendation.",
                 "recommended_next_steps": [],
                 "risks": ["This was a stub run; no decisions are model-backed."],
                 "commands": [],
                 "evidence_paths": [],
             }},
        ]
        judge_fn = StubJudge(script=script)
    else:
        from eval_agent.orchestrator.gemini_judge import (  # noqa: PLC0415
            DEFAULT_JUDGE_MODEL, build_gemini_llm,
        )
        api_key = args.api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("Gemini API key required. Set GEMINI_API_KEY or pass "
                  "--api-key, or use --no-llm for a deterministic dry run.",
                  file=_sys.stderr)
            return 2
        judge_fn = build_gemini_llm(
            api_key=api_key,
            model=args.judge or DEFAULT_JUDGE_MODEL,
            rpm=args.rpm or 60,
        )

    budget = Budget(
        max_steps=args.max_steps,
        max_seconds=args.max_seconds,
        max_usd=args.max_usd,
    )

    # Phase 1 only allows the plan-only allowlist. Future phases extend
    # ALLOW_BY_MODE; the CLI flag here exists so the schema is stable
    # but Phase-2+ modes refuse politely (empty allowlist).
    mode = MODE_PLAN_ONLY
    if args.supervised:
        from eval_agent.orchestrator import MODE_SUPERVISED  # noqa: PLC0415
        mode = MODE_SUPERVISED
    elif args.autonomous:
        from eval_agent.orchestrator import MODE_AUTONOMOUS  # noqa: PLC0415
        mode = MODE_AUTONOMOUS

    def _emit(ev: dict) -> None:
        # Single-line JSON prefixed with "[TRACE]" so subprocess
        # integrators can filter their stdout. Mirrors the existing
        # "[STEP]"/"[STATS]" convention used by the candidate-level loop.
        try:
            print("[TRACE] " + _json.dumps(ev, ensure_ascii=False), flush=True)
        except Exception:  # noqa: BLE001
            pass

    print(f"[STEP] orchestrator starting mode={mode!r} goal={args.goal!r}",
          flush=True)
    orch = Orchestrator(
        judge=judge_fn, state_dir=state_dir, goal=args.goal,
        mode=mode,
        budget=budget,
        pipeline_root=args.pipeline_root,
        pipeline_output=args.pipeline_output,
        api_key=args.api_key or os.environ.get("GEMINI_API_KEY", ""),
        on_step=_emit,
    )
    result = orch.run()
    print(f"[STEP] orchestrator done outcome={result.outcome!r} "
          f"steps={result.steps_used} wall={result.wall_seconds:.2f}s",
          flush=True)
    print(f"session_dir: {result.session_dir}")
    print(f"final_report: {result.session_dir / 'final_report.md'}")
    return 0 if result.outcome == "final" else 3


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="eval-agent",
        description="Long-running Gemini-based evaluation agent for the MHM Pipeline.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="health check")
    sub.add_parser("verify", help="session-startup pre-flight")

    p_run = sub.add_parser("run", help="evaluate a pipeline output")
    p_run.add_argument("--pipeline-output", required=True,
                       help="path to pipeline eval/work/ folder containing "
                            "marc_extracted.json and ner_results.json")
    # Defaults are None so the config file (config/default.yaml) wins when
    # the user doesn't explicitly pass a flag. SessionConfig.from_args
    # falls back to config values when the arg is None.
    p_run.add_argument("--threshold", type=float, default=None,
                       help="confidence floor (default: from config/default.yaml)")
    p_run.add_argument("--rpm", type=int, default=None,
                       help="global RPM cap (default: from config/default.yaml)")
    p_run.add_argument("--parallel", type=int, default=None,
                       help="worker pool size (default: from config/default.yaml)")
    p_run.add_argument("--evaluators", default="all",
                       help="comma-separated evaluator ids, or 'all' (default)")
    p_run.add_argument("--judge", default=None,
                       help="override judge model id (default: from config/default.yaml)")
    p_run.add_argument("--api-key", default=None,
                       help="Gemini API key (default: GEMINI_API_KEY env, then getpass)")
    p_run.add_argument("--dry-run", action="store_true",
                       help="extract candidates + print counts; no Gemini calls")
    p_run.add_argument("--resume", action="store_true",
                       help="(Phase 2) resume an interrupted run")
    p_run.add_argument("--no-self-verify", action="store_true",
                       help="skip the 5%% re-judge self-verification pass after the run")
    p_run.add_argument("--no-cache", action="store_true",
                       help="bypass the verdict cache on reads (every candidate is "
                            "freshly judged). Fresh verdicts still overwrite the "
                            "cache entry — use when you want to measure judge "
                            "non-determinism on the full corpus, not just the "
                            "5%% self-verify sample.")
    p_run.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Override EVAL_AGENT_STATE_DIR / built-in default. Used by "
             "integrators (e.g. MHM Pipeline bundle) to point at a writable per-user dir.",
    )
    # ── Agentic mode (default = gated agentic) ────────────────────────
    p_run.add_argument("--linear", action="store_true",
                       help="disable agency: single-shot judge per candidate "
                            "(the reproducible / citable path).")
    p_run.add_argument("--agentic-all", action="store_true",
                       help="run the tool-loop on EVERY candidate (vs the default "
                            "gated mode that only escalates abstain/partial cases).")
    p_run.add_argument("--agentic-max-steps", type=int, default=None,
                       help="max tool-loop steps per candidate (default 6).")
    p_run.add_argument("--tier-model", default=None,
                       help="tier-1 model for the cheap pass (default gemini-3.5-flash).")
    p_run.add_argument("--escalate-model", default=None,
                       help="model the loop escalates to when still uncertain "
                            "(default gemini-3.1-pro-preview).")

    p_report = sub.add_parser("report", help="regenerate report from a run")
    p_report.add_argument("--run", default="latest")

    p_diff = sub.add_parser("diff", help="compare two runs")
    p_diff.add_argument("--from", dest="from_run", required=True)
    p_diff.add_argument("--to", required=True)

    sub.add_parser("recover", help="safe-mode: rebuild state from cache + git")

    p_calibrate = sub.add_parser(
        "calibrate",
        help="emit per (evaluator, sub_type) confidence thresholds from a run",
    )
    p_calibrate.add_argument("--run", default="latest",
                             help="run_id under state/runs/ or 'latest' (default)")
    p_calibrate.add_argument("--target-precision", type=float, default=0.90,
                             dest="target_precision",
                             help="strict-precision target per bucket (default: 0.90)")
    p_calibrate.add_argument("--floor", type=float, default=0.85,
                             help="lowest threshold to ever recommend (default: 0.85)")
    p_calibrate.add_argument("--out", default=None,
                             help="output YAML path (default: "
                                  "state/runs/<run_id>/per_sub_type_thresholds.yaml)")

    # ── Orchestrator (Phase 1: --plan-only by default) ────────────────
    p_orch = sub.add_parser(
        "orchestrate",
        help="LLM-driven orchestrator over the eval-agent state "
             "(Phase 1: read-only)",
    )
    p_orch.add_argument("--goal", required=True,
                        help="natural-language goal for this session, e.g. "
                             "'Should we re-train person_ner.TRANSCRIBER?'")
    # Mode flags — Phase 1 ships --plan-only (default). The others
    # parse but currently produce an empty allowlist (the model will be
    # told no tools are reachable and refused into a no-progress
    # outcome). Reserved so the CLI is stable from here.
    mode = p_orch.add_mutually_exclusive_group()
    mode.add_argument("--plan-only", dest="plan_only", action="store_true",
                      default=True,
                      help="read-only inspection tools (default).")
    mode.add_argument("--supervised", action="store_true",
                      help="explicit opt-in for controlled execution/proposal tools.")
    mode.add_argument("--autonomous", action="store_true",
                      help="explicit opt-in for the full orchestrator tool allowlist.")
    p_orch.add_argument("--judge", default=None,
                        help="override judge model id "
                             "(default: gemini-3.5-flash; Rule 55).")
    p_orch.add_argument("--api-key", default=None,
                        help="Gemini API key (default: GEMINI_API_KEY env).")
    p_orch.add_argument("--rpm", type=int, default=60,
                        help="rate limit for the orchestrator's judge.")
    p_orch.add_argument("--max-steps", type=int, default=12,
                        help="max LLM action steps before forcing a final.")
    p_orch.add_argument("--max-seconds", type=int, default=180,
                        help="wall-clock cap (default 180s).")
    p_orch.add_argument("--max-usd", type=float, default=0.10,
                        help="USD spend cap (default $0.10).")
    p_orch.add_argument("--no-llm", action="store_true",
                        help="run with a deterministic stub Judge — exercises "
                             "the loop + tools + trace without contacting "
                             "Gemini. Used by tests and the web bridge "
                             "integration smoke.")
    p_orch.add_argument(
        "--state-dir", type=Path, default=None,
        help="Override state dir (same semantics as `run --state-dir`).",
    )
    p_orch.add_argument(
        "--pipeline-root",
        type=Path,
        default=None,
        help="Optional MHM Pipeline repo root, used for benchmark-result tools.",
    )
    p_orch.add_argument(
        "--pipeline-output",
        type=Path,
        default=None,
        help="Optional pipeline output directory. Required if an execution-mode "
             "session chooses run_eval_agent.",
    )

    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    dispatch = {
        "doctor": _cmd_doctor,
        "verify": _cmd_verify,
        "run": _cmd_run,
        "report": _cmd_report,
        "diff": _cmd_diff,
        "recover": _cmd_recover,
        "calibrate":   _cmd_calibrate,
        "orchestrate": _cmd_orchestrate,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

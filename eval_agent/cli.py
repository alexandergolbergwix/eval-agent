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
STATE_DIR = REPO_ROOT / "state"
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
    from eval_agent.orchestration.session import Session, SessionConfig, _load_defaults  # noqa: PLC0415

    defaults = _load_defaults()
    try:
        config = SessionConfig.from_args(args, defaults)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    session = Session(config)
    session.startup()
    try:
        verdicts = session.execute()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not verdicts and config.dry_run:
        return 0
    if not verdicts:
        print("No verdicts produced (no candidates above threshold).")
        return 0

    run_dir = session.checkpoint(verdicts)
    session.finalize()

    # Phase 2: self-verify + feature_list status update (best-effort, opt-out via --no-self-verify)
    if not getattr(args, "no_self_verify", False):
        try:
            from eval_agent.orchestration.self_verify import SelfVerifier  # noqa: PLC0415
            sv_cfg = defaults.get("self_verify", {})
            verifier = SelfVerifier(
                sample_rate=float(sv_cfg.get("sample_rate", 0.05)),
                agreement_floor=float(sv_cfg.get("agreement_floor", 0.95)),
            )
            sv_result = verifier.run(verdicts, judge=session._judge, run_dir=run_dir)
            print(f"  self_verify:   {sv_result.agreements}/{sv_result.sample_size} agree "
                  f"({sv_result.agreement_rate:.0%}, {'PASS' if sv_result.passed else 'FAIL'})")
        except Exception as exc:  # noqa: BLE001
            print(f"  self_verify:   skipped ({exc})", file=sys.stderr)

    feature_list_path = STATE_DIR / "feature_list.json"
    if feature_list_path.is_file():
        try:
            from eval_agent.orchestration import feature_list as fl  # noqa: PLC0415
            floor = float(defaults.get("passes_floor", defaults.get("threshold", {}).get("passes_floor", 0.80)))
            fl.update_status_from_run(
                feature_list_path=feature_list_path, run_dir=run_dir, precision_floor=floor,
            )
            print(f"  feature_list:  updated ({feature_list_path})")
        except Exception as exc:  # noqa: BLE001
            print(f"  feature_list:  update skipped ({exc})", file=sys.stderr)

    print()
    print(f"  results.jsonl: {run_dir / 'results.jsonl'}")
    print(f"  summary.csv:   {run_dir / 'summary.csv'}")
    print(f"  report.md:     {run_dir / 'report.md'}")
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

    p_report = sub.add_parser("report", help="regenerate report from a run")
    p_report.add_argument("--run", default="latest")

    p_diff = sub.add_parser("diff", help="compare two runs")
    p_diff.add_argument("--from", dest="from_run", required=True)
    p_diff.add_argument("--to", required=True)

    sub.add_parser("recover", help="safe-mode: rebuild state from cache + git")

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
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

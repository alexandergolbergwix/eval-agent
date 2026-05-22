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
    """Session-startup pre-flight: schemas + cache + fixtures + tests.

    Phase 0: validates the JSON Schema file itself and confirms state
    dirs exist. Phase 2 will add cache integrity + pytest invocation.
    """
    import jsonschema  # noqa: PLC0415 — optional dep, only needed here

    schema_path = SCHEMAS_DIR / "verdict.v1.json"
    if not schema_path.is_file():
        print(f"FAIL: schema missing at {schema_path}")
        return 2
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: schema invalid: {exc}")
        return 2
    print(f"  schemas/verdict.v1.json   ok")
    print(f"  state/                    {'ok' if STATE_DIR.is_dir() else 'MISSING'}")
    print(f"  config/                   {'ok' if CONFIG_DIR.is_dir() else 'MISSING'}")
    print("verify ok (phase 0 checks only)")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    print("eval-agent run — NOT YET IMPLEMENTED in Phase 0.")
    print(f"  --pipeline-output: {args.pipeline_output}")
    print()
    print("Phase 1 will port the candidate-builder + Gemini judge from")
    print("`/Users/alexandergo/Documents/Doctorat/pipeline/scripts/")
    print("evaluate_models_with_gemini.py` into this agent.")
    return 1


def _cmd_report(_args: argparse.Namespace) -> int:
    print("eval-agent report — NOT YET IMPLEMENTED in Phase 0.")
    return 1


def _cmd_diff(_args: argparse.Namespace) -> int:
    print("eval-agent diff — NOT YET IMPLEMENTED in Phase 0.")
    return 1


def _cmd_recover(_args: argparse.Namespace) -> int:
    print("eval-agent recover — NOT YET IMPLEMENTED in Phase 0.")
    return 1


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
    p_run.add_argument("--threshold", type=float, default=0.85)
    p_run.add_argument("--rpm", type=int, default=25)
    p_run.add_argument("--parallel", type=int, default=2)
    p_run.add_argument("--evaluators", default="all")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--resume", action="store_true")

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

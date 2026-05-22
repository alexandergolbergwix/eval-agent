"""Read / write / bootstrap ``state/feature_list.json``.

Phase 0: ``bootstrap`` subcommand that scans ``config/rubrics/`` and
emits one feature per declared (evaluator, sub_type). Phase 2 adds
``update_status`` + ``select_next_task`` helpers used by the worker
session lifecycle.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = REPO_ROOT / "state"
RUBRICS_DIR = REPO_ROOT / "config" / "rubrics"
FEATURE_LIST_PATH = STATE_DIR / "feature_list.json"

# Default sub-types per evaluator. Stays in code for Phase 0 — Phase 1 will
# move these into per-rubric front-matter so the rubric markdown is the
# canonical declaration of an evaluator's surface area.
_DEFAULT_SUBTYPES: dict[str, list[str]] = {
    "person_ner":       ["AUTHOR", "TRANSCRIBER", "TRANSLATOR", "COMMENTATOR",
                         "OWNER", "EDITOR", "CENSOR"],
    "provenance_ner":   ["OWNER", "DATE", "COLLECTION"],
    "contents_ner":     ["WORK", "FOLIO", "WORK_AUTHOR"],
    "genre_classifier": ["Piyyutim", "Poetry", "Illustrated works (Manuscript)",
                         "Personal correspondence", "Censored manuscripts",
                         "Autograph manuscripts", "Records (Documents)",
                         "Bibliographies"],
    "marc500_colophon": ["COLOPHON"],
}


def _empty_status() -> dict[str, Any]:
    return {
        "passes": False,
        "attempts": 0,
        "last_run": None,
        "last_precision": None,
        "notes": "",
    }


def bootstrap(threshold: float = 0.85) -> dict[str, Any]:
    """Generate a fresh feature_list.json (does not overwrite if exists)."""
    if FEATURE_LIST_PATH.exists():
        return json.loads(FEATURE_LIST_PATH.read_text(encoding="utf-8"))
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    features = []
    for evaluator, sub_types in _DEFAULT_SUBTYPES.items():
        for sub in sub_types:
            features.append({
                "id": f"{evaluator}.{sub}",
                "evaluator": evaluator,
                "sub_type": sub,
                "threshold": threshold,
                "status": _empty_status(),
            })

    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "features": features,
    }
    FEATURE_LIST_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
    return payload


def load() -> dict[str, Any]:
    if not FEATURE_LIST_PATH.exists():
        raise FileNotFoundError(f"{FEATURE_LIST_PATH} not bootstrapped — run init.sh first")
    return json.loads(FEATURE_LIST_PATH.read_text(encoding="utf-8"))


def _cli() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "bootstrap":
        print("usage: python -m eval_agent.orchestration.feature_list bootstrap", file=sys.stderr)
        return 2
    payload = bootstrap()
    print(f"feature_list.json: {len(payload['features'])} features bootstrapped")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

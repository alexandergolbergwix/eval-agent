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
    # ``marc500_colophon`` removed 2026-05-23 (6 % strict precision —
    # see eval_agent/evaluators/__init__.py).
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


def update_status_from_run(
    *,
    feature_list_path: Path,
    run_dir: Path,
    precision_floor: float = 0.80,
) -> None:
    """Mutate ``feature_list_path`` in-place to reflect precision from a run.

    Reads ``run_dir / "results.jsonl"``, groups verdicts by
    ``(evaluator_id, sub_type)``, computes precision (fraction with
    ``verdict.overall == "full"`` among non-errored verdicts), and:

      - bumps ``status.attempts`` by 1
      - updates ``status.last_run`` to ``run_dir.name``
      - updates ``status.last_precision``
      - flips ``status.passes`` to True iff precision >= ``precision_floor``

    Features that had zero non-errored verdicts in this run keep their
    previous status unchanged. Features in the run that are not yet in
    the file are appended with ``threshold=0.85``. Features in the file
    that have no verdicts in this run are preserved untouched.

    The write is atomic (temp file + rename).
    """
    payload = json.loads(feature_list_path.read_text(encoding="utf-8"))
    features: list[dict[str, Any]] = list(payload.get("features", []))

    results_path = run_dir / "results.jsonl"
    grouped: dict[tuple[str, str], dict[str, int]] = {}
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("error"):
                continue
            evaluator_id = rec.get("evaluator_id")
            sub_type = rec.get("sub_type")
            if not evaluator_id or sub_type is None:
                continue
            verdict = rec.get("verdict") or {}
            overall = verdict.get("overall")
            key = (str(evaluator_id), str(sub_type))
            bucket = grouped.setdefault(key, {"total": 0, "full": 0})
            bucket["total"] += 1
            if overall == "full":
                bucket["full"] += 1

    precisions: dict[tuple[str, str], float] = {}
    for key, bucket in grouped.items():
        if bucket["total"] > 0:
            precisions[key] = bucket["full"] / bucket["total"]

    existing_keys: set[tuple[str, str]] = set()
    for feature in features:
        evaluator = str(feature.get("evaluator", ""))
        sub_type = str(feature.get("sub_type", ""))
        key = (evaluator, sub_type)
        existing_keys.add(key)
        if key not in precisions:
            continue
        precision = precisions[key]
        status = feature.setdefault("status", _empty_status())
        status["attempts"] = int(status.get("attempts", 0)) + 1
        status["last_run"] = run_dir.name
        status["last_precision"] = precision
        status["passes"] = precision >= precision_floor

    for key, precision in precisions.items():
        if key in existing_keys:
            continue
        evaluator, sub_type = key
        features.append({
            "id": f"{evaluator}.{sub_type}",
            "evaluator": evaluator,
            "sub_type": sub_type,
            "threshold": 0.85,
            "status": {
                "passes": precision >= precision_floor,
                "attempts": 1,
                "last_run": run_dir.name,
                "last_precision": precision,
                "notes": "",
            },
        })

    payload["features"] = features

    tmp_path = feature_list_path.with_suffix(feature_list_path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(feature_list_path)


def _cli() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "bootstrap":
        print("usage: python -m eval_agent.orchestration.feature_list bootstrap", file=sys.stderr)
        return 2
    payload = bootstrap()
    print(f"feature_list.json: {len(payload['features'])} features bootstrapped")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

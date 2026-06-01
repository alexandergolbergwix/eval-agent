"""Read and compress eval-agent + benchmark state for the orchestrator."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def default_state_dir() -> Path:
    """Resolve the eval-agent state directory."""
    env = os.environ.get("EVAL_AGENT_STATE_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "state"


def latest_child(directory: Path) -> Path | None:
    """Return the lexicographically latest child directory."""
    if not directory.is_dir():
        return None
    children = sorted(p for p in directory.iterdir() if p.is_dir())
    return children[-1] if children else None


def read_text_if_exists(path: Path, *, limit: int = 6000) -> str:
    """Read a text file with a bounded character budget."""
    if not path.is_file():
        return f"(missing: {path})"
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit:
        return text[-limit:]
    return text


def load_json_if_exists(path: Path) -> dict[str, Any]:
    """Load a JSON object or return a diagnostic object."""
    if not path.is_file():
        return {"missing": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"error": f"invalid json: {exc}", "path": str(path)}
    return data if isinstance(data, dict) else {"value": data}


def summarize_state(*, state_dir: Path, pipeline_root: Path | None = None) -> str:
    """Return a compact state summary for the orchestrator prompt."""
    runs_dir = state_dir / "runs"
    latest_run = latest_child(runs_dir)
    feature_list = load_json_if_exists(state_dir / "feature_list.json")
    progress_tail = read_text_if_exists(state_dir / "progress.md", limit=2500)
    lines = [
        f"state_dir: {state_dir}",
        f"latest_eval_agent_run: {latest_run.name if latest_run else '(none)'}",
        f"feature_list_summary: {_summarize_feature_list(feature_list)}",
        "progress_tail:",
        progress_tail,
    ]
    if pipeline_root is not None:
        person_latest = latest_child(
            pipeline_root / "eval" / "gemini_benchmark" / "results" / "person_ner"
        )
        lines.append(
            "latest_person_benchmark: "
            + (str(person_latest) if person_latest else "(none)")
        )
    return "\n".join(lines)


def compact_state_summary(state_dir: Path) -> str:
    """Compatibility wrapper used by the orchestrator loop."""
    return summarize_state(state_dir=state_dir)


def _summarize_feature_list(data: dict[str, Any]) -> str:
    features = data.get("features")
    if not isinstance(features, list):
        return json.dumps(data, ensure_ascii=False)[:1200]
    total = len(features)
    passing = sum(1 for f in features if isinstance(f, dict) and f.get("passes"))
    failing = total - passing
    sample = []
    for f in features[:12]:
        if isinstance(f, dict):
            sample.append(
                f"{f.get('id')} pass={f.get('passes')} "
                f"precision={f.get('last_precision')}"
            )
    return f"features={total}, passing={passing}, failing={failing}; sample={sample}"


__all__ = [
    "latest_child",
    "compact_state_summary",
    "default_state_dir",
    "load_json_if_exists",
    "read_text_if_exists",
    "summarize_state",
]

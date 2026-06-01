"""Append-only traces for orchestrator sessions."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_dir(state_dir: Path) -> Path:
    """Create and return ``state/orchestrator/sessions/<timestamp>``."""
    root = state_dir / "orchestrator" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    while True:
        path = root / _utc_stamp()
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue


class TraceWriter:
    """Thread-safe JSONL writer for one orchestrator session."""

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.trace_path = session_dir / "trace.jsonl"
        self._lock = threading.Lock()

    def event(self, event_type: str, **payload: Any) -> dict[str, Any]:
        row = {"ts": _iso_now(), "type": event_type, **payload}
        self._append(row)
        return row

    def session_start(
        self,
        *,
        session_id: str,
        mode: str,
        goal: str,
        allowlist: list[str],
        budget: dict[str, Any],
    ) -> dict[str, Any]:
        return self.event(
            "session.start",
            session_id=session_id,
            mode=mode,
            goal=goal,
            allowlist=allowlist,
            budget=budget,
        )

    def llm_turn(
        self,
        *,
        raw: dict[str, Any],
        parsed_kind: str,
        thought_summary: str,
    ) -> dict[str, Any]:
        return self.event(
            "llm.turn",
            parsed_kind=parsed_kind,
            thought_summary=thought_summary,
            raw=raw,
        )

    def policy_refuse(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        reason: str,
        detail: str,
    ) -> dict[str, Any]:
        return self.event(
            "policy.refuse",
            tool=tool,
            args=args,
            reason=reason,
            detail=detail,
        )

    def tool_dispatch(self, *, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.event("tool.dispatch", tool=tool, args=args)

    def tool_result(
        self,
        *,
        tool: str,
        ok: bool,
        summary: str,
        data: dict[str, Any],
        error: str | None = None,
    ) -> dict[str, Any]:
        return self.event(
            "tool.result",
            tool=tool,
            ok=ok,
            summary=summary,
            data=data,
            error=error,
        )

    def session_final(self, *, final: dict[str, Any]) -> dict[str, Any]:
        return self.event("session.final", final=final)

    def session_end(
        self,
        *,
        outcome: str,
        steps_used: int,
        usd_used: float,
        wall_seconds: float,
    ) -> dict[str, Any]:
        return self.event(
            "session.end",
            outcome=outcome,
            steps_used=steps_used,
            usd_used=usd_used,
            wall_seconds=wall_seconds,
        )

    def _append(self, row: dict[str, Any]) -> None:
        with self._lock:
            with self.trace_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def replay_trace(session_dir: Path) -> Iterable[dict[str, Any]]:
    """Yield trace rows from a session directory."""
    path = session_dir / "trace.jsonl"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def write_decisions(session_dir: Path, decisions: list[dict[str, Any]]) -> Path:
    """Write one decision JSON object per line."""
    path = session_dir / "decisions.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for decision in decisions:
            fh.write(json.dumps(decision, ensure_ascii=False) + "\n")
    return path


def write_final_report(session_dir: Path, body: str) -> Path:
    """Write the orchestrator's final markdown report."""
    path = session_dir / "final_report.md"
    path.write_text(body, encoding="utf-8")
    return path


@dataclass
class OrchestratorTrace:
    """Small compatibility wrapper for direct trace appenders."""

    session_dir: Path
    steps: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(cls, state_dir: Path) -> "OrchestratorTrace":
        return cls(session_dir=new_session_dir(state_dir))

    @property
    def trace_path(self) -> Path:
        return self.session_dir / "trace.jsonl"

    @property
    def decisions_path(self) -> Path:
        return self.session_dir / "decisions.jsonl"

    @property
    def final_report_path(self) -> Path:
        return self.session_dir / "final_report.md"

    def add(self, row: dict[str, Any]) -> None:
        record = {"ts": _iso_now(), **row}
        self.steps.append(record)
        with self.trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        if "action" in record:
            with self.decisions_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record["action"], ensure_ascii=False) + "\n")

    def write_final_report(self, report: dict[str, Any]) -> None:
        lines = [
            "# eval-agent orchestrator report",
            "",
            str(report.get("summary", "")),
            "",
            "## Recommended next steps",
        ]
        for item in report.get("recommended_next_steps", []) or []:
            lines.append(f"- {item}")
        lines.extend(["", "## Risks"])
        for item in report.get("risks", []) or []:
            lines.append(f"- {item}")
        lines.extend(["", "## Commands"])
        for item in report.get("commands", []) or []:
            lines.append(f"- `{item}`")
        lines.extend(["", "## Evidence"])
        for item in report.get("evidence_paths", []) or []:
            lines.append(f"- `{item}`")
        write_final_report(self.session_dir, "\n".join(lines) + "\n")


__all__ = [
    "OrchestratorTrace",
    "TraceWriter",
    "new_session_dir",
    "replay_trace",
    "write_decisions",
    "write_final_report",
]

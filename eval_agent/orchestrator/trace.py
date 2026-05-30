"""Trace + session output writers.

A trace is an append-only ``trace.jsonl`` recording every action the
orchestrator took, every observation it received, and every refusal
the policy issued. The web bridge tails this file for the live UI.

The trace is also the audit log: a Phase 4 autonomous run that
mis-behaves can be replayed step-by-step from the trace alone.

Event types (one per line, ``ts`` always ISO-8601 UTC):

  ``session.start``  — session_id, mode, goal, allowlist, budget
  ``llm.turn``       — LLM's raw JSON action (parsed + thought_summary)
  ``policy.refuse``  — Refusal reason + detail, with the tool/args that
                        triggered it
  ``tool.dispatch``  — tool name + args (about to run)
  ``tool.result``    — tool's Observation (ok / summary / data / error)
  ``session.final``  — final report from the LLM
  ``session.end``    — stats + outcome
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


@dataclass
class TraceWriter:
    """Threadsafe append-only writer for one session's trace.jsonl.

    Threadsafety matters because the web SSE bridge spawns a tail
    thread that reads while the orchestrator thread writes.
    """

    session_dir: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        # Touch so SSE tailers don't 404 between the first
        # consumer connect and the first write.
        (self.session_dir / "trace.jsonl").touch()

    def event(self, type_: str, **payload: Any) -> dict[str, Any]:
        """Write one event and return it as a dict.

        The returned dict mirrors what's on disk so the loop can also
        keep an in-memory transcript (for the prompt's
        ``observations`` window).
        """
        ev = {
            "ts":   datetime.now(timezone.utc).isoformat(),
            "type": type_,
            **payload,
        }
        line = json.dumps(ev, ensure_ascii=False, default=_json_default)
        with self._lock:
            with (self.session_dir / "trace.jsonl").open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        return ev

    # ── Convenience event helpers — keep call sites short. ──────────────

    def session_start(
        self, *, session_id: str, mode: str, goal: str,
        allowlist: list[str], budget: dict[str, Any],
    ) -> dict[str, Any]:
        return self.event(
            "session.start",
            session_id=session_id, mode=mode, goal=goal,
            allowlist=allowlist, budget=budget,
        )

    def llm_turn(
        self, *, raw: dict[str, Any], parsed_kind: str, thought_summary: str,
    ) -> dict[str, Any]:
        return self.event(
            "llm.turn", raw=raw, kind=parsed_kind,
            thought_summary=thought_summary,
        )

    def policy_refuse(
        self, *, tool: str, args: dict[str, Any], reason: str, detail: str,
    ) -> dict[str, Any]:
        return self.event(
            "policy.refuse", tool=tool, args=args, reason=reason, detail=detail,
        )

    def tool_dispatch(self, *, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.event("tool.dispatch", tool=tool, args=args)

    def tool_result(
        self, *, tool: str, ok: bool, summary: str,
        data: dict[str, Any], error: str | None = None,
    ) -> dict[str, Any]:
        return self.event(
            "tool.result", tool=tool, ok=ok, summary=summary,
            data=data, error=error,
        )

    def session_final(self, *, final: dict[str, Any]) -> dict[str, Any]:
        return self.event("session.final", final=final)

    def session_end(
        self, *, outcome: str, steps_used: int, usd_used: float,
        wall_seconds: float,
    ) -> dict[str, Any]:
        return self.event(
            "session.end", outcome=outcome,
            steps_used=steps_used, usd_used=usd_used,
            wall_seconds=wall_seconds,
        )


# ── Session-dir helpers ────────────────────────────────────────────────


def new_session_dir(state_dir: Path) -> Path:
    """Create a fresh ``state/orchestrator/sessions/<ts>/`` and return it."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out = state_dir / "orchestrator" / "sessions" / ts
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_decisions(session_dir: Path, decisions: list[dict[str, Any]]) -> None:
    """Mirror the action stream (parsed turns only) into decisions.jsonl.

    The trace has everything, including raw LLM JSON and the full
    observation payloads. decisions.jsonl is a curated reading: just
    the parsed action / final-report turns the LLM committed to.
    """
    with (session_dir / "decisions.jsonl").open("w", encoding="utf-8") as fp:
        for d in decisions:
            fp.write(json.dumps(d, ensure_ascii=False, default=_json_default) + "\n")


def write_final_report(session_dir: Path, body: str) -> Path:
    """Write the LLM-authored final_report.md and return its path."""
    p = session_dir / "final_report.md"
    p.write_text(body, encoding="utf-8")
    return p


# ── Replay (for tests + the web bridge) ────────────────────────────────


def replay_trace(session_dir: Path) -> Iterator[dict[str, Any]]:
    """Yield every event in *session_dir*'s trace.jsonl, in order."""
    p = session_dir / "trace.jsonl"
    if not p.exists():
        return
    with p.open(encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _json_default(value: Any) -> Any:
    """Tolerant default — never fail to write a trace line. Unsupported
    types become their repr."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    try:
        return value.__dict__
    except AttributeError:
        return repr(value)


__all__ = [
    "TraceWriter",
    "new_session_dir",
    "replay_trace",
    "write_decisions",
    "write_final_report",
]

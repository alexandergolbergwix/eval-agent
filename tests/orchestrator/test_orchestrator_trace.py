"""Trace + session-dir tests."""

from __future__ import annotations

import json
from pathlib import Path

from eval_agent.orchestrator.trace import (
    TraceWriter, new_session_dir, replay_trace, write_decisions,
    write_final_report,
)


def test_new_session_dir_creates_unique_paths(tmp_path: Path) -> None:
    a = new_session_dir(tmp_path)
    b = new_session_dir(tmp_path)
    assert a != b
    assert a.parent == tmp_path / "orchestrator" / "sessions"
    assert b.parent == tmp_path / "orchestrator" / "sessions"


def test_writer_appends_one_line_per_event(tmp_path: Path) -> None:
    sess = new_session_dir(tmp_path)
    w = TraceWriter(sess)
    w.session_start(session_id=sess.name, mode="plan_only", goal="g",
                    allowlist=["inspect_state"],
                    budget={"max_steps": 1, "max_seconds": 1, "max_usd": 0.0})
    w.tool_dispatch(tool="inspect_state", args={})
    w.tool_result(tool="inspect_state", ok=True, summary="ok", data={"x": 1})
    w.session_end(outcome="final", steps_used=1, usd_used=0.0,
                  wall_seconds=0.01)
    events = list(replay_trace(sess))
    assert [e["type"] for e in events] == [
        "session.start", "tool.dispatch", "tool.result", "session.end",
    ]
    # Every line carries an ISO timestamp.
    for e in events:
        assert "T" in e["ts"]


def test_write_decisions_emits_one_line_per_decision(tmp_path: Path) -> None:
    sess = new_session_dir(tmp_path)
    write_decisions(sess, [
        {"kind": "action", "tool": "inspect_state"},
        {"kind": "final", "thought_summary": "done"},
    ])
    lines = [
        json.loads(l) for l in
        (sess / "decisions.jsonl").read_text().splitlines() if l.strip()
    ]
    assert lines[0]["tool"] == "inspect_state"
    assert lines[1]["kind"] == "final"


def test_write_final_report_writes_markdown(tmp_path: Path) -> None:
    sess = new_session_dir(tmp_path)
    p = write_final_report(sess, "# hi\n\nbody")
    assert p.exists()
    assert p.read_text().startswith("# hi")


def test_writer_is_threadsafe(tmp_path: Path) -> None:
    import threading
    sess = new_session_dir(tmp_path)
    w = TraceWriter(sess)

    def worker(i: int) -> None:
        for _ in range(20):
            w.event("test", n=i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()

    events = list(replay_trace(sess))
    # 4 threads × 20 events = 80, every one parsed cleanly (no torn lines).
    assert len(events) == 80
    # No mangled JSON in the file.
    assert all(e["type"] == "test" for e in events)

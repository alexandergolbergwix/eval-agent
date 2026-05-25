"""Tests for STATE_DIR resolution: env var, --state-dir flag, fallback.

These tests pin the contract the MHM Pipeline bundle relies on:

  - EVAL_AGENT_STATE_DIR env var, if set, points STATE_DIR at the
    caller-supplied location at module import time.
  - The ``run --state-dir <path>`` CLI flag overrides BOTH the env var
    and the in-tree default.
  - When neither is set, STATE_DIR falls back to ``REPO_ROOT / "state"``.
  - ``ui.emit_stats(...)`` emits a single ``[STATS] ...`` line that the
    MHM Pipeline GUI parses to update its live progress card.
"""

from __future__ import annotations

import importlib
import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# 1. Env var honored at import (cli.STATE_DIR)
# ──────────────────────────────────────────────────────────────────────────────


def test_state_dir_honors_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_AGENT_STATE_DIR", "/tmp/test-state-honors-env")
    import eval_agent.cli as cli_mod  # noqa: PLC0415
    cli_mod = importlib.reload(cli_mod)
    assert cli_mod.STATE_DIR == Path("/tmp/test-state-honors-env")


# ──────────────────────────────────────────────────────────────────────────────
# 2. Fallback to REPO_ROOT / state when env var unset
# ──────────────────────────────────────────────────────────────────────────────


def test_state_dir_falls_back_to_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_AGENT_STATE_DIR", raising=False)
    import eval_agent.cli as cli_mod  # noqa: PLC0415
    cli_mod = importlib.reload(cli_mod)
    assert cli_mod.STATE_DIR == cli_mod.REPO_ROOT / "state"


# ──────────────────────────────────────────────────────────────────────────────
# 3. Same env-var contract on the session module
# ──────────────────────────────────────────────────────────────────────────────


def test_session_module_honors_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_AGENT_STATE_DIR", "/tmp/test-state-session")
    from eval_agent.orchestration import session as session_mod  # noqa: PLC0415
    session_mod = importlib.reload(session_mod)
    assert session_mod.STATE_DIR == Path("/tmp/test-state-session")
    assert session_mod.RUNS_DIR == Path("/tmp/test-state-session") / "runs"
    assert session_mod.CACHE_PATH == (
        Path("/tmp/test-state-session") / "cache" / "verdict_cache.jsonl"
    )
    assert session_mod.PROGRESS_PATH == Path("/tmp/test-state-session") / "progress.md"


# ──────────────────────────────────────────────────────────────────────────────
# 4. --state-dir flag beats EVAL_AGENT_STATE_DIR env var
# ──────────────────────────────────────────────────────────────────────────────


def test_run_state_dir_flag_overrides_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EVAL_AGENT_STATE_DIR", str(tmp_path / "env_path"))

    # Reload so the module-level STATE_DIR picks up the env var first…
    import eval_agent.cli as cli_mod  # noqa: PLC0415
    cli_mod = importlib.reload(cli_mod)
    assert cli_mod.STATE_DIR == tmp_path / "env_path"

    # …then invoke the run subcommand with --state-dir pointing somewhere else.
    flag_path = tmp_path / "flag_path"
    flag_path.mkdir(parents=True, exist_ok=True)

    # Mock the heavy bits so the run handler returns quickly without
    # touching Gemini or the file system beyond our tmp tree.
    pipeline_output = tmp_path / "pipeline_work"
    pipeline_output.mkdir()
    (pipeline_output / "marc_extracted.json").write_text("[]")
    (pipeline_output / "ner_results.json").write_text("[]")

    with patch.object(cli_mod, "_cmd_run", wraps=cli_mod._cmd_run) as wrapped:
        # Bypass the real Session.startup/execute path by mocking Session.
        from eval_agent.orchestration import session as session_mod  # noqa: PLC0415

        fake_session = MagicMock()
        fake_session.execute.return_value = []  # no verdicts → early return
        fake_session.startup.return_value = None
        fake_config = MagicMock()
        fake_config.dry_run = True  # forces no-verdicts early-return path

        with patch.object(session_mod, "Session", return_value=fake_session), \
             patch.object(session_mod.SessionConfig, "from_args", return_value=fake_config), \
             patch.object(session_mod, "_load_defaults", return_value={}):
            rc = cli_mod.main([
                "run",
                "--pipeline-output", str(pipeline_output),
                "--state-dir", str(flag_path),
            ])
        assert wrapped.called
        assert rc == 0

    # The flag wins: cli.STATE_DIR is now flag_path, NOT env_path.
    assert sys.modules["eval_agent.cli"].STATE_DIR == flag_path
    # And the session module's STATE_DIR was propagated too.
    assert sys.modules["eval_agent.orchestration.session"].STATE_DIR == flag_path
    assert sys.modules["eval_agent.orchestration.session"].RUNS_DIR == flag_path / "runs"


# ──────────────────────────────────────────────────────────────────────────────
# 5. ui.emit_stats emits the parseable [STATS] line
# ──────────────────────────────────────────────────────────────────────────────


def test_emit_stats_writes_parseable_line(capsys: pytest.CaptureFixture) -> None:
    from eval_agent import ui  # noqa: PLC0415

    ui.emit_stats(
        candidates_total=10,
        cache_hits=2,
        candidates_judged=5,
        input_tokens=1234,
        output_tokens=567,
    )

    captured = capsys.readouterr()
    line = captured.out.strip()
    assert line.startswith("[STATS] "), f"line does not start with [STATS]: {line!r}"
    assert "total=10" in line
    assert "hits=2" in line
    assert "judged=5" in line
    assert "in_tok=1234" in line
    assert "out_tok=567" in line


def test_emit_stats_defaults_tokens_to_zero(capsys: pytest.CaptureFixture) -> None:
    from eval_agent import ui  # noqa: PLC0415

    ui.emit_stats(candidates_total=7, cache_hits=0, candidates_judged=0)
    captured = capsys.readouterr()
    line = captured.out.strip()
    assert "in_tok=0" in line
    assert "out_tok=0" in line

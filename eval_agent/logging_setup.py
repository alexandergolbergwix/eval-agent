"""Centralised debug logging for the eval-agent.

Two sinks:

1. **File** — always-on, ``DEBUG`` level. Written to
   ``state/logs/eval-agent-YYYY-MM-DD.log``. The file rotates daily
   (one per UTC date) and the loader prunes files older than
   ``LOG_RETENTION_DAYS`` (default 7) on init. This gives us a
   forensic trail without unbounded disk growth.

2. **stderr** — gated by the environment variable ``EVAL_AGENT_DEBUG``
   (``1`` / ``true`` / ``yes`` enable). When disabled, stderr only
   sees ``WARNING`` and above; the file still captures everything.

Safety
------

The logger MUST NEVER serialise an API key, OAuth token, or other
secret. Use ``redact(value)`` for any value that may be sensitive. The
``GeminiJudge`` and ``Session`` already pass authentication through
HTTP headers, never via the prompt body, so the only secret in scope
is the API key string itself — keep it out of structured-log fields
and never print the raw HTTP headers.

Usage
-----

::

    from eval_agent.logging_setup import get_logger, redact
    log = get_logger(__name__)
    log.debug("gemini.request", extra={"model": model, "api_key": redact(key)})

To inspect today's log file::

    tail -f state/logs/eval-agent-$(date -u +%Y-%m-%d).log
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_INITIALISED = False
_REDACTED = "***REDACTED***"

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "state" / "logs"
LOG_RETENTION_DAYS = 7


def _stderr_enabled() -> bool:
    val = os.environ.get("EVAL_AGENT_DEBUG", "").strip().lower()
    return val in {"1", "true", "yes", "on", "debug"}


def _prune_old_logs(directory: Path, *, days: int) -> None:
    """Delete log files older than ``days`` days (mtime-based)."""
    if not directory.is_dir():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = cutoff.timestamp()
    for path in directory.glob("eval-agent-*.log"):
        try:
            if path.stat().st_mtime < cutoff_ts:
                path.unlink()
        except OSError:
            continue


def init() -> None:
    """Initialise the eval-agent logger once per process.

    Wires a daily file handler (always DEBUG) and an optional stderr
    handler (DEBUG when ``EVAL_AGENT_DEBUG`` is set, WARNING otherwise).
    Prunes log files older than ``LOG_RETENTION_DAYS`` before opening
    today's file.
    """
    global _INITIALISED  # noqa: PLW0603
    if _INITIALISED:
        return

    root = logging.getLogger("eval_agent")
    root.setLevel(logging.DEBUG)  # let handlers decide what to emit

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # File sink — always DEBUG, daily rotation by filename.
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _prune_old_logs(LOG_DIR, days=LOG_RETENTION_DAYS)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_path = LOG_DIR / f"eval-agent-{today}.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        # Filesystem unavailable (read-only mount, missing parent) — fall
        # through to stderr-only so the agent still runs.
        pass

    # stderr sink — gated by EVAL_AGENT_DEBUG.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.DEBUG if _stderr_enabled() else logging.WARNING)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    _INITIALISED = True


def get_logger(name: str) -> logging.Logger:
    init()
    return logging.getLogger(name)


def redact(value: Any) -> str:
    """Return a non-sensitive placeholder for a secret value.

    Always returns ``***REDACTED***`` so logs cannot leak the secret
    even if a stray ``str(value)`` reaches a handler.
    """
    if value is None or value == "":
        return "<unset>"
    return _REDACTED


def truncate(text: str, limit: int = 200) -> str:
    """Truncate a possibly-long string for safe logging."""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


__all__ = ["init", "get_logger", "redact", "truncate",
           "LOG_DIR", "LOG_RETENTION_DAYS"]

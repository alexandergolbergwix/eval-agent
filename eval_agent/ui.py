"""User-facing CLI output — clean, single-purpose, no debug noise.

This module is for the *human* reading the terminal. All structured
debug logging goes through ``logging_setup`` (file always-on, stderr
on ``EVAL_AGENT_DEBUG=1``) and never appears here.

Helpers:

- ``header(title)``      — bold cyan banner
- ``kv(label, value)``   — aligned key/value row
- ``section(title)``     — minor section heading
- ``info(text)``         — neutral line
- ``ok(text)``           — green ✓ check
- ``warn(text)``         — yellow ! mark
- ``error(text)``        — red ✗ mark (stderr)
- ``bullet(text)``       — indented · point
- ``progress_line(...)`` — single-line live updater (uses \\r when tty)
- ``done_line()``        — finalises the live updater with a newline

Colour rules:

- Colours are enabled iff stdout is a TTY and ``NO_COLOR`` is unset.
- Use ANSI directly (no ``rich``/``colorama`` dependency).
- Failure colours go to stderr; success/info to stdout.
"""

from __future__ import annotations

import os
import sys

# ── ANSI primitives ──────────────────────────────────────────────────────

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_CYAN = "\x1b[36m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_GREY = "\x1b[90m"


def _use_colour(stream: "object" = sys.stdout) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _wrap(text: str, code: str, *, stream: "object" = sys.stdout) -> str:
    if not _use_colour(stream):
        return text
    return f"{code}{text}{_RESET}"


# ── Public surface ───────────────────────────────────────────────────────


def header(title: str) -> None:
    """Top-of-output banner. Single Unicode rule above and below."""
    line = "─" * max(60, len(title) + 4)
    print(_wrap(line, _GREY))
    print(_wrap(f" {title}", _BOLD + _CYAN))
    print(_wrap(line, _GREY))


def section(title: str) -> None:
    """Minor section heading — blank line + bold label."""
    print()
    print(_wrap(title, _BOLD))


def kv(label: str, value: object, *, width: int = 16) -> None:
    """Aligned key/value row (label dimmed, value plain)."""
    print(f"  {_wrap(label.ljust(width), _DIM)}{value}")


def info(text: str) -> None:
    print(text)


def bullet(text: str, *, indent: int = 2) -> None:
    print(" " * indent + _wrap("·", _GREY) + " " + text)


def ok(text: str) -> None:
    print(_wrap("✓", _GREEN) + " " + text)


def warn(text: str) -> None:
    print(_wrap("!", _YELLOW) + " " + text)


def error(text: str) -> None:
    print(_wrap("✗", _RED) + " " + text, file=sys.stderr)


def progress_line(current: int, total: int, *, elapsed: float, errors: int = 0,
                  width: int = 30) -> None:
    """Single in-place progress line (uses \\r when stdout is a TTY).

    When not a TTY (piped / log-redirected), prints a normal line every
    invocation instead — caller is expected to throttle.
    """
    pct = current / total if total > 0 else 0.0
    bar_full = int(round(pct * width))
    bar = "█" * bar_full + "░" * (width - bar_full)
    pct_text = f"{pct * 100:5.1f}%"
    rate = current / elapsed if elapsed > 0 else 0.0
    eta = (total - current) / rate if rate > 0 else 0.0
    err_note = (
        " " + _wrap(f"({errors} err)", _YELLOW) if errors else ""
    )
    msg = (
        f"  [{_wrap(bar, _CYAN)}] {pct_text} "
        f"{current}/{total}  "
        f"{_wrap(f'{elapsed:.0f}s elapsed', _DIM)}  "
        f"{_wrap(f'~{eta:.0f}s left', _DIM)}"
        f"{err_note}"
    )
    if _use_colour():
        # carriage-return updates in place
        sys.stdout.write("\r" + msg)
        sys.stdout.flush()
    else:
        print(msg)


def done_line() -> None:
    """Terminate an in-place progress line with a newline."""
    if _use_colour():
        sys.stdout.write("\n")
        sys.stdout.flush()


def summary_table(rows: list[tuple[str, object]], *, indent: int = 2) -> None:
    """Print a small two-column summary table."""
    if not rows:
        return
    label_w = max(len(r[0]) for r in rows)
    for label, value in rows:
        print(" " * indent + _wrap(label.ljust(label_w), _DIM) + "  " + str(value))


def emit_stats(*, candidates_total: int, cache_hits: int, candidates_judged: int,
               input_tokens: int = 0, output_tokens: int = 0) -> None:
    """Emit a structured stats line on stdout that integrators
    (e.g. MHM Pipeline's GUI) can parse to update their live
    progress cards. Format kept simple so a regex parser can split
    it without ambiguity."""
    print(
        f"[STATS] total={candidates_total} hits={cache_hits} "
        f"judged={candidates_judged} in_tok={input_tokens} out_tok={output_tokens}",
        flush=True,
    )


__all__ = [
    "header", "section", "kv", "info", "bullet",
    "ok", "warn", "error",
    "progress_line", "done_line", "summary_table",
    "emit_stats",
]

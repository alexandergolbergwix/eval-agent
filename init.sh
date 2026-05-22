#!/usr/bin/env bash
# eval-agent — one-shot bootstrap script (Anthropic "Initializer" agent role).
#
# Idempotent: safe to re-run. Performs:
#   1. Locates `uv` or falls back to `python3 -m venv`.
#   2. Creates `.venv` (Python 3.12) and installs the package + dev deps.
#   3. Scaffolds `state/feature_list.json` from `config/rubrics/` (one feature per
#      rubric's declared `sub_types`). Initial status: passes=false.
#   4. Writes `state/progress.md` header if missing.
#   5. Runs the baseline test suite to confirm a clean install.
#   6. Initialises git (if not already) and stages the first commit.
#
# Re-running: respects existing state — does not overwrite feature_list.json or
# progress.md. Just confirms tools, deps, and tests are healthy.

set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
echo "=== eval-agent bootstrap (root: $ROOT) ==="

# ──────────────────────────────────────────────────────────────────────────────
# 1. Locate uv (preferred) or python3
# ──────────────────────────────────────────────────────────────────────────────
UV=""
if command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
  UV="$HOME/.local/bin/uv"
fi

if [ -n "$UV" ]; then
  echo "[1/6] uv found at $UV"
  # `uv venv` errors if .venv exists; --allow-existing is the idempotent flag.
  $UV venv --python 3.12 --allow-existing .venv >/dev/null
  $UV pip install --python .venv/bin/python -e ".[dev]" --quiet
else
  echo "[1/6] uv not found — falling back to python3 -m venv"
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip --quiet
  .venv/bin/pip install -e ".[dev]" --quiet
fi

# ──────────────────────────────────────────────────────────────────────────────
# 2. Verify Python is 3.12+
# ──────────────────────────────────────────────────────────────────────────────
PYV="$(.venv/bin/python -c 'import sys; print(sys.version_info[0:2])')"
echo "[2/6] Python version in venv: $PYV"

# ──────────────────────────────────────────────────────────────────────────────
# 3. Seed state files (feature_list.json + progress.md) — IDEMPOTENT
# ──────────────────────────────────────────────────────────────────────────────
mkdir -p state/runs
if [ ! -f state/feature_list.json ]; then
  echo "[3/6] state/feature_list.json missing — bootstrapping from config/rubrics/"
  .venv/bin/python -m eval_agent.orchestration.feature_list bootstrap
else
  echo "[3/6] state/feature_list.json already present — leaving in place"
fi

if [ ! -f state/progress.md ]; then
  echo "[3/6] writing state/progress.md header"
  cat > state/progress.md <<'EOF'
# eval-agent — session log

This file is an append-only narrative log of every Worker session.
New sessions append a dated section at the bottom. Past sections are
never edited or deleted — they are the audit trail.

Read the tail of this file at the start of every session to recover
context that does not fit in the model's context window.
EOF
else
  echo "[3/6] state/progress.md already present — leaving in place"
fi

# ──────────────────────────────────────────────────────────────────────────────
# 4. Run the baseline test suite
# ──────────────────────────────────────────────────────────────────────────────
echo "[4/6] running pytest baseline …"
PYTHONPATH=. .venv/bin/pytest tests/ -q

# ──────────────────────────────────────────────────────────────────────────────
# 5. Git init (idempotent)
# ──────────────────────────────────────────────────────────────────────────────
if [ ! -d .git ]; then
  echo "[5/6] git init"
  git init --quiet --initial-branch=main
  git add -A
  git commit --quiet -m "chore: bootstrap eval-agent v0.1"
else
  echo "[5/6] .git already present — leaving in place"
fi

# ──────────────────────────────────────────────────────────────────────────────
# 6. Doctor — final health check
# ──────────────────────────────────────────────────────────────────────────────
echo "[6/6] doctor"
.venv/bin/python -m eval_agent.cli doctor

echo
echo "=== bootstrap complete ==="
echo "Next: GEMINI_API_KEY=… make run PIPELINE_OUTPUT=/path/to/pipeline/eval/work"

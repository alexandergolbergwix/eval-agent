# eval-agent

A long-running, Gemini-powered evaluation agent for the **MHM Pipeline**
(`/Users/alexandergo/Documents/Doctorat/pipeline`). Reads pipeline JSON
outputs from disk, asks Gemini 3.x to compare each prediction against
the full MARC record, and emits reproducible per-model precision
reports plus reviewer-grade audit artefacts.

Built following Anthropic's [effective harnesses for long-running
agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
— Initializer + Worker split, structured file state, git checkpointing,
session-startup procedure, self-verification.

---

## Why a separate project

The eval logic started life as a 838-line script in the pipeline repo
(`pipeline/scripts/evaluate_models_with_gemini.py`). It worked, but it
was:

- a throw-away script, not infrastructure
- coupled to the pipeline repo
- not resumable beyond the SHA-256 verdict cache
- single-judge (Gemini) hardcoded into every layer
- no upgrade path to evaluate Stages 3–6

`eval-agent` extracts that logic into a standalone, file-coupled tool
with proper module boundaries, a pluggable `Evaluator` interface, and
session memory.

**Loose coupling:** the agent **reads pipeline JSON from disk** and
never imports pipeline modules. Pipeline can ship; eval-agent stays
independent. If the pipeline schema changes, only the ingest layer
needs an update.

---

## Quick start

```bash
# 1. Bootstrap (one-shot, idempotent)
cd /Users/alexandergo/Documents/Doctorat/eval-agent
bash init.sh

# 2. Doctor — confirm API key, schemas, cache, fixtures
make doctor

# 3. Run against a pipeline output folder
export GEMINI_API_KEY="…"
make run PIPELINE_OUTPUT=/Users/alexandergo/Documents/Doctorat/pipeline/eval/work

# 4. Read the latest report
ls -t state/runs/ | head -1 | xargs -I{} cat state/runs/{}/report.md
```

---

## What it evaluates (MVP)

All 5 trained models from MHM Pipeline Stage 2:

| Evaluator | Source field | Sub-types |
|---|---|---|
| `person_ner` | `entities[source=person_ner]` | AUTHOR, TRANSCRIBER, TRANSLATOR, COMMENTATOR, OWNER, EDITOR, CENSOR |
| `provenance_ner` | `entities[source=provenance_ner]` | OWNER, DATE, COLLECTION |
| `contents_ner` | `entities[source=contents_ner]` | WORK, FOLIO, WORK_AUTHOR |
| `genre_classifier` | `ml_genres[]` | 8 Hebrew-MS genre classes + NOTA |
| `marc500_colophon` | `ml_colophon_sentences[]` | COLOPHON (binary) |

Roadmap (post-MVP): Stage 3 authority resolution, Stage 4 RDF mapping,
Stage 5 SHACL violation triage, Stage 6 Wikidata upload diff. Adding
each is **one new evaluator module** under `eval_agent/evaluators/`.

---

## What gets produced per run

Every `make run` writes a self-contained run folder under
`state/runs/<YYYYMMDDThhmmssZ>/`:

```
state/runs/20260516T143000Z/
├── manifest.json          # pipeline input paths + agent config + session id + cache hits/misses
├── results.jsonl          # one line per judged candidate (~150–200 per run)
├── summary.csv            # per-(evaluator, sub_type) precision metrics
├── report.md              # human-readable summary + sample failures
└── self_verify.json       # 5% re-judge consistency check
```

The verdict schema lives at `config/schemas/verdict.v1.json`. Every
`results.jsonl` entry carries its `schema_version` so older runs can
be diffed against newer ones cleanly.

---

## Architecture

See `CLAUDE.md` for the full operating manual + design rationale.
Headlines:

- **`eval_agent/client/`** — Gemini REST client, sliding-window
  rate-limiter, abstract `Judge` interface
- **`eval_agent/cache/`** — SHA-256-keyed JSONL verdict cache (every
  re-run is incremental)
- **`eval_agent/ingest/`** — readers for pipeline JSON outputs; no
  pipeline imports
- **`eval_agent/evaluators/`** — one module per (stage, model);
  declares its MARC slice, prompt rubric, and verdict parser
- **`eval_agent/orchestration/`** — session lifecycle, feature list,
  progress log, self-verification
- **`eval_agent/report/`** — JSONL/CSV/Markdown writers + cross-run
  diff
- **`config/rubrics/`** — per-evaluator prompt templates (Markdown)
- **`config/schemas/`** — versioned JSON Schemas
- **`state/`** — feature_list.json + progress.md + per-run artefacts

---

## CLI

```text
eval-agent
├── init        # bootstrap (idempotent)
├── verify      # session-startup pre-flight
├── run         # judge a pipeline output end-to-end
├── report      # regenerate report.md from results.jsonl
├── diff        # compare two runs for regression detection
├── recover     # safe-mode: rebuild state from cache + git
└── doctor      # health check
```

`make <subcommand>` proxies to the CLI for common workflows.

---

## Key invariants

(Always-on rules; the agent will refuse to start if any are violated.)

1. **Never write back into the pipeline repo.** This includes the
   pipeline's `data/`, `eval/`, `dist/`, etc.
2. **Never make Wikidata writes.** All Wikidata-touching evaluators
   are dry-run only — read-only judgement against pipeline output.
3. **`make verify` must pass** before any new evaluator run. Workers
   refuse to start new judgements if cache integrity is broken or
   schemas don't validate.
4. **Cache + commits are append-only.** Bad runs are recovered via
   `eval-agent recover` (replays from cache + git), not by deleting
   files.

---

## Cost & runtime budget

- Per-candidate: ~1.5 K input + ~200 output tokens with Gemini 3.1 Pro
  Preview ≈ **$0.0019 / candidate**.
- 68-record `test_subset.tsv` corpus → ~160 candidates → **~$0.30 / run**.
- Default rate-limit: **25 RPM** (safe on free-tier). Runtime ~7 min.
- Cache hits are free — incremental re-runs cost only the new
  candidates.

---

## Status

- **Phase 0 (Bootstrap):** complete — directory layout, init.sh,
  pyproject, Makefile, README, CLAUDE.md
- **Phase 1 (MVP):** in progress — porting evaluators from the
  pipeline script
- **Phase 2 (Harness hardening):** pending — feature_list,
  progress.md, self_verify, verify, recover

See `state/progress.md` for the current session log.

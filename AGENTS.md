# AGENTS.md — eval-agent (Codex / generic agent operating manual)

This file is read by Codex CLI and other AI coding agents at the start
of every session in this directory. It mirrors `CLAUDE.md` but uses
the `AGENTS.md` filename convention used by Codex and the broader
agent ecosystem.

For the full operating manual, **read [`CLAUDE.md`](CLAUDE.md)**. This
file is a deliberate short-form pointer to keep both agent
ecosystems in sync.

---

## TL;DR — what this project is

A standalone, long-running Gemini-based evaluation agent for the MHM
Pipeline (`/Users/alexandergo/Documents/Doctorat/pipeline`). Reads
pipeline JSON outputs from disk, asks Gemini 3.x to compare each
prediction against the full MARC record, and emits reproducible
per-model precision reports.

Built following Anthropic's [effective harnesses for long-running
agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents).

---

## Session startup — MANDATORY order

1. `git log --oneline -10`
2. Read the tail of `state/progress.md` (last 50 lines)
3. Read `state/feature_list.json`
4. Run `make verify` — REFUSE TO START WORK IF IT FAILS
5. Identify the next task from `feature_list.json`

---

## Hard rules (cannot violate)

1. Read pipeline output from disk only — no `from converter ...` imports
2. Never write to `../pipeline/`
3. No external mutations (Wikidata, GitHub, Hugging Face)
4. `state/progress.md` is append-only
5. `state/feature_list.json` entries are never deleted (only status updated)
6. `state/cache/verdict_cache.jsonl` is append-only
7. `make verify` must pass before any new run

---

## Common operations

```bash
bash init.sh                                # one-shot bootstrap
make verify                                  # pre-flight before any run
make run PIPELINE_OUTPUT=<path>              # evaluate a pipeline output
make report                                  # regenerate the latest report
eval-agent diff --from <ts1> --to <ts2>      # compare two runs
eval-agent recover                           # rebuild state from cache + git
```

---

## When in doubt

Read `CLAUDE.md`. It is the canonical operating manual. This file is
just the Codex-convention entry point.

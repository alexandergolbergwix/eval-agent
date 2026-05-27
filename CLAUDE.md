# eval-agent — operating manual

This file is the **agent-facing operating manual** for the eval-agent
project. It is read by Claude Code at the start of every session.

The MHM Pipeline lives at `/Users/alexandergo/Documents/Doctorat/pipeline`.
This eval-agent lives at `/Users/alexandergo/Documents/Doctorat/eval-agent`
and runs the MHM Pipeline's outputs through a Gemini-based evaluation
harness following Anthropic's [effective harnesses for long-running
agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
recipe.

---

## Session-startup procedure (MANDATORY)

At the start of every session, **in this exact order**:

1. **Read recent commits** — `git log --oneline -10` to see what
   prior sessions did. The git log is the canonical "what changed."
2. **Read the tail of `state/progress.md`** — last 50 lines minimum.
   Contains free-form session-by-session narrative the model wrote
   itself last time. Treat it as primed context.
3. **Read `state/feature_list.json`** — the canonical task ledger.
   Which evaluators currently `passes: true`? Which need attention?
4. **Run `make verify`** — confirms cache integrity, schema validity,
   fixture round-trip. **REFUSE TO START NEW WORK IF VERIFY FAILS.**
   If `make verify` fails, the next action is always
   `eval-agent recover`, NOT pushing through with broken state.
5. **Identify the next task** from feature_list.json. Only one task
   should be `in_progress` at a time.

Do not skip any step. Anthropic's research shows agents that skip
session-startup procedures regress prior work within 2–3 sessions.

---

## Architectural invariants (CANNOT VIOLATE)

These are checked by `make verify` and by the worker session lifecycle.
If any check fails, the worker refuses to proceed.

1. **Loose coupling with the pipeline repo.** The eval-agent reads
   `marc_extracted.json` / `ner_results.json` / etc. **from disk**.
   It MUST NOT `from converter import …` or `from mhm_pipeline import …`.
   Pipeline output is treated like any external data source.
2. **Never write to the pipeline repo.** The pipeline is a sibling
   project. The eval-agent's only effect on the pipeline is to read
   its outputs.
3. **Never make external mutations.** No Wikidata writes, no GitHub
   API writes, no Hugging Face uploads. This agent is read-only on
   every external system except the local file system inside
   `/Users/alexandergo/Documents/Doctorat/eval-agent/`.
4. **No verdict cache deletes from code.** Workers may APPEND to
   `state/cache/verdict_cache.jsonl` but never rewrite or delete.
   The cache is append-only by design — duplicate keys are tolerated
   (last-write wins in reader); manual `rm` is the only way to clear.
5. **No `state/progress.md` rewrites.** Append-only. If you discover
   a prior entry is wrong, write a CORRECTION entry below it.
6. **No `state/feature_list.json` deletes.** Workers may flip a
   feature's `status.passes` between true/false and update
   `last_run`/`last_precision`, but they MUST NOT remove a feature
   entry. New evaluators get appended; old evaluators stay even after
   the underlying model is deprecated.
7. **Self-verification is mandatory.** Every run must execute the
   5% re-judge consistency check at the end. If agreement falls
   below 0.95, the run is flagged in `feature_list.json` (`notes:
   "self-verify regression"`) and the worker stops new work.

---

## Two-agent split (Anthropic harness pattern)

### Initializer (`init.sh`)

Runs once. Idempotent. Creates the venv, installs deps, scaffolds
state files, runs baseline tests, makes the first git commit. Safe
to re-run if you want to confirm tools are still healthy — it never
overwrites existing state files.

### Worker (`eval-agent run`)

Runs every subsequent session. Reads state files, picks the next
unfinished task, executes one evaluator (or all, with `--evaluators
all`), checkpoints state after each evaluator, commits.

A Worker session ALWAYS:

- Starts by running `make verify` (refuses to start otherwise)
- Updates `state/progress.md` at the end of every evaluator
- Commits to git after every meaningful state change
- Runs `self_verify` before declaring success
- Never trusts what it can re-derive from the cache + commits

---

## Files the agent maintains

```
state/
├── feature_list.json   — canonical task ledger (UPDATE status only)
├── progress.md         — narrative session log (APPEND only)
└── runs/
    └── <ts>/
        ├── manifest.json
        ├── results.jsonl
        ├── summary.csv
        ├── report.md
        └── self_verify.json
```

```
state/cache/
└── verdict_cache.jsonl  — SHA-256-keyed verdicts (APPEND only)
```

---

## Pluggable evaluator interface

Every model evaluation lives in `eval_agent/evaluators/<name>.py` and
implements:

```python
class Evaluator:
    id: str                         # canonical name, e.g. "person_ner"
    sub_types: list[str]            # categories to break out in metrics
    marc_field_keys: list[str]      # semantic MARC slice this evaluator needs
    rubric_path: str                # config/rubrics/<id>.md
    confidence_field: str = "confidence"  # "confidence" or "model_confidence"

    def extract_candidates(self, ner_record, marc_record, threshold) -> Iterable[Candidate]: ...
    def build_prompt(self, candidate: Candidate) -> str: ...
    def parse_verdict(self, raw: dict, candidate: Candidate) -> Verdict: ...
    def verify_self(self, sample, judge) -> SelfVerifyResult: ...
```

Adding a new evaluator (e.g. for Stage 3 authority resolution):

1. New module under `eval_agent/evaluators/authority.py`.
2. New rubric Markdown at `config/rubrics/authority.md`.
3. New ingest reader at `eval_agent/ingest/authority.py`.
4. Register evaluator in `eval_agent/evaluators/__init__.py`.
5. Append new entries to `state/feature_list.json`.
6. Add fixtures under `tests/fixtures/`.
7. `make verify && make run`.

No core code is touched — the harness orchestrates whichever
evaluators are registered.

---

## Tool registry (the Worker's tools)

`eval_agent/tools/tool_registry.py` exposes named, schema-described
operations the Worker can sequence:

- `cache_lookup(key) -> Verdict | None`
- `re_judge(candidate, alternative_judge_id) -> Verdict`
- `diff_runs(from_ts, to_ts) -> DiffReport`
- `emit_report(run_id) -> Path`
- `fetch_marc_extract(pipeline_output_dir) -> list[dict]`
- `verify_self(sample) -> SelfVerifyResult`

Workers prefer tool-registry calls over ad-hoc code so behaviour is
introspectable + testable.

---

## Agentic judging (the tool-loop)

The judge used to be a single-shot LLM-as-judge workflow: one Gemini
call per candidate against a fixed `responseSchema`. It is now
**agentic by default** — a ReAct tool-loop where the model directs its
own evidence-gathering before committing a verdict.

**Modes** (`--linear` / `--agentic-all` flags; default = gated):

- `gated` (default) — tier-1 single-shot on every candidate; the
  tool-loop fires only when tier-1 returns `abstain` / `partial`.
  Keeps cost + determinism for the easy majority.
- `agentic_all` — every candidate runs the loop.
- `linear` — the original single-shot path. Reproducible / citable;
  `--linear` must reproduce pre-agentic numbers (guarded by
  `tests/test_agentic_parity.py`).

**The loop** (`eval_agent/agentic/loop.py`, `AgenticJudge.run`): builds
the agent system prompt (`config/rubrics/agentic_system.md`) + the
evaluator's per-candidate prompt, then calls
`GeminiJudge.generate_with_tools` (function-calling) in a budgeted loop
(`max_steps`, default 6). The model chooses which tools to call — it is
never forced. On an uncertain verdict it escalates the model once
(tier_model `gemini-3.5-flash` → escalate_model `gemini-3.1-pro-preview`,
both config-driven). Every step is recorded in a `Trace` written to
`<run_dir>/traces/<evaluator_id>.jsonl`.

**Agent tools** (`eval_agent/agentic/tools.py`, model-chosen):

- `fetch_marc_field(field)` — read any field from the full MARC record
  on disk (the per-evaluator projection drops most fields).
- `expand_note()` — full untruncated notes + colophon.
- `list_record_entities()` — all NER predictions on the record (joint
  reasoning).
- `lookup_authority(name, kind)` — VIAF + Wikidata existence check
  (`eval_agent/client/authority_client.py`; the only networked tool;
  never raises; honours `EVAL_AGENT_NO_NETWORK`).

**Invariants preserved:** the Rule-48 trust boundary is intact — tools
read pipeline JSON on disk or make the eval-agent's OWN network calls;
zero Python imports across to the pipeline. The verdict cache key is
mode-tagged (`<model>::<mode>`) so agentic + linear verdicts never
collide. `self_verify`'s agreement gate samples LINEAR verdicts only
(agentic verdicts re-gather evidence on re-run and legitimately
diverge); their count is reported in `self_verify.json` as
`agentic_excluded`, non-gating.

**Model-id caveat:** `gemini-3.5-flash` / `gemini-3.1-pro-preview` must
resolve on the live API; they live in `config/default.yaml`. Fall back
to `gemini-2.5-flash` / `gemini-2.5-pro` if rejected. The
function-response turn role in `loop.py:_function_response_turn` is set
to `"user"` per v1beta — verify in the live smoke if tool turns error.

---

## What to do when things go wrong

| Symptom | Recover-mode action |
|---|---|
| `make verify` fails on cache | `eval-agent recover --cache` — rebuilds cache from `state/runs/*/results.jsonl` |
| `make verify` fails on schemas | Check `config/schemas/verdict.vN.json` against `state/runs/latest/results.jsonl`; bump schema version if intentional |
| Gemini 429s past max-retries | Lower `--rpm` in `config/default.yaml`; re-run; cache reuse means no work lost |
| Mid-run crash (process killed) | `eval-agent run --resume` — picks up from cache + manifest checkpoint |
| Hallucinated verdict (Gemini drift) | `eval-agent re-judge <verdict_id>` with `--judge alternative` |
| Lost state file | `git reflog` then `git checkout <last-good>` — state is committed every run |

---

## What this agent does NOT do

- Train models (the pipeline owns model training).
- Modify pipeline source code.
- Push changes to GitHub / Hugging Face / Wikidata.
- Run the pipeline itself (the user runs the pipeline; this agent
  judges the outputs).
- Make decisions about whether a model should ship to production —
  it produces evidence (precision metrics + sample failures) for a
  human to decide on.

---

## Reading list (re-read at session start when stuck)

- `state/progress.md` (the last 100 lines)
- `state/feature_list.json` (full)
- `state/runs/$(ls -t state/runs | head -1)/report.md` (most recent run)
- Anthropic's [effective harnesses](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- The plan file at `/Users/alexandergo/.claude/plans/majestic-percolating-fern.md`

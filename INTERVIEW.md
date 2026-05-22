# Project: `eval-agent` — interview talk-track

This document is for the **Senior AI Research Engineer interview at Tenzai**. It maps the eval-agent project onto the four pillars of the Tenzai role and gives talking points + concrete artefacts to demo. Source code lives at `/Users/alexandergo/Documents/Doctorat/eval-agent`.

---

## One-line elevator pitch

A **long-running, multi-stage evaluation agent** that judges every output of a Hebrew-manuscript MARC-to-Wikidata pipeline against the source record using an LLM-as-judge harness. Built following Anthropic's effective-harnesses-for-long-running-agents recipe — Initializer + Worker split, structured file-based memory, git-backed checkpointing, session-startup procedure, self-verification, pluggable evaluators.

**~838-line script → standalone project with proper module boundaries, schema versioning, append-only memory, and a tool registry.**

---

## Mapping to the Tenzai role's four responsibilities

### 1. Build Tenzai's Agent Harness — knowledge management, context engineering, agentic tooling

**What I built:**

- **Two-agent architecture (Anthropic pattern)**: `init.sh` (Initializer, idempotent bootstrap) + `eval-agent run` (Worker, per-session execution with mandatory startup procedure: read git log → tail progress.md → load feature_list.json → `make verify` → only then execute new work).
- **Tool registry** (`eval_agent/tools/`): named, schema-described callables (`cache_lookup`, `re_judge`, `diff_runs`, `emit_report`, `fetch_marc_extract`, `verify_self`). Workers prefer registry calls over ad-hoc code → introspectable + testable.
- **Pluggable evaluator interface** (`eval_agent/evaluators/_base.py`): each model evaluator declares its own MARC slice, prompt rubric, and verdict parser. Adding Stage 3 evaluation is one module — no core code touched.
- **Context engineering**: per-evaluator MARC field selection (different evaluators see different slices of the source record); explicit prompt-budget management; semantic-key projection from a 170-key raw record down to the 5–7 keys each evaluator needs.

**Key file to show**: `eval_agent/orchestration/session.py` (Worker lifecycle), `eval_agent/evaluators/_base.py` (plug-in interface), `config/rubrics/*.md` (markdown prompt templates, version-controlled).

### 2. Develop Data Systems for AI Memory — storage, retrieval, reasoning

**What I built — three-layer memory hierarchy:**

| Memory layer | Storage | Purpose | Read frequency |
|---|---|---|---|
| **Working memory** | per-Worker run, in-process | Current candidate + judge response | Constantly |
| **Episodic memory** | `state/runs/<ts>/results.jsonl` | Every past verdict, per run | Diff across runs |
| **Semantic memory** | `state/cache/verdict_cache.jsonl` (SHA-256 keyed) | Idempotent verdict cache — re-runs reuse | Every candidate lookup |
| **Procedural memory** | `state/feature_list.json` + `state/progress.md` | What evaluators exist, pass/fail status, session-by-session log | Session startup |

- **Schema versioning**: every verdict carries `schema_version`; old runs remain readable across schema upgrades; migration is explicit (`config/schemas/verdict.vN.json` + grace-window logic in the cache reader).
- **Append-only invariant**: caches and logs are never rewritten by code — corruption is impossible, recovery is `git checkout` + cache replay.
- **Content-addressed dedup**: cache key = SHA-256(judge_id + prompt). Same (judge, prompt) always returns the same verdict. Different judge → different cache entry — no cross-contamination.
- **Cross-run reasoning** (`eval-agent diff`): compare two runs on the same input; flag any feature whose precision regressed. This is the "data system for AI memory" reasoning layer.

**Key file**: `eval_agent/cache/verdict_cache.py`, `config/schemas/verdict.v1.json`, `eval_agent/report/diff_runs.py`.

### 3. Evaluation & Experimentation Systems

**What I built:**

- **End-to-end eval pipeline**: pipeline output JSON → candidate extraction (per evaluator) → prompt building → parallel Gemini calls (rate-limited) → structured-output parsing → JSONL/CSV/Markdown report.
- **Production-grade LLM client**: sliding-window thread-safe rate limiter (hard cap on RPM, blocks until budget refills); exponential-backoff retry on 429; structured-output enforcement via `responseSchema` (the model physically cannot emit non-conforming JSON); explicit thinking-token control (`thinkingLevel: "low"` for Gemini 3.x).
- **Cost + runtime instrumentation**: every run's `manifest.json` records token counts, cache hit/miss ratio, RPM budget, total cost estimate, runtime per stage.
- **Self-verification**: after each run the Worker re-judges a 5% random sample with a fresh cache key; if agreement < 95%, the run is flagged. This catches judge drift across model versions or prompt iterations.
- **Multi-judge support** (architecture): `Judge` interface is pluggable. Gemini today, Claude/GPT tomorrow. Multi-judge consensus (run two, surface disagreements) is one config flag away.
- **Experimentation primitives**: re-run with a different `--judge` or `--threshold` or alternate rubric; cross-run `diff` shows where the change moved precision. The unit of experiment is "one git commit on the eval-agent + one re-run on the same pipeline output."

**Real numbers from the project**: evaluated all 5 trained models in the MHM Pipeline on a 68-record corpus. Discovered that the MARC500 colophon classifier has 14% strict precision (vs 91% for Provenance NER), pinpointing a retraining target. ~$0.30 per full run, ~7 min, zero 429s after rate-limiter rollout.

**Key file**: `eval_agent/client/gemini_client.py`, `eval_agent/client/rate_limiter.py`, `eval_agent/orchestration/self_verify.py`, `eval_agent/report/diff_runs.py`.

### 4. Working with Researchers — measurable improvements

**What I built:**

- The eval-agent is the **measurement instrument** for the parent research project (MHM Pipeline, a PhD dissertation in digital humanities). Every model improvement the pipeline ships is measured here.
- **Reproducibility primitives**: SHA-256 corpus pinning, schema versioning, idempotent verdict cache → any historical result can be replayed bit-for-bit, including with a different judge for triangulation.
- **Reviewer-grade reports**: per-run Markdown report has headline numbers + sample failures with Gemini's reasoning quoted. Hands directly to a supervisor / paper reviewer — no further preprocessing.
- **Regression detection**: `diff_runs` surfaces any precision regression introduced by a model retrain or rubric change.
- **Feature ledger** (`feature_list.json`): canonical task list. Researchers can scan it to see which models are above the precision floor (`passes: true`) and which need attention. Append-only — historical context is preserved.

---

## Why this maps directly to Tenzai's stack

Tenzai builds autonomous offensive-security AI agents — agents that run for long sessions, exercise tools (scanners, exploit frameworks), accumulate knowledge across runs, and must be continuously evaluated to confirm they're getting *better*, not just *different*. Concretely:

| Tenzai need | What this project demonstrates |
|---|---|
| Long-running agents that don't lose context | Initializer / Worker split + session-startup procedure + append-only memory |
| Knowledge management across sessions | feature_list.json + progress.md + verdict cache (semantic memory) |
| Context engineering | Per-evaluator MARC field projection; prompt-budget enforcement; structured-output schema |
| Agentic tooling | Tool registry with schema-described callables |
| Data systems for AI memory | 4-layer hierarchy (working / episodic / semantic / procedural) with explicit storage + retrieval |
| Evaluation pipelines | The entire project IS one |
| Experimentation systems | Pluggable Judge interface; cross-run diff; schema-versioned verdicts |
| Measurable improvements with researchers | Reviewer-grade reports + reproducibility primitives + regression detection |

---

## Engineering details to surface in the interview

These are the bits that tend to land well with a strong interviewer:

### Production LLM client design

- **Rate limiter is sliding-window, threading-safe**, with a hard cap. Workers block on `acquire()` rather than retry-on-429, so 429s become impossible in steady state. Retry is for transient network issues, not for rate limits.
- **Structured outputs are SCHEMA-bound**, not prompt-bound: Gemini physically cannot emit malformed JSON or out-of-enum values. The local parser is therefore trivial — no markdown-fence stripping, no salvage logic.
- **Cache key includes judge id**: switching from `gemini-3.1-pro-preview` to `claude-opus-4-7` invalidates the cache cleanly. No silent cross-judge contamination.

### Anthropic harness fidelity

- **`make verify` is the session gatekeeper**. Cache integrity check + schema validation + fixture round-trip + pytest. The Worker refuses to start new work if it fails — catches drift early.
- **Self-verification is mandatory**. Without it, an LLM judge silently drifts as the underlying model updates. The 5% re-judge + agreement floor catches this within one session.
- **No state file is ever rewritten by code.** Append-only `progress.md`, append-only verdict cache, never-deleted feature_list entries (status flips between true/false, but the feature itself stays). Recovery is git revert, not delete-and-restart.

### Subtle bugs caught + fixed during development

(These are good "war story" moments to share in an interview.)

1. **Empty MARC context bug**: original script passed raw MARC subfields (`100$a`, `561$a`) to Gemini — but the pipeline's `marc_extracted.json` doesn't have those keys; it has semantic projections (`authors`, `provenance`, `notes`). Result: every Gemini call got an empty MARC context, and the judge correctly answered "I can't verify because there's nothing here." Took one careful inspection of the JSON to discover; took one rewrite of the candidate-builder to fix. Lesson: read your own ground-truth data before scaling.
2. **Gemini 2.5 → 3.x API shape drift**: 3.x uses `thinkingLevel: "low"` not `thinkingBudget: 0`; 3.x advertises `responseFormat.text.schema` but v1beta REST still uses the 2.x flat `responseMimeType + responseSchema`. Required reading the Gemini changelog + parsing 400-error bodies to discover. Lesson: an LLM API is a moving target; pin the shape with a working smoke test.
3. **Rate limit triage**: 429 retry-after-backoff isn't enough on free-tier Pro Preview models. The fix is a hard rate-limiter, not bigger retries. Lesson: prevent the failure mode in the first place, don't paper over it.

---

## Repo tour for a live demo

If the interview has a "share your screen" moment:

```bash
cd /Users/alexandergo/Documents/Doctorat/eval-agent

# 1. The two-agent split
bat init.sh CLAUDE.md           # show the bootstrap + operating manual

# 2. The memory layers
ls state/                       # feature_list.json, progress.md, runs/
cat state/feature_list.json     # the canonical task ledger

# 3. The pluggable evaluator interface
bat eval_agent/evaluators/_base.py
ls eval_agent/evaluators/       # one module per (stage, model)

# 4. The Gemini client + rate limiter
bat eval_agent/client/gemini_client.py
bat eval_agent/client/rate_limiter.py

# 5. The schema-versioned verdict
bat config/schemas/verdict.v1.json
bat config/schemas/README.md

# 6. The tool registry
bat eval_agent/tools/tool_registry.py

# 7. A real run report (after a `make run`)
cat state/runs/$(ls -t state/runs | head -1)/report.md
```

---

## Talking points / questions to anticipate

**Q: How does this differ from a regular eval script with retries?**

It's an *agent*, not a script. The Worker reads its own memory at session start, decides what to do next from a canonical task ledger, executes tools, checkpoints state, self-verifies, and writes a narrative log for the next session. A script doesn't survive a crash; this agent does. A script doesn't know what it did yesterday; this agent does.

**Q: What's the riskiest design choice you made?**

Treating the eval cache as the *primary* memory layer. Every verdict is content-addressed; cache hits are free; cache invalidation is automatic when the judge changes. The risk was that I'd accumulate stale verdicts that don't match new prompts — but the SHA-256 prompt+judge key makes that physically impossible. The cache is correctness, not just speed.

**Q: How would you extend this to Tenzai's offensive-security agents?**

Same harness shape. Instead of one `Judge` interface, you'd have a `Tool` interface — scanners, exploit modules, recon utilities — each schema-described, each with append-only execution logs (this is the audit trail). Memory hierarchy becomes: working = current target context, episodic = past engagements, semantic = CVE / exploit-pattern knowledge base, procedural = playbook state (which tools have been tried, with what parameters, against this target). Worker session-startup procedure is the same: read recent commits + tail of progress file + load target state + verify environment before running new exploits. Self-verification is the same: did the exploit actually do what we claim it did? Re-run with a different signature to confirm.

**Q: What's the most important thing you learned?**

Long-running agents fail when they lose context. The fix isn't bigger context windows — it's an append-only file-based memory that the agent reads at session start. The model is stateless; the harness gives it the illusion of state. That's the whole game.

---

## Status as of interview-readiness

- Phase 0 (Bootstrap): ✅ complete
- Phase 1 (MVP Gemini eval): in progress — Gemini client + rate limiter + 5 evaluators
- Phase 2 (Harness hardening): pending — feature_list updates + self_verify
- Phase 3 (Cross-run diff): pending
- Phase 4–6 (Stage 3–6 evaluators + multi-judge): roadmap

If you're reading this for an interview, the **Phase 0+1 outputs are the demo material**; Phases 2+ are roadmap framing.

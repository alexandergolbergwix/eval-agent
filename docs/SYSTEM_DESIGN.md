# eval-agent — system design

Detailed Mermaid diagrams of the eval-agent architecture. Mirrors the
four pillars laid out in [`INTERVIEW.md`](../INTERVIEW.md): agent
harness, AI memory, evaluation pipeline, experimentation system.

Each diagram below is renderable in any Mermaid-aware viewer (GitHub,
Obsidian, VS Code with the Mermaid extension, [mermaid.live](https://mermaid.live/)).

---

## 1 · Two-agent split (Anthropic harness)

The initializer bootstraps the environment once. Every subsequent
session is a Worker run with a mandatory startup procedure.

```mermaid
flowchart LR
    user([User / CI / cron]) -->|first run| init[init.sh<br/>Initializer]
    user -->|subsequent runs| cli[eval-agent CLI<br/>Worker]

    subgraph one_time[" One-time bootstrap "]
        init --> uv[uv venv + uv pip install]
        init --> seed[Seed state files<br/>feature_list.json<br/>progress.md]
        init --> baseline[pytest baseline]
        init --> commit[git init + first commit]
    end

    subgraph per_session[" Per-session Worker lifecycle "]
        cli --> startup[1 · startup<br/>read git log<br/>tail progress.md<br/>load feature_list.json]
        startup --> verify{2 · verify<br/>cache + schemas<br/>+ fixtures}
        verify -->|pass| execute[3 · execute<br/>extract candidates<br/>judge in parallel<br/>cache verdicts]
        verify -->|fail| recover[recover<br/>rebuild from cache + git]
        recover --> startup
        execute --> checkpoint[4 · checkpoint<br/>write run dir]
        checkpoint --> selfverify[5 · self_verify<br/>5 percent re-judge]
        selfverify --> finalize[6 · finalize<br/>append progress.md<br/>git commit]
    end

    style init fill:#fff4cc,stroke:#cc9900,color:#000
    style cli fill:#cce5ff,stroke:#0066cc,color:#000
    style verify fill:#ffe0cc,stroke:#cc6600,color:#000
    style recover fill:#ffcccc,stroke:#cc0000,color:#000
```

---

## 2 · Memory hierarchy

Four explicit layers, each backed by a file. Working memory lives in
the running process; episodic / semantic / procedural live on disk and
survive crashes, restarts, and OS reboots.

```mermaid
flowchart TB
    subgraph working[" Working memory  (per-Worker, in-process) "]
        cand[Candidates]
        prompts[Prompts]
        verdicts_w[Verdicts in flight]
    end

    subgraph episodic[" Episodic memory  (per-run, on disk) "]
        manifest[manifest.json<br/>run config + stats]
        results[results.jsonl<br/>every verdict]
        summary[summary.csv<br/>per-evaluator metrics]
        report[report.md<br/>human-readable]
    end

    subgraph semantic[" Semantic memory  (cross-run, append-only) "]
        cache[(verdict_cache.jsonl<br/>SHA-256 keyed<br/>by judge_id + prompt)]
    end

    subgraph procedural[" Procedural memory  (canonical task ledger) "]
        feature[feature_list.json<br/>evaluator x sub_type<br/>passes / attempts / notes]
        progress[progress.md<br/>append-only<br/>session log]
    end

    working -- on success --> cache
    working -- per session --> episodic
    episodic -- recompute metrics --> feature
    episodic -- narrate --> progress

    cache -. read on every<br/>candidate .-> working
    progress -. tail on<br/>session start .-> working
    feature -. select next task .-> working

    style working fill:#e6f3ff,stroke:#0066cc,color:#000
    style episodic fill:#fff4e6,stroke:#cc6600,color:#000
    style semantic fill:#e6ffe6,stroke:#009900,color:#000
    style procedural fill:#f4e6ff,stroke:#7700cc,color:#000
```

**Invariants:**

- `progress.md` is append-only. Corrections are new entries below, never edits.
- `feature_list.json` entries are never deleted — only `status` is mutated.
- `verdict_cache.jsonl` is append-only. Cache key = `SHA-256(judge_id ‖ prompt)`. Switching judges invalidates the cache cleanly.
- Episodic memory under `state/runs/<ts>/` is read-only after the run finishes.

---

## 3 · End-to-end run dataflow

A single `make run` from candidate extraction to report emission.

```mermaid
flowchart TB
    pl[(Pipeline output<br/>eval/work/<br/>marc_extracted.json<br/>ner_results.json)]:::ext

    pl --> ingest[ingest/<br/>marc_extract.py<br/>ner_results.py<br/>pipeline_run.py]

    subgraph eval_loop[" For each registered Evaluator "]
        ingest --> extract[extract_candidates<br/>filter by threshold<br/>project MARC slice]
        extract --> prompt[build_prompt<br/>rubric + MARC + prediction]
        prompt --> cache_check{cache hit?}
        cache_check -->|yes| parsed
        cache_check -->|no| tier1[tier-1 judge<br/>fast model<br/>single shot]
        tier1 --> gate{abstain /<br/>partial?}
        gate -->|no| structured[(Gemini<br/>verdict)]:::ext
        gate -->|yes| loop[agentic tool-loop<br/>see section 10]
        loop --> structured
        structured --> cache_write[cache.append<br/>verdict_cache.jsonl]
        cache_write --> parsed[parse_verdict<br/>typed Verdict]
        parsed --> agg[accumulate<br/>per evaluator]
    end

    agg --> writers[report writers<br/>jsonl + csv + markdown]
    writers --> run_dir[(state/runs/&lt;ts&gt;/<br/>manifest.json<br/>results.jsonl<br/>summary.csv<br/>report.md)]:::int
    writers --> sv[self_verify<br/>5 percent re-judge]
    sv --> fl[update feature_list.json]
    fl --> commit[git commit]

    classDef ext fill:#f5f5dc,stroke:#8b7700,color:#000
    classDef int fill:#cce5ff,stroke:#0066cc,color:#000
```

---

## 4 · Module boundaries

Strict layering: CLI ─→ Orchestration ─→ Evaluators + Client + Cache.
Ingest is one-way (reads pipeline files); evaluators never call ingest
directly from outside this graph.

```mermaid
flowchart LR
    subgraph cli_layer[" CLI "]
        cli[eval_agent.cli]
    end

    subgraph orch_layer[" Orchestration "]
        session[orchestration/<br/>session.py]
        featlist[orchestration/<br/>feature_list.py]
        prog[orchestration/<br/>progress.py]
        sv[orchestration/<br/>self_verify.py]
    end

    subgraph eval_layer[" Evaluators (pluggable) "]
        base[_base.py<br/>Evaluator + Verdict + Candidate]
        e1[person_ner.py]
        e2[provenance_ner.py]
        e3[contents_ner.py]
        e4[genre_classifier.py]
        e5[marc500_colophon.py]
        reg[__init__.py<br/>REGISTRY]
    end

    subgraph client_layer[" Client (judges, rate-limit) "]
        judge_iface[judge_interface.py<br/>Judge protocol]
        gemini[gemini_client.py<br/>GeminiJudge]
        ratel[rate_limiter.py<br/>RateLimiter]
    end

    subgraph data_layer[" Data (cache, ingest, reports) "]
        cache[cache/<br/>verdict_cache.py]
        ingest[ingest/<br/>marc_extract.py<br/>ner_results.py<br/>pipeline_run.py]
        report[report/<br/>jsonl + csv + markdown]
    end

    subgraph cfg_layer[" Config (versioned, on disk) "]
        rubrics[config/rubrics/*.md]
        schemas[config/schemas/<br/>verdict.v1.json]
        defaults[config/default.yaml]
    end

    cli --> session
    session --> featlist
    session --> sv
    session --> reg
    reg --> e1 & e2 & e3 & e4 & e5
    e1 & e2 & e3 & e4 & e5 --> base
    base --> ingest
    base --> rubrics
    session --> judge_iface
    judge_iface -.- gemini
    gemini --> ratel
    gemini --> schemas
    session --> cache
    session --> report
    session --> defaults

    style cli_layer fill:#cce5ff
    style orch_layer fill:#f4e6ff
    style eval_layer fill:#e6ffe6
    style client_layer fill:#fff4e6
    style data_layer fill:#ffe6cc
    style cfg_layer fill:#f5f5dc
```

---

## 5 · Pluggable Judge interface

A new judge (Claude, GPT, local model) plugs in by implementing the
`Judge` protocol. The orchestration layer doesn't care which judge is
running — only the cache key does (so cross-judge contamination is
impossible).

```mermaid
classDiagram
    class Judge {
        <<Protocol>>
        +str id
        +judge(prompt, schema, timeout) JudgeResponse
    }

    class JudgeResponse {
        +verdict: dict | None
        +raw_text: str | None
        +error: str | None
        +judge_id: str
        +input_tokens: int | None
        +output_tokens: int | None
    }

    class GeminiJudge {
        -api_key: str
        -rate_limiter: RateLimiter
        -thinking_level: str
        -max_output_tokens: int
        -max_retries: int
        +judge(prompt, schema)
    }

    class ClaudeJudge {
        <<future>>
        -api_key: str
        -rate_limiter: RateLimiter
        +judge(prompt, schema)
    }

    class OpenAIJudge {
        <<future>>
        -api_key: str
        -rate_limiter: RateLimiter
        +judge(prompt, schema)
    }

    class RateLimiter {
        -max_rpm: int
        -window: deque
        -lock: threading.Lock
        +acquire()
    }

    Judge <|.. GeminiJudge
    Judge <|.. ClaudeJudge
    Judge <|.. OpenAIJudge
    GeminiJudge --> RateLimiter
    GeminiJudge --> JudgeResponse
```

---

## 6 · Pluggable Evaluator interface

Adding evaluation of a new pipeline stage (e.g. Stage 3 authority
resolution) is **one new module** + one rubric + one registry entry.
No core code touched.

```mermaid
classDiagram
    class Evaluator {
        <<ABC>>
        +str id
        +list[str] sub_types
        +list[str] marc_field_keys
        +str rubric_name
        +extract_candidates(ner, marc, threshold) Iterable[Candidate]*
        +build_prompt(candidate) str*
        +parse_verdict(raw, candidate) Verdict
        +rubric_text() str
        +render_prompt(candidate, prediction_block) str
    }

    class Candidate {
        +record_id: str
        +evaluator_id: str
        +sub_type: str
        +payload: dict
        +confidence: float
        +marc_context: dict
    }

    class Verdict {
        +record_id: str
        +evaluator_id: str
        +sub_type: str
        +name_ok: str
        +type_ok: str
        +role_ok: str
        +overall: str
        +reasoning: str
        +judge_id: str
        +cache_key: str
        +error: str | None
        +to_jsonl_record() dict
    }

    class PersonNERevaluator {
        +id = "person_ner"
        +sub_types = AUTHOR, SCRIBE, TRANSLATOR, ...
        +marc_field_keys = title, authors, contributors, provenance, ...
    }

    class ProvenanceNERevaluator {
        +id = "provenance_ner"
        +sub_types = OWNER, DATE, COLLECTION
    }

    class ContentsNERevaluator {
        +id = "contents_ner"
        +sub_types = WORK, FOLIO, WORK_AUTHOR
    }

    class GenreClassifierEvaluator {
        +id = "genre_classifier"
        +sub_types = 8 Hebrew-MS genres + NOTA
    }

    class Marc500ColophonEvaluator {
        +id = "marc500_colophon"
        +sub_types = COLOPHON
    }

    class AuthorityEvaluator {
        <<future>>
        +id = "authority"
        +sub_types = mazal, viaf, kima, wikidata
    }

    Evaluator <|-- PersonNERevaluator
    Evaluator <|-- ProvenanceNERevaluator
    Evaluator <|-- ContentsNERevaluator
    Evaluator <|-- GenreClassifierEvaluator
    Evaluator <|-- Marc500ColophonEvaluator
    Evaluator <|-- AuthorityEvaluator
    Evaluator ..> Candidate : produces
    Evaluator ..> Verdict : produces
```

---

## 7 · File-coupling boundary with the parent pipeline

The eval-agent is a sibling project to the MHM Pipeline. **Only files
cross the boundary** — no Python imports, no shared modules, no
shared dependencies (apart from stdlib + JSON).

```mermaid
flowchart TB
    subgraph pipeline_repo[" /Users/alexandergo/Documents/Doctorat/pipeline/ "]
        marcparse[Stage 1<br/>MarcParseWorker]
        ner[Stage 2<br/>NerWorker]
        authority[Stage 3+<br/>future]
        marcparse --> marcjson[(marc_extracted.json)]
        ner --> nerjson[(ner_results.json)]
    end

    subgraph eval_agent_repo[" /Users/alexandergo/Documents/Doctorat/eval-agent/ "]
        ingest[ingest/]
        session[Session]
        judge[Judge]
        runs[state/runs/&lt;ts&gt;/]
    end

    marcjson -. file read .-> ingest
    nerjson -. file read .-> ingest
    ingest --> session
    session --> judge
    session --> runs

    note["Hard rule:<br/>NO eval-agent module<br/>may 'from converter ...'<br/>or 'from mhm_pipeline ...'"]:::note

    classDef note fill:#fff4e6,stroke:#cc6600,color:#000

    style pipeline_repo fill:#e6f0ff
    style eval_agent_repo fill:#e6ffe6
```

The MHM Pipeline doesn't know the eval-agent exists. The eval-agent
reads only public JSON files. This means:

- Pipeline can refactor freely; eval-agent only breaks if the JSON
  schema changes (caught by ingest unit tests).
- Eval-agent can ship as its own project, open-source-able, with no
  pipeline dependency footprint.
- The Tenzai-style "harness as separate concern" pattern: the agent
  that *runs* the system is separate from the *system being run*.

---

## 8 · Session-startup procedure (Anthropic pattern, detailed)

What a Worker does before it's allowed to start new work.

```mermaid
sequenceDiagram
    autonumber
    participant U as User / cron
    participant W as Worker
    participant Git as git
    participant FS as Filesystem
    participant V as make verify
    participant FL as feature_list.json

    U->>W: eval-agent run --pipeline-output ...
    W->>Git: git log --oneline -10
    Git-->>W: recent commits
    W->>FS: tail state/progress.md (last 20 lines)
    FS-->>W: prior-session narrative
    W->>FL: load feature_list.json
    FL-->>W: canonical task ledger
    W->>V: run make verify
    alt verify passes
        V-->>W: cache, schema, fixtures OK
        W->>W: begin execute()
    else verify fails
        V-->>W: ERROR
        W-->>U: ABORT — run recover first
    end
```

This procedure is **mandatory**. Skipping it is what Anthropic's
research identifies as the most common cause of long-running agents
regressing prior work within 2-3 sessions.

---

## 9 · Verdict schema lifecycle

Schemas are versioned. Every verdict carries its `schema_version`;
old runs remain readable across schema upgrades; migrations are
explicit.

```mermaid
flowchart LR
    v1[verdict.v1.json<br/>5 fields:<br/>name_ok, type_ok,<br/>role_ok, overall,<br/>reasoning]
    v2[verdict.v2.json<br/>future: + judge_uncertainty<br/>+ alternative_label]
    v3[verdict.v3.json<br/>future: ...]

    v1 -. grace window<br/>both accepted .-> v2
    v2 -. grace window .-> v3

    cache[(verdict_cache.jsonl)]
    runs[(state/runs/&lt;ts&gt;/<br/>results.jsonl)]

    v1 --> cache
    v2 --> cache
    v1 --> runs
    v2 --> runs

    style v1 fill:#e6ffe6,stroke:#009900
    style v2 fill:#fff4cc,stroke:#cc9900
    style v3 fill:#f5f5f5,stroke:#999999
```

Migration runbook (see `config/schemas/README.md`):

1. Bump `schema_version` integer in a new `verdict.vN.json` file.
2. Verdict cache reader accepts both versions during a grace window.
3. Migration script back-fills v(N-1) entries to vN (where possible).
4. After grace window, dropping older schema is one commit.

---

## 10 · Agentic judging (the tool-loop)

The judge is **agentic by default** (gated). Tier-1 judges every
candidate in a single shot; only `abstain` / `partial` verdicts enter
the ReAct tool-loop, where the model chooses which evidence to gather
(`fetch_marc_field`, `expand_note`, `list_record_entities`,
`lookup_authority`) and escalates to a stronger model once if it stays
uncertain. `--linear` disables the loop (reproducible / citable path);
`--agentic-all` routes every candidate through it.

Each tool dispatch and escalation prints a `[STEP] tool <name>` /
`[STEP] escalate <model>` line; the MHM Pipeline's live agent-flow
diagram animates those nodes in real time.

```mermaid
flowchart TB
    cand[Candidate] --> tier1[Tier-1 judge<br/>fast model<br/>single shot]
    tier1 --> gate{overall ∈<br/>abstain / partial?}
    gate -->|no| verdict[(Verdict)]:::out

    subgraph loop[" ReAct tool-loop  (gated; budget = max_steps) "]
        direction TB
        model[Model turn<br/>generate_with_tools] --> choose{wants a tool?}
        choose -->|functionCall| tools[Tools<br/>fetch_marc_field · expand_note<br/>list_record_entities · lookup_authority]
        tools -->|observation| model
        choose -->|answer + still unsure| esc[Escalate once<br/>stronger model]
        esc --> model
        choose -->|answer confident| done[verdict]
    end

    gate -->|yes| model
    done --> verdict
    loop -. budget exhausted .-> forced[forced final<br/>no tools] --> verdict

    authority[(VIAF / Wikidata<br/>authority_client)]:::ext
    tools -. lookup_authority .-> authority
    marc[(marc_extracted.json<br/>full record on disk)]:::ext
    tools -. fetch / expand / list .-> marc

    classDef out fill:#e6ffe6,stroke:#009900,color:#000
    classDef ext fill:#f5f5dc,stroke:#8b7700,color:#000
    style loop fill:#f4e6ff,stroke:#7700cc,color:#000
```

**Invariants:**

- The tool-loop runs inside the eval-agent's own process; tools read
  the pipeline's on-disk JSON or make the eval-agent's OWN network
  calls (VIAF / Wikidata). The file-coupling boundary (section 7) is
  unchanged — no Python imports across.
- Every loop step is recorded to `state/runs/<ts>/traces/<evaluator>.jsonl`
  for audit (the agency is fully traceable).
- The verdict cache key is mode-tagged (`<model>::<mode>`) so agentic
  and linear verdicts never collide. `self_verify` gates on linear
  verdicts only — agentic verdicts re-gather evidence on re-run and
  legitimately diverge, so they are reported separately, non-gating.

---

## 11 · Reading order for a code-walkthrough

If you're reading the code for the first time (e.g. in an interview):

```mermaid
flowchart TB
    a[1. README.md<br/>headline + four pillars]
    b[2. INTERVIEW.md<br/>job-mapping]
    c[3. CLAUDE.md<br/>operating manual]
    d[4. config/default.yaml<br/>config surface]
    e[5. eval_agent/orchestration/session.py<br/>Worker lifecycle]
    f[6. eval_agent/evaluators/_base.py<br/>pluggable interface]
    g[7. eval_agent/client/<br/>rate_limiter + gemini_client + judge_interface]
    h[8. eval_agent/cache/verdict_cache.py<br/>SHA-256 keyed JSONL]
    i[9. config/rubrics/person_ner.md<br/>example rubric]
    j[10. state/runs/&lt;latest&gt;/report.md<br/>a real run]

    a --> b --> c --> d --> e --> f --> g --> h --> i --> j

    style a fill:#cce5ff
    style j fill:#fff4cc
```

"""Worker session lifecycle.

Implements the Anthropic long-running-agent harness pattern for one
run of the eval-agent against one pipeline output directory.

Lifecycle
---------

1. ``startup()`` — print git log, tail progress.md, load feature_list
   (informational only in Phase 1; Phase 2 will gate execution on
   ``make verify`` success).
2. ``execute(...)`` — for each evaluator: extract candidates, judge
   each (cache-aware), accumulate Verdicts.
3. ``checkpoint(...)`` — write run artefacts under
   ``state/runs/<ts>/``: manifest.json, results.jsonl, summary.csv,
   report.md.
4. ``finalize()`` — append a session block to progress.md.

The session is a class instead of a free function so the orchestrator
can introspect per-session state (cache stats, token counts, errors)
between phases.
"""

from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from eval_agent.cache.verdict_cache import VerdictCache
from eval_agent.client.gemini_client import GeminiJudge
from eval_agent.client.judge_interface import Judge
from eval_agent.client.rate_limiter import RateLimiter
from eval_agent.evaluators import REGISTRY, build as build_evaluator
from eval_agent.evaluators._base import Candidate, Evaluator, Verdict
from eval_agent.ingest import marc_extract, ner_results, pipeline_run
from eval_agent.logging_setup import get_logger
from eval_agent.report.csv_writer import write_csv
from eval_agent.report.jsonl_writer import write_jsonl
from eval_agent.report.markdown_report import write_markdown
from eval_agent import ui

log = get_logger("eval_agent.session")

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = REPO_ROOT / "state"
RUNS_DIR = STATE_DIR / "runs"
CACHE_PATH = STATE_DIR / "cache" / "verdict_cache.jsonl"
PROGRESS_PATH = STATE_DIR / "progress.md"
CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"
VERDICT_SCHEMA_PATH = REPO_ROOT / "config" / "schemas" / "verdict.v1.json"


# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class SessionConfig:
    pipeline_output: Path
    threshold: float
    rpm: int
    parallel: int
    judge_model: str
    evaluators: list[str]
    api_key: str
    dry_run: bool = False
    no_cache: bool = False  # skip cache reads (still appends new verdicts)

    @classmethod
    def from_args(cls, args: Any, defaults: dict[str, Any]) -> "SessionConfig":
        judge_cfg = defaults.get("judge", {})
        rl_cfg = defaults.get("rate_limit", {})
        thr_cfg = defaults.get("threshold", {})

        evals_arg = args.evaluators or "all"
        if evals_arg == "all":
            evaluators = list(REGISTRY)
        else:
            evaluators = [e.strip() for e in evals_arg.split(",") if e.strip()]
            for e in evaluators:
                if e not in REGISTRY:
                    raise ValueError(
                        f"unknown evaluator {e!r}; known: {sorted(REGISTRY)}"
                    )

        judge_model = args.judge or judge_cfg.get("id", "gemini-3.5-flash")

        # Pro-safety: free-tier RPM on Pro variants is roughly 10× tighter than
        # Flash. When the user picks a Pro model without overriding --rpm /
        # --parallel, fall back to Pro-safe defaults to avoid quota 429s.
        # Explicit user values always win — we only fill in unset slots.
        rpm = args.rpm
        parallel = args.parallel
        is_pro = _looks_like_pro_model(judge_model)
        if is_pro:
            pro_cfg = rl_cfg.get("pro", {})
            pro_rpm = int(pro_cfg.get("rpm", 10))
            pro_parallel = int(pro_cfg.get("parallel", 1))
            if rpm is None:
                rpm = pro_rpm
            if parallel is None:
                parallel = pro_parallel

        return cls(
            pipeline_output=Path(args.pipeline_output).expanduser().resolve(),
            threshold=float(args.threshold or thr_cfg.get("default", 0.85)),
            rpm=int(rpm if rpm is not None else rl_cfg.get("rpm", 25)),
            parallel=int(parallel if parallel is not None else rl_cfg.get("parallel", 2)),
            judge_model=judge_model,
            evaluators=evaluators,
            api_key=args.api_key or "",
            dry_run=bool(args.dry_run),
            no_cache=bool(getattr(args, "no_cache", False)),
        )


def _looks_like_pro_model(judge_id: str) -> bool:
    """True when the model id looks like a Gemini Pro variant.

    Pro-tier free quotas are ~10 RPM (vs ~150 RPM on Flash). We detect by
    substring match so future Pro variants (preview, GA, regional) all hit
    the safer defaults without code changes.
    """
    return "pro" in judge_id.lower()


# ──────────────────────────────────────────────────────────────────────────────
# Session
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class SessionStats:
    candidates_total: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    judged_full: int = 0
    judged_partial: int = 0
    judged_fail: int = 0
    judged_error: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str = ""


class Session:
    """One Worker session against one pipeline-output directory."""

    def __init__(
        self,
        config: SessionConfig,
        *,
        judge: Judge | None = None,
        cache_path: Path | None = None,
        runs_dir: Path | None = None,
        progress_path: Path | None = None,
    ) -> None:
        """Construct a Worker session.

        All file-system + judge dependencies are injectable to keep
        e2e tests fast + hermetic. In production callers pass nothing
        and the constructor falls back to the canonical paths.

        Parameters
        ----------
        judge
            If provided, used directly (skips ``_build_judge``). Tests
            inject a ``MockJudge`` here. Production leaves this None
            and the judge is built lazily at the start of ``execute``.
        cache_path, runs_dir, progress_path
            Override the on-disk locations. Tests point them at
            ``tmp_path`` so the real ``state/`` directory is never
            touched.
        """
        self.config = config
        self.stats = SessionStats()
        self._defaults = _load_defaults()

        self._run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self._runs_dir = runs_dir if runs_dir is not None else RUNS_DIR
        self._run_dir = self._runs_dir / self._run_id
        self._cache = VerdictCache(cache_path if cache_path is not None else CACHE_PATH)
        self._progress_path = progress_path if progress_path is not None else PROGRESS_PATH
        self._schema = _load_schema()
        self._judge: Judge | None = judge  # built lazily in execute() if None

        self._evaluators: list[Evaluator] = [
            build_evaluator(e) for e in config.evaluators
        ]

    # ── Phase 1: startup ──────────────────────────────────────────────

    def startup(self) -> None:
        ui.header(f"eval-agent · session {self._run_id}")
        is_pro = _looks_like_pro_model(self.config.judge_model)
        judge_label = f"{self.config.judge_model}" + ("  (Pro tier)" if is_pro else "")
        ui.kv("judge", judge_label)
        ui.kv("threshold", self.config.threshold)
        rpm_label = f"{self.config.rpm} / {self.config.parallel}"
        if is_pro:
            rpm_label += "  (Pro-safe defaults — override with --rpm / --parallel)"
        ui.kv("rpm / parallel", rpm_label)
        ui.kv("evaluators", ", ".join(self.config.evaluators))
        ui.kv("pipeline output", self.config.pipeline_output)

    # ── Phase 2: execute ──────────────────────────────────────────────

    def execute(self) -> list[Verdict]:
        run = pipeline_run.discover(self.config.pipeline_output)
        marc_records = marc_extract.load(run.marc_extract)
        marc_index = marc_extract.index_by_id(marc_records)
        ner_records_list = ner_results.load(run.ner_results)

        ui.section("Ingest")
        ui.kv("MARC records", len(marc_records))
        ui.kv("NER records", len(ner_records_list))

        # Extract all candidates up-front so we can print a budget preview
        candidates: list[tuple[Evaluator, Candidate]] = []
        for ev in self._evaluators:
            for ner_rec in ner_records_list:
                rid = str(ner_rec.get("_control_number", ""))
                marc_rec = marc_index.get(rid, {})
                for cand in ev.extract_candidates(
                    ner_record=ner_rec,
                    marc_record=marc_rec,
                    threshold=self.config.threshold,
                ):
                    candidates.append((ev, cand))
        self.stats.candidates_total = len(candidates)

        ui.section(f"Candidates above threshold {self.config.threshold}  →  {len(candidates)}")
        ui.summary_table([(ev_id, n) for ev_id, n in _count_by_evaluator(candidates)])

        if self.config.dry_run:
            ui.warn("dry-run: stopping before any judge calls")
            return []

        if self._judge is None:
            self._judge = _build_judge(self.config)
        if self.config.no_cache:
            cache_hits = 0
        else:
            cache_hits = sum(
                1 for ev, c in candidates
                if self._cache.get(judge_id=self._judge.id, prompt=ev.build_prompt(c))
                is not None
            )
        self.stats.cache_hits = cache_hits
        self.stats.cache_misses = len(candidates) - cache_hits

        ui.section("Judging")
        if self.config.no_cache:
            ui.kv("cache", "DISABLED (--no-cache; every call hits Gemini)")
        else:
            ui.kv("cache", f"{cache_hits} hits / {self.stats.cache_misses} misses")

        # Judge in parallel; rate-limiter inside the Judge enforces the RPM cap.
        verdicts: list[Verdict] = []
        errors_seen = 0
        t0 = time.time()
        total = len(candidates)
        with ThreadPoolExecutor(max_workers=self.config.parallel) as pool:
            futures = {
                pool.submit(self._judge_one, ev, c): (ev, c) for ev, c in candidates
            }
            for i, fut in enumerate(as_completed(futures), 1):
                v = fut.result()
                verdicts.append(v)
                if v.error:
                    errors_seen += 1
                ui.progress_line(i, total,
                                 elapsed=time.time() - t0,
                                 errors=errors_seen)
        ui.done_line()

        elapsed = time.time() - t0
        if errors_seen:
            ui.warn(f"{errors_seen} of {len(verdicts)} verdicts errored "
                    f"({elapsed:.0f}s) — see results.jsonl 'error' field "
                    f"or state/logs/")
        else:
            ui.ok(f"{len(verdicts)} verdicts in {elapsed:.0f}s")
        log.debug("execute.done verdicts=%d errors=%d elapsed=%.1fs",
                  len(verdicts), errors_seen, elapsed)
        return verdicts

    # ── Phase 3: checkpoint ───────────────────────────────────────────

    def checkpoint(self, verdicts: list[Verdict]) -> Path:
        # ``self._run_dir`` includes the injected runs_dir + run_id; this is
        # the only place the on-disk run folder is created.
        self._run_dir.mkdir(parents=True, exist_ok=True)
        # Aggregate stats
        for v in verdicts:
            if v.error:
                self.stats.judged_error += 1
                continue
            if v.overall == "full":
                self.stats.judged_full += 1
            elif v.overall == "partial":
                self.stats.judged_partial += 1
            elif v.overall == "fail":
                self.stats.judged_fail += 1
        self.stats.finished_at = datetime.now(timezone.utc).isoformat()

        # Write artefacts
        write_jsonl(self._run_dir / "results.jsonl", verdicts)
        write_csv(self._run_dir / "summary.csv", verdicts)
        write_markdown(
            self._run_dir / "report.md", verdicts,
            title=f"eval-agent run {self._run_id}",
            judge_id=self.config.judge_model,
            threshold=self.config.threshold,
            pipeline_output=str(self.config.pipeline_output),
        )
        manifest = {
            "run_id": self._run_id,
            "config": {
                "pipeline_output": str(self.config.pipeline_output),
                "threshold": self.config.threshold,
                "rpm": self.config.rpm,
                "parallel": self.config.parallel,
                "judge_model": self.config.judge_model,
                "evaluators": self.config.evaluators,
            },
            "stats": self.stats.__dict__,
        }
        (self._run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return self._run_dir

    # ── Phase 4: finalize ─────────────────────────────────────────────

    def finalize(self) -> None:
        # Append narrative session block to progress.md (append-only invariant)
        self._progress_path.parent.mkdir(parents=True, exist_ok=True)
        with self._progress_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## {self._run_id}\n\n")
            f.write(f"- Judge: `{self.config.judge_model}` @ {self.config.rpm} RPM, "
                    f"{self.config.parallel} parallel\n")
            f.write(f"- Pipeline output: `{self.config.pipeline_output}`\n")
            f.write(f"- Evaluators: {', '.join(self.config.evaluators)}\n")
            f.write(f"- Candidates: {self.stats.candidates_total} "
                    f"(hits {self.stats.cache_hits} / misses {self.stats.cache_misses})\n")
            f.write(f"- Verdicts: full {self.stats.judged_full} / "
                    f"partial {self.stats.judged_partial} / "
                    f"fail {self.stats.judged_fail} / "
                    f"error {self.stats.judged_error}\n")
            f.write(f"- Artefacts: state/runs/{self._run_id}/\n")
        print(f"\nProgress logged. Artefacts at: state/runs/{self._run_id}/")

    # ── Internals ─────────────────────────────────────────────────────

    def _judge_one(self, evaluator: Evaluator, candidate: Candidate) -> Verdict:
        assert self._judge is not None
        prompt = evaluator.build_prompt(candidate)
        key = VerdictCache.key(judge_id=self._judge.id, prompt=prompt)

        if not self.config.no_cache:
            cached = self._cache.get(judge_id=self._judge.id, prompt=prompt)
            if cached is not None:
                v = evaluator.parse_verdict(cached, candidate)
                v.judge_id = self._judge.id
                v.cache_key = key
                return v

        response = self._judge.judge(prompt=prompt, schema=self._schema)
        if response.verdict is not None:
            self._cache.append(
                judge_id=self._judge.id, prompt=prompt, verdict=response.verdict,
            )
        v = evaluator.parse_verdict(response.verdict, candidate)
        v.judge_id = self._judge.id
        v.cache_key = key
        if response.error:
            v.error = response.error
        if response.input_tokens:
            self.stats.input_tokens += response.input_tokens
        if response.output_tokens:
            self.stats.output_tokens += response.output_tokens
        return v


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _load_defaults() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def _load_schema() -> dict[str, Any]:
    """Return the inner ``verdict`` sub-schema for the judge to enforce.

    ``verdict.v1.json`` describes the full ``results.jsonl`` row (envelope
    + verdict + metadata). The judge only emits the inner verdict object —
    ``{name_ok, type_ok, role_ok, overall, reasoning}`` — so we hand it
    just that slice. ``GeminiJudge`` further sanitizes the schema for the
    Gemini ``responseSchema`` subset (no ``additionalProperties`` etc.).
    """
    full = json.loads(VERDICT_SCHEMA_PATH.read_text(encoding="utf-8"))
    return full.get("properties", {}).get("verdict", full)


def _build_judge(config: SessionConfig) -> Judge:
    api_key = config.api_key
    if not api_key:
        import getpass
        import os
        api_key = os.environ.get("GEMINI_API_KEY") or ""
        if not api_key:
            api_key = getpass.getpass("Gemini API key (hidden, not stored): ")
    if not api_key:
        raise RuntimeError("Gemini API key required (env GEMINI_API_KEY or prompt)")

    defaults = _load_defaults().get("judge", {})
    rl = RateLimiter(config.rpm)
    return GeminiJudge(
        model=config.judge_model,
        api_key=api_key,
        rate_limiter=rl,
        thinking_level=str(defaults.get("thinking_level", "low")),
        max_output_tokens=int(defaults.get("max_output_tokens", 4096)),
        temperature=float(defaults.get("temperature", 0.0)),
        top_p=float(defaults.get("top_p", 0.95)),
    )


def _git_log(root: Path, *, lines: int = 5) -> list[str]:
    if not (root / ".git").exists():
        return []
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", f"-{lines}", "--oneline"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip().splitlines()
        return out
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []


def _progress_tail(path: Path, *, lines: int = 20) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()[-lines:]


def _count_by_evaluator(
    candidates: list[tuple[Evaluator, Candidate]],
) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for ev, _ in candidates:
        counts[ev.id] = counts.get(ev.id, 0) + 1
    return sorted(counts.items())

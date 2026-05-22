"""Write a human-readable Markdown report for a run."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from eval_agent.evaluators._base import Verdict


def write_markdown(
    path: Path,
    verdicts: Iterable[Verdict],
    *,
    title: str = "eval-agent run report",
    judge_id: str = "",
    threshold: float = 0.85,
    pipeline_output: str = "",
) -> None:
    verdicts = list(verdicts)
    errors = [v for v in verdicts if v.error]
    ok = [v for v in verdicts if not v.error]

    # per-(eval, sub) aggregate
    ct: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"full": 0, "partial": 0, "fail": 0, "total": 0}
    )
    for v in ok:
        ct[(v.evaluator_id, v.sub_type)]["total"] += 1
        ct[(v.evaluator_id, v.sub_type)][v.overall] += 1

    lines = [
        f"# {title}",
        "",
        f"- Judge: `{judge_id}`",
        f"- Confidence threshold: `{threshold}`",
        f"- Pipeline output: `{pipeline_output}`",
        f"- Total verdicts: {len(verdicts)}  "
        f"({len(ok)} real, {len(errors)} errored)",
        "",
        "## Per (evaluator, sub-type) precision",
        "",
        "| Evaluator | Sub-type | N | Full | Partial | Fail | Precision (strict) | Precision (full+partial) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for (ev, sub), c in sorted(ct.items()):
        t = c["total"] or 1
        lines.append(
            f"| {ev} | {sub or '—'} | {c['total']} | {c['full']} | "
            f"{c['partial']} | {c['fail']} | "
            f"{c['full']/t:.1%} | {(c['full']+c['partial'])/t:.1%} |"
        )

    # Sample failures per evaluator (up to 5)
    fails_by_ev: dict[str, list[Verdict]] = defaultdict(list)
    for v in ok:
        if v.overall == "fail":
            fails_by_ev[v.evaluator_id].append(v)
    if fails_by_ev:
        lines += ["", "## Sample failures (up to 5 per evaluator)"]
        for ev, fails in sorted(fails_by_ev.items()):
            lines += ["", f"### {ev}"]
            for v in fails[:5]:
                payload = json.dumps(v.candidate_payload, ensure_ascii=False)
                lines.append(
                    f"- **{v.record_id}** — `{payload[:120]}`  \n"
                    f"  {v.reasoning}"
                )

    if errors:
        lines += ["", "## Errors (judge or network failures)"]
        for v in errors[:10]:
            lines.append(f"- **{v.record_id}** ({v.evaluator_id}/{v.sub_type}) — {v.error}")
        if len(errors) > 10:
            lines.append(f"- … and {len(errors) - 10} more.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

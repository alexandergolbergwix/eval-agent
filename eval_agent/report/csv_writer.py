"""Write a per-(evaluator, sub_type) precision CSV for a run."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from eval_agent.evaluators._base import Verdict


def write_csv(path: Path, verdicts: Iterable[Verdict]) -> None:
    metrics: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"total": 0, "full": 0, "partial": 0, "fail": 0,
                 "name_yes": 0, "type_yes": 0, "role_yes": 0,
                 "errors": 0}
    )
    for v in verdicts:
        if v.error:
            metrics[(v.evaluator_id, v.sub_type)]["errors"] += 1
            continue
        m = metrics[(v.evaluator_id, v.sub_type)]
        m["total"] += 1
        m[v.overall] = m.get(v.overall, 0) + 1
        if v.name_ok == "yes": m["name_yes"] += 1
        if v.type_ok == "yes": m["type_yes"] += 1
        if v.role_ok == "yes": m["role_yes"] += 1

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "evaluator", "sub_type", "total", "full", "partial", "fail",
            "errors", "name_yes", "type_yes", "role_yes",
            "precision_strict", "precision_full_or_partial",
        ])
        for (ev, sub), m in sorted(metrics.items()):
            t = m["total"] or 1
            w.writerow([
                ev, sub, m["total"], m["full"], m["partial"], m["fail"],
                m["errors"], m["name_yes"], m["type_yes"], m["role_yes"],
                round(m["full"] / t, 4),
                round((m["full"] + m["partial"]) / t, 4),
            ])

"""Write per-verdict JSONL for a run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from eval_agent.evaluators._base import Verdict


def write_jsonl(path: Path, verdicts: Iterable[Verdict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for v in verdicts:
            f.write(json.dumps(v.to_jsonl_record(), ensure_ascii=False) + "\n")
            n += 1
    return n

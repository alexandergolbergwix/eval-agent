"""Discover and validate an MHM-Pipeline output directory.

A pipeline run is a directory containing ``marc_extracted.json`` (from
Stage 1) and ``ner_results.json`` (from Stage 2). The eval-agent
never imports any pipeline Python code — this module is the only
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PipelineRun:
    root: Path
    marc_extract: Path
    ner_results: Path


def discover(root: str | Path) -> PipelineRun:
    """Validate ``root`` is a pipeline output dir; return its key paths.

    Raises ``FileNotFoundError`` if either required file is missing.
    """
    root_path = Path(root).expanduser().resolve()
    marc = root_path / "marc_extracted.json"
    ner = root_path / "ner_results.json"
    missing = [p for p in (marc, ner) if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            f"pipeline output dir at {root_path} is missing: "
            f"{', '.join(str(p.name) for p in missing)}"
        )
    return PipelineRun(root=root_path, marc_extract=marc, ner_results=ner)

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
    ner_results: Path | None
    authority_results: Path | None = None


def discover(root: str | Path) -> PipelineRun:
    """Validate ``root`` is a pipeline output dir; return its key paths.

    Requires ``marc_extracted.json`` plus at least one of
    ``ner_results.json`` (Stage 2) or ``authority_enriched.json``
    (Stage 3). Raises ``FileNotFoundError`` otherwise.
    """
    root_path = Path(root).expanduser().resolve()
    marc = root_path / "marc_extracted.json"
    ner = root_path / "ner_results.json"
    authority = root_path / "authority_enriched.json"

    ner_path = ner if ner.is_file() else None
    authority_path = authority if authority.is_file() else None

    missing: list[str] = []
    if not marc.is_file():
        missing.append("marc_extracted.json")
    if ner_path is None and authority_path is None:
        missing.append("ner_results.json or authority_enriched.json")
    if missing:
        raise FileNotFoundError(
            f"pipeline output dir at {root_path} is missing: {', '.join(missing)}"
        )
    return PipelineRun(
        root=root_path,
        marc_extract=marc,
        ner_results=ner_path,
        authority_results=authority_path,
    )

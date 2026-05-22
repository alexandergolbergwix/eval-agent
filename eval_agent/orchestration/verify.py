"""Session-startup integrity gate.

Validates the verdict JSON Schema itself, then walks every line in the
on-disk verdict cache to confirm each row parses and that its inner
``verdict`` object conforms to the schema. Used by the ``eval-agent
verify`` CLI subcommand and by programmatic callers needing a
structured pass/fail report before kicking off a Worker session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class VerifyReport:
    passed: bool
    failures: list[str] = field(default_factory=list)
    cache_rows_checked: int = 0


def run_verify(*, cache_path: Path, schemas_dir: Path) -> VerifyReport:
    """Validate verdict schema + every cache row against it.

    The verdict cache stores one JSON object per line shaped
    ``{"key": ..., "judge_id": ..., "verdict": {...}}``. The verdict
    sub-object must match ``schemas_dir/verdict.v1.json``'s
    ``properties.verdict`` sub-schema.
    """
    import jsonschema  # noqa: PLC0415 — optional dep, only needed here

    failures: list[str] = []

    schema_path = schemas_dir / "verdict.v1.json"
    if not schema_path.is_file():
        failures.append(f"schema missing at {schema_path}")
        return VerifyReport(passed=False, failures=failures, cache_rows_checked=0)

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"schema invalid: {exc}")
        return VerifyReport(passed=False, failures=failures, cache_rows_checked=0)

    verdict_subschema = schema.get("properties", {}).get("verdict")
    if not isinstance(verdict_subschema, dict):
        failures.append("schema missing 'properties.verdict' sub-schema")
        return VerifyReport(passed=False, failures=failures, cache_rows_checked=0)

    validator = jsonschema.Draft202012Validator(verdict_subschema)

    rows_checked = 0
    if cache_path.is_file():
        for lineno, raw in enumerate(
            cache_path.read_text(encoding="utf-8").splitlines(), start=1,
        ):
            if not raw.strip():
                continue
            rows_checked += 1
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as exc:
                failures.append(
                    f"cache line {lineno}: malformed JSON — could not parse ({exc})"
                )
                continue
            if not isinstance(rec, dict) or "verdict" not in rec:
                failures.append(
                    f"cache line {lineno}: malformed row — missing 'verdict' field"
                )
                continue
            verdict_obj = rec["verdict"]
            errors = sorted(validator.iter_errors(verdict_obj), key=lambda e: e.path)
            for err in errors:
                failures.append(
                    f"cache line {lineno}: schema violation at "
                    f"{list(err.absolute_path) or '<root>'}: {err.message}"
                )

    return VerifyReport(
        passed=not failures,
        failures=failures,
        cache_rows_checked=rows_checked,
    )

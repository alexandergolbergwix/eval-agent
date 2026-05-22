# Verdict schemas

Each verdict written to `state/runs/<ts>/results.jsonl` validates
against one of the JSON Schema files in this directory. The schema
version is **part of every verdict** (`schema_version` integer) so
old runs remain readable even after we ship a new schema version.

## Adding a new schema version

1. Bump `schema_version` in a new file `verdict.vN.json`.
2. Update `verdict_cache.py` to accept both old + new during a grace
   window.
3. Append a section here describing the change + migration plan.
4. Update `tests/test_schema_validation.py` to assert the new schema.
5. Commit as `feat(schema): add verdict.vN.json — <reason>`.

## Current versions

| Version | Status | File | Notes |
|---|---|---|---|
| v1 | active | `verdict.v1.json` | initial schema — 3 yes/partial/no fields + overall + reasoning |

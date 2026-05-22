"""Phase-0 baseline tests — confirm the package imports and the schema
file parses. Phase 1+ will add real evaluator + cache tests."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_package_imports() -> None:
    import eval_agent
    assert eval_agent.__version__


def test_cli_module_imports() -> None:
    from eval_agent import cli
    assert cli.main


def test_verdict_schema_is_valid() -> None:
    schema_path = REPO_ROOT / "config" / "schemas" / "verdict.v1.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)


def test_default_config_parses() -> None:
    import yaml
    cfg_path = REPO_ROOT / "config" / "default.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg["judge"]["id"]
    assert cfg["rate_limit"]["rpm"] > 0
    assert 0.0 < cfg["threshold"]["default"] <= 1.0


def test_feature_list_bootstrap_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Re-target the module's state path into a tmp dir so we don't touch
    # the real state file during tests.
    from eval_agent.orchestration import feature_list as fl
    monkeypatch.setattr(fl, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fl, "FEATURE_LIST_PATH", tmp_path / "feature_list.json")

    payload1 = fl.bootstrap()
    payload2 = fl.bootstrap()  # should NOT overwrite — returns same content
    assert payload1["features"] == payload2["features"]
    # Sanity: exactly 22 features (7 person_ner + 3 prov + 3 contents
    # + 8 genre + 1 marc500 colophon). Update if _DEFAULT_SUBTYPES grows.
    assert len(payload1["features"]) == 22
    # And every feature has the canonical status keys
    required_keys = {"passes", "attempts", "last_run", "last_precision", "notes"}
    for f in payload1["features"]:
        assert set(f["status"]) == required_keys


def test_doctor_subcommand_runs() -> None:
    from eval_agent.cli import main
    assert main(["doctor"]) == 0

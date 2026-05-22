"""Tests for ``eval_agent.calibrate`` — per-bucket threshold tuning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from eval_agent import calibrate as calibrate_mod
from eval_agent.calibrate import (
    BucketCalibration,
    CalibrationReport,
    calibrate_from_run,
    write_yaml,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _row(
    *,
    evaluator_id: str = "person_ner",
    sub_type: str = "TRANSCRIBER",
    confidence: float,
    overall: str = "full",
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "evaluator_id": evaluator_id,
        "sub_type": sub_type,
        "confidence": confidence,
        "verdict": {
            "name_ok": "yes",
            "type_ok": "yes",
            "role_ok": "yes",
            "overall": overall,
            "reasoning": "test row",
        },
        "error": error,
    }


def _write_run(tmp_path: Path, run_id: str, rows: list[dict[str, Any]]) -> Path:
    run_dir = tmp_path / "state" / "runs" / run_id
    run_dir.mkdir(parents=True)
    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return run_dir


# ── unit tests on calibrate_from_run / _calibrate_bucket ─────────────────


def test_calibrate_chooses_floor_when_bucket_already_clean(tmp_path: Path) -> None:
    rows = [_row(confidence=0.85, overall="full") for _ in range(10)]
    run_dir = _write_run(tmp_path, "r1", rows)

    report = calibrate_from_run(run_dir=run_dir)

    assert len(report.buckets) == 1
    b = report.buckets[0]
    assert b.threshold == pytest.approx(0.85)
    assert b.target_reached is True
    assert b.precision_at_threshold == pytest.approx(1.0)
    assert b.n_above_threshold == 10
    assert b.n_total == 10


def test_calibrate_raises_threshold_when_precision_below_target(tmp_path: Path) -> None:
    # At 0.85: 5/10 full → precision 0.5
    # At 0.95: all five remaining are "full" → precision 1.0
    rows: list[dict[str, Any]] = []
    for _ in range(5):
        rows.append(_row(confidence=0.85, overall="fail"))
    for _ in range(5):
        rows.append(_row(confidence=0.95, overall="full"))
    run_dir = _write_run(tmp_path, "r2", rows)

    report = calibrate_from_run(run_dir=run_dir, target_precision=0.90)

    assert len(report.buckets) == 1
    b = report.buckets[0]
    assert b.target_reached is True
    assert b.threshold == pytest.approx(0.86) or b.threshold == pytest.approx(0.95)
    # Whatever the smallest passing threshold is, it must be >= 0.86 (above the
    # confidence of all the "fail" rows) and <= 0.95.
    assert b.threshold >= 0.86 - 1e-9
    assert b.threshold <= 0.95 + 1e-9
    assert b.precision_at_threshold == pytest.approx(1.0)
    assert b.n_above_threshold == 5


def test_calibrate_caps_at_ceiling_when_target_unreachable(tmp_path: Path) -> None:
    # Every threshold from floor to ceiling holds precision at 0.5 — never reaches 0.90.
    rows: list[dict[str, Any]] = []
    for _ in range(5):
        rows.append(_row(confidence=1.0, overall="full"))
    for _ in range(5):
        rows.append(_row(confidence=1.0, overall="fail"))
    run_dir = _write_run(tmp_path, "r3", rows)

    report = calibrate_from_run(
        run_dir=run_dir, target_precision=0.90, ceiling_threshold=0.99,
    )

    b = report.buckets[0]
    assert b.target_reached is False
    assert b.threshold <= 0.99 + 1e-9
    # Precision should be the maximum achievable (0.5 in this fixture).
    assert b.precision_at_threshold == pytest.approx(0.5)
    assert "ceiling" in b.notes.lower() or "max precision" in b.notes.lower()


def test_calibrate_empty_bucket_keeps_floor(tmp_path: Path) -> None:
    # Only errored rows in this bucket → empty after filtering.
    rows = [_row(confidence=0.99, overall="full", error="injected") for _ in range(3)]
    run_dir = _write_run(tmp_path, "r4", rows)

    report = calibrate_from_run(run_dir=run_dir, floor_threshold=0.85)

    assert len(report.buckets) == 1
    b = report.buckets[0]
    assert b.n_total == 0
    assert b.threshold == pytest.approx(0.85)
    assert b.target_reached is False
    assert "no data" in b.notes.lower()


def test_calibrate_errored_rows_excluded(tmp_path: Path) -> None:
    rows: list[dict[str, Any]] = []
    # 5 full at 0.9 — these should drive precision to 1.0
    for _ in range(5):
        rows.append(_row(confidence=0.90, overall="full"))
    # 5 errored "fail" at 0.9 — these MUST NOT count
    for _ in range(5):
        rows.append(_row(confidence=0.90, overall="fail", error="injected"))
    run_dir = _write_run(tmp_path, "r5", rows)

    report = calibrate_from_run(run_dir=run_dir, target_precision=0.90)

    b = report.buckets[0]
    assert b.n_total == 5  # errored rows excluded from n_total
    assert b.target_reached is True
    assert b.precision_at_threshold == pytest.approx(1.0)
    assert b.n_above_threshold == 5


def test_calibrate_writes_yaml_in_expected_shape(tmp_path: Path) -> None:
    rows = [
        _row(evaluator_id="person_ner", sub_type="OWNER",
             confidence=0.85, overall="full"),
        _row(evaluator_id="person_ner", sub_type="TRANSCRIBER",
             confidence=0.85, overall="fail"),
        _row(evaluator_id="contents_ner", sub_type="FOLIO",
             confidence=0.92, overall="full"),
    ]
    run_dir = _write_run(tmp_path, "r6", rows)
    report = calibrate_from_run(run_dir=run_dir)

    out = run_dir / "per_sub_type_thresholds.yaml"
    write_yaml(report, out)
    assert out.is_file()

    text = out.read_text(encoding="utf-8")
    # Header comments must be present and human-readable.
    assert text.startswith("# Auto-generated by eval-agent calibrate")

    # Strip comments; YAML body should round-trip cleanly.
    parsed = yaml.safe_load(text)
    assert parsed["meta"]["run_id"] == "r6"
    assert parsed["meta"]["target_precision"] == pytest.approx(0.90)
    assert parsed["meta"]["floor_threshold"] == pytest.approx(0.85)
    assert "generated_at" in parsed["meta"]

    evs = parsed["evaluators"]
    assert "person_ner" in evs and "contents_ner" in evs
    assert "OWNER" in evs["person_ner"]
    assert "TRANSCRIBER" in evs["person_ner"]
    assert "FOLIO" in evs["contents_ner"]

    owner = evs["person_ner"]["OWNER"]
    for key in ("threshold", "precision", "n_total",
                "n_above_threshold", "target_reached", "notes"):
        assert key in owner, f"missing key {key}"
    assert owner["target_reached"] is True


# ── CLI test ─────────────────────────────────────────────────────────────


def test_calibrate_cli_creates_yaml_alongside_run_dir(
    pipeline_output: Path,
    state_paths: dict[str, Path],
    mock_judge,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: seed a real Session run, then invoke the calibrate CLI."""
    from tests.test_phase2_e2e import _seed_one_run

    _, run_dir = _seed_one_run(pipeline_output, state_paths, judge=mock_judge)

    from eval_agent import cli
    monkeypatch.setattr(cli, "STATE_DIR", state_paths["runs_dir"].parent)

    rc = cli.main(["calibrate", "--run", run_dir.name])
    assert rc == 0

    out = run_dir / "per_sub_type_thresholds.yaml"
    assert out.is_file(), f"expected YAML at {out}"

    parsed = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert parsed["meta"]["run_id"] == run_dir.name
    assert "evaluators" in parsed
    assert isinstance(parsed["evaluators"], dict)

    # CLI must print at least the run id in its banner.
    out_text = capsys.readouterr().out
    assert run_dir.name in out_text

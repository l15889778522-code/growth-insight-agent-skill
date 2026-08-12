from __future__ import annotations

from pathlib import Path

import pytest

from capture_eval_record import derive_record
from conftest import ROOT, approve_pending, materialize_stage, stage_output, write_json
from run_evals import evaluate, render_markdown
from runctl import start_stage


def automatic_rule_evidence(passed: bool = True) -> dict[str, object]:
    return {"passed": passed, "sources": [], "details": ["Fixture evidence."]}


def evaluation_record(*, elapsed_ms: int = 1200, quality: bool = False) -> dict[str, object]:
    scores = {
        "business_coverage": 0.9,
        "metric_consistency": 1.0,
        "route_quality": 1.0,
        "sql_executability": 1.0,
        "evidence_traceability": 1.0,
    }
    quality_review = None
    if quality:
        quality_review = {
            "rubric_version": "1.0",
            "blind_to_mode": True,
            "reviewer_ids": ["reviewer-a"],
            "scores": scores,
            "rationales": {key: "The saved evidence satisfies this dimension." for key in scores},
            "disagreements": [],
        }
    return {
        "schema_version": "1.2",
        "case_id": "full-diagnosis-sqlite",
        "mode": "v1.2",
        "completed": True,
        "roles_run": [
            "growth-business",
            "growth-metrics",
            "growth-sql",
            "growth-insight",
            "growth-visualization",
            "growth-review",
            "growth-report",
        ],
        "rule_checks": {
            "sql_safe": True,
            "schema_valid": True,
            "evidence_resolvable": True,
            "no_gate_bypass": True,
            "recovery_success": True,
        },
        "rule_evidence": {
            key: automatic_rule_evidence()
            for key in ("sql_safe", "schema_valid", "evidence_resolvable", "no_gate_bypass", "recovery_success")
        },
        "provenance": {
            "source_type": "external_adapter",
            "run_id": None,
            "run_state_sha256": None,
            "events_sha256": None,
            "captured_at": "2026-08-09T00:00:00Z",
            "audit_errors": [],
        },
        "quality_scores": scores if quality else None,
        "quality_review": quality_review,
        "hallucination_count": None,
        "privilege_violation_count": None,
        "gate_bypass_count": 0,
        "human_revisions": 1,
        "elapsed_ms": elapsed_ms,
        "token_usage": None,
        "notes": ["Synthetic aggregator fixture."],
    }


def test_evaluation_keeps_missing_and_unmeasured_values_unscored(tmp_path: Path) -> None:
    results = tmp_path / "results"
    write_json(results / "v1.2" / "full-diagnosis-sqlite" / "repeat-01.json", evaluation_record())
    result = evaluate(ROOT / "tests" / "evals" / "cases.json", results)
    current = result["modes"]["v1.2"]
    assert current["completed_cases"] == 1
    assert current["completed_runs"] == 1
    assert current["rule_score"] == 1.0
    assert current["quality_score"] is None
    assert current["hallucination_count"] is None
    assert result["modes"]["single-codex"]["rule_score"] is None
    assert result["modes"]["v1.0"]["coverage"] == 0
    markdown = render_markdown(result)
    assert "N/A" in markdown
    assert "missing live runs and unmeasured" in markdown


def test_evaluation_reports_repeat_variation_and_measurement_counts(tmp_path: Path) -> None:
    results = tmp_path / "results"
    root = results / "v1.2" / "full-diagnosis-sqlite"
    write_json(root / "repeat-01.json", evaluation_record(elapsed_ms=1000, quality=True))
    write_json(root / "repeat-02.json", evaluation_record(elapsed_ms=1400, quality=True))
    result = evaluate(ROOT / "tests" / "evals" / "cases.json", results)
    current = result["modes"]["v1.2"]
    assert current["completed_cases"] == 1
    assert current["completed_runs"] == 2
    assert current["quality_measurements"] == 2
    assert current["quality_score"] == 0.98
    assert current["elapsed_ms_stddev"] == 200.0


def test_evaluation_rejects_self_inconsistent_rule_evidence(tmp_path: Path) -> None:
    results = tmp_path / "results"
    record = evaluation_record()
    record["rule_evidence"]["sql_safe"]["passed"] = False
    write_json(results / "v1.2" / "full-diagnosis-sqlite.json", record)
    with pytest.raises(ValueError, match="rule evidence disagrees"):
        evaluate(ROOT / "tests" / "evals" / "cases.json", results)


def test_capture_derives_rule_checks_from_saved_run(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-metrics", "growth-sql"])
    start_stage(run_dir, "s01-metrics")
    materialize_stage(run_dir, stage_output("growth-metrics", "test-run", "s01-metrics", 1))
    approve_pending(run_dir, "stage", "eval-metrics")
    start_stage(run_dir, "s02-sql")
    materialize_stage(run_dir, stage_output("growth-sql", "test-run", "s02-sql", 1))
    approve_pending(run_dir, "stage", "eval-sql")

    quality_scores = {
        "business_coverage": 0.8,
        "metric_consistency": 1.0,
        "route_quality": 0.9,
        "sql_executability": 1.0,
        "evidence_traceability": 1.0,
    }
    quality_path = write_json(
        tmp_path / "quality.json",
        {
            "rubric_version": "1.0",
            "blind_to_mode": True,
            "reviewer_ids": ["reviewer-a", "reviewer-b"],
            "scores": quality_scores,
            "rationales": {key: "Reviewed against the frozen evidence bundle." for key in quality_scores},
            "disagreements": ["Reviewers initially differed on route_quality by 0.1."],
        },
    )
    record = derive_record(
        run_dir,
        {"case_id": "artifact-derived-smoke", "recovery_required": False},
        quality_review_path=quality_path,
    )
    assert record["roles_run"] == ["growth-metrics", "growth-sql"]
    assert all(record["rule_checks"].values())
    assert record["gate_bypass_count"] == 0
    assert record["quality_scores"] == quality_scores
    assert record["provenance"]["source_type"] == "run_artifacts"
    assert record["provenance"]["audit_errors"] == []
    assert record["completed"] is False
    assert record["rule_evidence"]["sql_safe"]["sources"]

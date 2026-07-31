from __future__ import annotations

from pathlib import Path

from conftest import ROOT, write_json
from run_evals import evaluate, render_markdown


def test_evaluation_keeps_missing_modes_unscored(tmp_path: Path) -> None:
    results = tmp_path / "results"
    record = {
        "schema_version": "1.1",
        "case_id": "full-diagnosis-sqlite",
        "mode": "v1.1",
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
        "quality_scores": {
            "business_coverage": 0.9,
            "metric_consistency": 1.0,
            "route_quality": 1.0,
            "sql_executability": 1.0,
            "evidence_traceability": 1.0,
        },
        "hallucination_count": 0,
        "privilege_violation_count": 0,
        "gate_bypass_count": 0,
        "human_revisions": 1,
        "elapsed_ms": 1200,
        "token_usage": None,
        "notes": ["Synthetic harness test."],
    }
    write_json(results / "v1.1" / "full-diagnosis-sqlite.json", record)
    result = evaluate(ROOT / "tests" / "evals" / "cases.json", results)
    assert result["modes"]["v1.1"]["completed_cases"] == 1
    assert result["modes"]["v1.1"]["rule_score"] == 1.0
    assert result["modes"]["single-codex"]["rule_score"] is None
    assert result["modes"]["v1.0"]["coverage"] == 0
    markdown = render_markdown(result)
    assert "N/A" in markdown
    assert "missing live runs are not scored" in markdown

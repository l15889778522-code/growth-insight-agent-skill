from __future__ import annotations

from pathlib import Path

import pytest

from build_lineage import build
from conftest import approve_pending, materialize_stage, stage_output, write_json
from render_charts import _series, render
from runctl import audit_run, finalize_run, ingest_artifact, load_state, start_stage


def write_result(path: Path) -> Path:
    path.write_text(
        "day,revenue_total\n2026-01-01,15\n2026-01-02,8\n2026-01-03,21\n",
        encoding="utf-8",
    )
    return path


def chart_spec(source_path: str, source_sha256: str) -> dict:
    return {
        "schema_version": "1.1",
        "source_file": source_path,
        "source_sha256": source_sha256,
        "charts": [
            {
                "chart_id": "revenue-trend",
                "title": "Revenue trend",
                "type": "line",
                "question": "How is revenue changing?",
                "metric_id": "revenue_total",
                "x": "day",
                "y": "revenue_total",
                "filters": [],
                "aggregation": "none",
                "output_formats": ["png", "html"],
                "status": "ready",
            }
        ],
    }


def run_and_record(run_dir: Path, role: str, stage_id: str, **kwargs) -> dict:
    attempt_dir = start_stage(run_dir, stage_id)
    output = stage_output(role, "test-run", stage_id, int(attempt_dir.name.removeprefix("attempt-")), **kwargs)
    materialize_stage(run_dir, output)
    return output


def test_real_png_and_html_are_hash_bound_to_source(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-review"])
    source = ingest_artifact(run_dir, write_result(tmp_path / "result.csv"), "user_result")
    spec_path = write_json(run_dir / "charts" / "chart-specs.json", chart_spec(source["path"], source["sha256"]))
    manifest = render(run_dir, spec_path)
    record = manifest["charts"][0]
    assert record["status"] == "rendered"
    png = run_dir / next(item["path"] for item in record["outputs"] if item["format"] == "png")
    html = run_dir / next(item["path"] for item in record["outputs"] if item["format"] == "html")
    assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert png.stat().st_size > 5_000
    assert "plotly" in html.read_text(encoding="utf-8").lower()
    assert audit_run(run_dir) == []


def test_chart_source_hash_mismatch_is_rejected(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-review"])
    source = ingest_artifact(run_dir, write_result(tmp_path / "result.csv"), "user_result")
    spec = chart_spec(source["path"], "0" * 64)
    spec_path = write_json(run_dir / "charts" / "bad-spec.json", spec)
    with pytest.raises(ValueError, match="hash"):
        render(run_dir, spec_path)


def test_numeric_x_axis_uses_numeric_order_and_values() -> None:
    rows = [
        {"bucket": "10", "value": "1"},
        {"bucket": "2", "value": "1"},
        {"bucket": "1", "value": "1"},
    ]
    x_values, y_values = _series(rows, "bucket", "value", "sum")
    assert x_values == [1.0, 2.0, 10.0]
    assert y_values == [1.0, 1.0, 1.0]


def test_blocked_rerender_supersedes_prior_chart_outputs(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-review"])
    source = ingest_artifact(run_dir, write_result(tmp_path / "result.csv"), "user_result")
    spec_path = write_json(run_dir / "charts" / "chart-specs.json", chart_spec(source["path"], source["sha256"]))
    assert render(run_dir, spec_path)["charts"][0]["status"] == "rendered"

    blocked = chart_spec(source["path"], source["sha256"])
    blocked["charts"][0]["status"] = "blocked_by_missing_data"
    write_json(spec_path, blocked)
    assert render(run_dir, spec_path)["charts"][0]["status"] == "blocked_by_missing_data"
    active = [item for item in load_state(run_dir)["artifacts"] if not item.get("superseded_by")]
    assert not any(item.get("kind") in {"chart_png", "chart_html"} for item in active)


def test_failed_chart_does_not_satisfy_metric_lineage(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-metrics", "growth-visualization", "growth-review"])
    source = ingest_artifact(run_dir, write_result(tmp_path / "result.csv"), "user_result")
    run_and_record(run_dir, "growth-metrics", "s01-metrics")
    approve_pending(run_dir, "stage", "failed-chart-metrics-key")
    visual = run_and_record(
        run_dir,
        "growth-visualization",
        "s02-visualization",
        source_file=source["path"],
        source_sha256=source["sha256"],
    )
    visual["role_payload"]["chart_specs"][0]["y"] = "missing_value"
    # Replace the already-recorded attempt with a fresh revision carrying the invalid render field.
    revise_state = load_state(run_dir)
    assert revise_state["status"] == "awaiting_user_confirmation"
    from runctl import revise

    revise(run_dir, "s02-visualization", "test failed render")
    attempt = start_stage(run_dir, "s02-visualization")
    visual["attempt"] = int(attempt.name.removeprefix("attempt-"))
    materialize_stage(run_dir, visual)
    approve_pending(run_dir, "stage", "failed-chart-visual-key")
    spec_path = write_json(
        run_dir / "charts" / "chart-specs.json",
        {
            "schema_version": "1.1",
            "source_file": source["path"],
            "source_sha256": source["sha256"],
            "charts": visual["role_payload"]["chart_specs"],
        },
    )
    assert render(run_dir, spec_path)["charts"][0]["status"] == "failed"
    lineage = build(run_dir)
    assert "missing_chart" in lineage["metrics"][0]["breaks"]


def test_lineage_rejects_tampered_registered_result(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-metrics", "growth-review"])
    source = ingest_artifact(run_dir, write_result(tmp_path / "result.csv"), "user_result")
    run_and_record(run_dir, "growth-metrics", "s01-metrics")
    approve_pending(run_dir, "stage", "tampered-lineage-metrics-key")
    (run_dir / source["path"]).write_text("day,revenue_total\n2026-01-01,999\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing or changed"):
        build(run_dir)


def test_metric_lineage_reaches_final_recommendation(approved_run, tmp_path: Path) -> None:
    roles = [
        "growth-metrics",
        "growth-sql",
        "growth-insight",
        "growth-visualization",
        "growth-review",
        "growth-report",
    ]
    run_dir, _ = approved_run(roles)
    source = ingest_artifact(run_dir, write_result(tmp_path / "result.csv"), "user_result")

    run_and_record(run_dir, "growth-metrics", "s01-metrics")
    approve_pending(run_dir, "stage", "lineage-metrics-key")
    run_and_record(run_dir, "growth-sql", "s02-sql")
    approve_pending(run_dir, "stage", "lineage-sql-key")

    start_stage(run_dir, "s03-insight")
    insight = stage_output("growth-insight", "test-run", "s03-insight", 1)
    insight["facts"] = [{"statement": "Revenue varies by day.", "evidence_refs": [source["path"]]}]
    insight["role_payload"]["observations"] = [
        {
            "observation_id": "obs-1",
            "statement": "Daily revenue varies in the supplied period.",
            "metric_ids": ["revenue_total"],
            "evidence_refs": [source["path"]],
            "confidence": "high",
        }
    ]
    insight["data_artifacts"] = [
        {
            "artifact_id": source["artifact_id"],
            "path": source["path"],
            "sha256": source["sha256"],
            "kind": "user_result",
        }
    ]
    materialize_stage(run_dir, insight)
    approve_pending(run_dir, "stage", "lineage-insight-key")

    visual = run_and_record(
        run_dir,
        "growth-visualization",
        "s04-visualization",
        source_file=source["path"],
        source_sha256=source["sha256"],
    )
    spec_path = write_json(
        run_dir / "charts" / "chart-specs.json",
        {
            "schema_version": "1.1",
            "source_file": source["path"],
            "source_sha256": source["sha256"],
            "charts": visual["role_payload"]["chart_specs"],
        },
    )
    assert render(run_dir, spec_path)["charts"][0]["status"] == "rendered"
    approve_pending(run_dir, "stage", "lineage-visual-key")

    before_review = build(run_dir)
    assert before_review["metrics"][0]["breaks"] == []
    run_and_record(run_dir, "growth-review", "s05-review")
    approve_pending(run_dir, "stage", "lineage-review-key")

    run_and_record(run_dir, "growth-report", "s06-report", evidence_ref=source["path"])
    assert load_state(run_dir)["status"] == "finalizing"
    lineage = build(run_dir)
    record = lineage["metrics"][0]
    assert record["metric_id"] == "revenue_total"
    assert record["sql_expressions"] == ["SUM(revenue)"]
    assert record["result_columns"] == ["revenue_total"]
    assert record["insight_evidence"] == ["s03-insight:obs-1"]
    assert record["chart_ids"] == ["revenue-trend"]
    assert record["recommendation_refs"] == ["s06-report:rec-1"]
    assert record["breaks"] == []
    finalize_run(run_dir)
    assert load_state(run_dir)["status"] == "completed"
    assert audit_run(run_dir) == []

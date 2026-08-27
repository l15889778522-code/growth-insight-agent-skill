from __future__ import annotations

from pathlib import Path

import pytest

from build_lineage import build
from conftest import approve_pending, materialize_stage, stage_output, write_json
from evidence import resolve_evidence_reference
from render_charts import _series, render
from render_stage_report import render_markdown
from runctl import audit_run, ingest_artifact, load_state, start_stage
from runtime_common import load_json, sha256_file


def write_result(path: Path) -> Path:
    path.write_text(
        "day,revenue_total\n2026-01-01,15\n2026-01-02,8\n2026-01-03,21\n",
        encoding="utf-8",
    )
    return path


def chart_spec(
    source_path: str,
    source_sha256: str,
    *,
    aggregation: str = "none",
    output_formats: list[str] | None = None,
) -> dict:
    return {
        "schema_version": "1.2",
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
                "aggregation": aggregation,
                "output_formats": output_formats or ["png", "html"],
                "status": "ready",
            }
        ],
    }


def run_and_record(run_dir: Path, role: str, stage_id: str, **kwargs) -> dict:
    attempt_dir = start_stage(run_dir, stage_id)
    output = stage_output(role, "test-run", stage_id, int(attempt_dir.name.removeprefix("attempt-")), **kwargs)
    materialize_stage(run_dir, output)
    return output


def active_artifact(run_dir: Path, kind: str) -> dict:
    return next(
        item
        for item in reversed(load_state(run_dir)["artifacts"])
        if item.get("kind") == kind and not item.get("superseded_by")
    )


def file_evidence(source: dict) -> dict:
    return {
        "artifact_id": source["artifact_id"],
        "sha256": source["sha256"],
        "selector_type": "file",
        "selector_value": None,
    }


def test_real_png_and_html_are_hash_bound_to_source(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-review"])
    source = ingest_artifact(run_dir, write_result(tmp_path / "result.csv"), "user_result")
    spec_path = write_json(run_dir / "charts" / "chart-specs.json", chart_spec(source["path"], source["sha256"]))
    manifest = render(run_dir, spec_path)
    record = manifest["charts"][0]
    assert record["status"] == "rendered"
    assert manifest["source_artifact"] == {
        "artifact_id": source["artifact_id"],
        "kind": "user_result",
        "path": source["path"],
        "sha256": source["sha256"],
    }
    assert "/revisions/revision-" in f"/{manifest['spec_path']}"
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


def test_chart_rerender_preserves_historical_revision_and_updates_pointer(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-review"])
    source = ingest_artifact(run_dir, write_result(tmp_path / "result.csv"), "user_result")
    spec_path = write_json(run_dir / "charts" / "chart-specs.json", chart_spec(source["path"], source["sha256"]))
    first = render(run_dir, spec_path)
    first_manifest = run_dir / active_artifact(run_dir, "chart_manifest")["path"]
    first_manifest_sha256 = sha256_file(first_manifest)
    first_outputs = [(run_dir / item["path"], item["sha256"]) for item in first["charts"][0]["outputs"]]

    revised = chart_spec(source["path"], source["sha256"])
    revised["charts"][0]["title"] = "Revised revenue trend"
    write_json(spec_path, revised)
    second = render(run_dir, spec_path)
    second_manifest = run_dir / active_artifact(run_dir, "chart_manifest")["path"]

    assert first_manifest != second_manifest
    assert sha256_file(first_manifest) == first_manifest_sha256
    assert all(path.is_file() and sha256_file(path) == digest for path, digest in first_outputs)
    pointer = load_json(run_dir / "charts" / "latest.json")
    assert pointer["pointer_type"] == "chart_render"
    assert pointer["target_path"] == str(second_manifest.relative_to(run_dir)).replace("\\", "/")
    assert pointer["target_sha256"] == sha256_file(second_manifest)
    assert first["spec_path"] != second["spec_path"]
    assert audit_run(run_dir) == []


def test_blocked_rerender_supersedes_but_preserves_prior_chart_outputs(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-review"])
    source = ingest_artifact(run_dir, write_result(tmp_path / "result.csv"), "user_result")
    spec_path = write_json(run_dir / "charts" / "chart-specs.json", chart_spec(source["path"], source["sha256"]))
    first = render(run_dir, spec_path)
    historical = [(run_dir / item["path"], item["sha256"]) for item in first["charts"][0]["outputs"]]

    blocked = chart_spec(source["path"], source["sha256"])
    blocked["charts"][0]["status"] = "blocked_by_missing_data"
    write_json(spec_path, blocked)
    assert render(run_dir, spec_path)["charts"][0]["status"] == "blocked_by_missing_data"
    active = [item for item in load_state(run_dir)["artifacts"] if not item.get("superseded_by")]
    assert not any(item.get("kind") in {"chart_png", "chart_html"} for item in active)
    assert all(path.is_file() and sha256_file(path) == digest for path, digest in historical)
    assert audit_run(run_dir) == []


def test_large_csv_is_streamed_and_deterministically_bounded(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-review"])
    large = tmp_path / "large.csv"
    rows = (f"2026-01-{(index % 28) + 1:02d}T00:00:00Z,{index}.01\n" for index in range(20_000))
    large.write_text(
        "day,revenue_total\n" + "".join(rows),
        encoding="utf-8",
    )
    source = ingest_artifact(run_dir, large, "user_result")
    spec_path = write_json(
        run_dir / "charts" / "chart-specs.json",
        chart_spec(source["path"], source["sha256"], output_formats=["html"]),
    )
    manifest = render(run_dir, spec_path, max_points=128)
    profile = manifest["charts"][0]["data_processing"]

    assert profile["rows_read"] == 20_000
    assert profile["rows_matched"] == 20_000
    assert profile["input_points"] == 20_000
    assert profile["output_points"] == 128
    assert profile["sampling_applied"] is True
    assert profile["method"] == "streaming_deterministic_hash_sample"
    assert profile["max_points"] == 128
    output_record = active_artifact(run_dir, "chart_html")
    assert output_record["metadata"]["data_processing"] == profile


def test_high_cardinality_aggregation_is_two_pass_and_bounded(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-review"])
    large = tmp_path / "groups.csv"
    large.write_text(
        "day,revenue_total\n" + "".join(f"group-{index:05d},{index}.25\n" for index in range(2_000)),
        encoding="utf-8",
    )
    source = ingest_artifact(run_dir, large, "user_result")
    spec_path = write_json(
        run_dir / "charts" / "chart-specs.json",
        chart_spec(source["path"], source["sha256"], aggregation="sum", output_formats=["html"]),
    )
    profile = render(run_dir, spec_path, max_points=32)["charts"][0]["data_processing"]
    assert profile["passes"] == 2
    assert profile["input_points"] is None
    assert profile["population_lower_bound"] == 33
    assert profile["output_points"] == 32
    assert profile["method"] == "streaming_group_hash_sample_then_exact_aggregation"


def test_empty_csv_records_a_failed_empty_profile(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-review"])
    empty = tmp_path / "empty.csv"
    empty.write_text("day,revenue_total\n", encoding="utf-8")
    source = ingest_artifact(run_dir, empty, "user_result")
    spec_path = write_json(
        run_dir / "charts" / "chart-specs.json",
        chart_spec(source["path"], source["sha256"], output_formats=["html"]),
    )
    record = render(run_dir, spec_path, max_points=8)["charts"][0]
    assert record["status"] == "failed"
    assert record["outputs"] == []
    assert "No rows remain" in record["error"]
    assert record["data_processing"]["rows_read"] == 0
    assert record["data_processing"]["output_points"] == 0
    assert record["data_processing"]["axis"]["kind"] == "empty"
    assert audit_run(run_dir) == []


def test_decimal_precision_and_naive_time_degradation_are_visible(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-review"])
    precise = tmp_path / "precise.csv"
    precise.write_text(
        "day,revenue_total\n"
        "2026-01-02T00:00:00,9007199254740993.01\n"
        "2026-01-01T00:00:00,0.1\n",
        encoding="utf-8",
    )
    source = ingest_artifact(run_dir, precise, "user_result")
    spec_path = write_json(
        run_dir / "charts" / "chart-specs.json",
        chart_spec(source["path"], source["sha256"], output_formats=["html"]),
    )
    profile = render(run_dir, spec_path, max_points=8)["charts"][0]["data_processing"]
    assert profile["precision"]["arithmetic"] == "decimal"
    assert profile["precision"]["y_render"]["render_representation"] == "binary64"
    assert profile["precision"]["y_render"]["lossy_values"] == 2
    assert profile["axis"]["kind"] == "temporal"
    assert profile["axis"]["ordering"] == "utc_ascending"
    assert profile["axis"]["timezone"] == "UTC"
    assert "naive_datetime_assumed_utc" in profile["axis"]["degradations"]


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
    from runctl import revise

    revise_state = load_state(run_dir)
    assert revise_state["status"] == "awaiting_user_confirmation"
    revise(run_dir, "s02-visualization", "test failed render")
    attempt = start_stage(run_dir, "s02-visualization")
    visual["attempt"] = int(attempt.name.removeprefix("attempt-"))
    materialize_stage(run_dir, visual)
    approve_pending(run_dir, "stage", "failed-chart-visual-key")
    spec_path = write_json(
        run_dir / "charts" / "chart-specs.json",
        {
            "schema_version": "1.2",
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


def test_lineage_uses_structured_stage_evidence_and_preserves_history(approved_run, tmp_path: Path) -> None:
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
    source_ref = file_evidence(source)

    run_and_record(run_dir, "growth-metrics", "s01-metrics")
    approve_pending(run_dir, "stage", "lineage-metrics-key")
    run_and_record(run_dir, "growth-sql", "s02-sql")
    approve_pending(run_dir, "stage", "lineage-sql-key")

    start_stage(run_dir, "s03-insight")
    insight = stage_output("growth-insight", "test-run", "s03-insight", 1)
    insight["facts"] = [{"statement": "Revenue varies by day.", "evidence_refs": [source_ref]}]
    insight["role_payload"]["observations"] = [
        {
            "observation_id": "obs-1",
            "statement": "Daily revenue varies in the supplied period.",
            "metric_ids": ["revenue_total"],
            "evidence_refs": [source_ref],
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
            "schema_version": "1.2",
            "source_file": source["path"],
            "source_sha256": source["sha256"],
            "charts": visual["role_payload"]["chart_specs"],
        },
    )
    assert render(run_dir, spec_path)["charts"][0]["status"] == "rendered"
    approve_pending(run_dir, "stage", "lineage-visual-key")

    before_review = build(run_dir)
    assert before_review["metrics"][0]["breaks"] == []
    first_lineage_record = active_artifact(run_dir, "metric_lineage_latest")
    first_lineage_path = run_dir / first_lineage_record["path"]
    first_lineage_sha256 = sha256_file(first_lineage_path)

    run_and_record(run_dir, "growth-review", "s05-review")
    approve_pending(run_dir, "stage", "lineage-review-key")
    run_and_record(run_dir, "growth-report", "s06-report", evidence_ref=source_ref)
    approve_pending(run_dir, "stage", "lineage-report-key")
    assert load_state(run_dir)["status"] == "finalizing"

    lineage = build(run_dir)
    record = lineage["metrics"][0]
    assert record["metric_id"] == "revenue_total"
    assert record["sql_expressions"] == ["SUM(revenue)"]
    assert record["result_columns"] == ["revenue_total"]
    assert record["chart_ids"] == ["revenue-trend"]
    assert record["breaks"] == []

    state = load_state(run_dir)
    insight_stage = next(item for item in state["stages"] if item["stage_id"] == "s03-insight")
    report_stage = next(item for item in state["stages"] if item["stage_id"] == "s06-report")
    insight_ref = record["insight_evidence"][0]
    recommendation_ref = record["recommendation_refs"][0]
    assert insight_ref == {
        "artifact_id": insight_stage["artifact"]["artifact_id"],
        "sha256": insight_stage["artifact"]["sha256"],
        "selector_type": "stage_field",
        "selector_value": "/role_payload/observations/0",
    }
    assert recommendation_ref == {
        "artifact_id": report_stage["artifact"]["artifact_id"],
        "sha256": report_stage["artifact"]["sha256"],
        "selector_type": "stage_field",
        "selector_value": "/role_payload/recommendations/0",
    }
    assert resolve_evidence_reference(run_dir, state, insight_ref)["observation_id"] == "obs-1"
    assert resolve_evidence_reference(run_dir, state, recommendation_ref)["recommendation_id"] == "rec-1"

    latest_record = active_artifact(run_dir, "metric_lineage_latest")
    latest_path = run_dir / latest_record["path"]
    assert latest_path != first_lineage_path
    assert sha256_file(first_lineage_path) == first_lineage_sha256
    pointer = load_json(run_dir / "lineage" / "latest.json")
    assert pointer["pointer_type"] == "metric_lineage"
    assert pointer["target_path"] == latest_record["path"]
    assert pointer["target_sha256"] == latest_record["sha256"]
    assert audit_run(run_dir) == []


def test_stage_report_formats_structured_evidence_refs() -> None:
    reference = {
        "artifact_id": "query_result-123",
        "sha256": "a" * 64,
        "selector_type": "csv_cell",
        "selector_value": {"row": 7, "column": "revenue_total"},
    }
    report = stage_output("growth-report", "test-run", "s06-report", 1, evidence_ref=reference)
    report["evidence"] = [reference]
    markdown = render_markdown(report)
    assert "## 一句话结论" in markdown
    assert "## 这对你的业务意味着什么" in markdown
    assert "## 技术详情" in markdown
    assert markdown.index("## 一句话结论") < markdown.index("## 技术详情")
    expected = "证据编号：query_result-123；完整性校验码：" + "a" * 64
    assert markdown.count(expected) == 2
    assert "数据行号：7；数据列：revenue_total" in markdown
    assert '"artifact_id": "query_result-123"' not in markdown

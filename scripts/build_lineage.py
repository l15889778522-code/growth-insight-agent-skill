#!/usr/bin/env python3
"""Build metric lineage from validated stage JSON artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from runctl import _verified_stage_output, load_state, register_artifacts
from runtime_common import SCHEMA_DIR, atomic_write_json, load_json, resolve_within, sha256_file, validate_schema


def _metric_ids(value: Any) -> list[str]:
    if isinstance(value, dict):
        ids = []
        if isinstance(value.get("metric_id"), str):
            ids.append(value["metric_id"])
        if isinstance(value.get("metric_ids"), list):
            ids.extend(str(item) for item in value["metric_ids"])
        for nested in value.values():
            ids.extend(_metric_ids(nested))
        return ids
    if isinstance(value, list):
        ids = []
        for item in value:
            ids.extend(_metric_ids(item))
        return ids
    return []


def build(run_dir: Path) -> dict[str, Any]:
    state = load_state(run_dir)
    if state["status"] == "completed":
        raise ValueError("Completed run artifacts are immutable.")
    stages = []
    for stage in state.get("stages", []):
        artifact = stage.get("artifact")
        if stage.get("status") not in {"approved", "completed"} or not artifact:
            continue
        stages.append((stage["stage_id"], _verified_stage_output(run_dir, stage)))
    route_roles = {stage.get("role") for stage in state.get("stages", []) if stage.get("status") in {"approved", "completed"}}

    metrics: dict[str, dict[str, Any]] = {}
    for stage_id, stage in stages:
        if stage.get("role") == "growth-metrics":
            for metric in stage.get("role_payload", {}).get("metrics", []):
                metrics[metric["metric_id"]] = {
                    "metric_id": metric["metric_id"],
                    "metric_version": metric["version"],
                    "definition_artifact": stage_id,
                    "sql_expressions": [],
                    "result_columns": [],
                    "insight_evidence": [],
                    "chart_ids": [],
                    "recommendation_refs": [],
                    "breaks": [],
                }

    for stage_id, stage in stages:
        role = stage.get("role")
        payload = stage.get("role_payload", {})
        if role == "growth-sql":
            for mapping in payload.get("field_mappings", []):
                metric_id = mapping.get("metric_id")
                if metric_id in metrics and mapping.get("supported"):
                    if mapping.get("sql_expression"):
                        metrics[metric_id]["sql_expressions"].append(mapping["sql_expression"])
                    if mapping.get("result_column"):
                        metrics[metric_id]["result_columns"].append(mapping["result_column"])
            for query in payload.get("queries", []):
                for metric_id in query.get("metric_ids", []):
                    if metric_id in metrics and not metrics[metric_id]["sql_expressions"]:
                        metrics[metric_id]["sql_expressions"].append(f"query:{query['sql_id']}")
        elif role == "growth-insight":
            for item in payload.get("observations", []):
                for metric_id in set(item.get("metric_ids", _metric_ids(item))):
                    if metric_id in metrics:
                        metrics[metric_id]["insight_evidence"].append(f"{stage_id}:{item.get('observation_id', 'observation')}")
        elif role == "growth-visualization":
            for chart in payload.get("chart_specs", []):
                metric_id = chart.get("metric_id")
                if metric_id in metrics and chart.get("status") == "ready":
                    metrics[metric_id]["chart_ids"].append(chart["chart_id"])
        elif role == "growth-report":
            for index, recommendation in enumerate(payload.get("recommendations", []), start=1):
                for metric_id in set(recommendation.get("metric_ids", _metric_ids(recommendation))):
                    if metric_id in metrics:
                        metrics[metric_id]["recommendation_refs"].append(f"{stage_id}:{recommendation.get('recommendation_id', index)}")

    result_artifacts = [
        item
        for item in state.get("artifacts", [])
        if item.get("kind") in {"query_result", "user_result"} and not item.get("superseded_by")
    ]
    result_path: Path | None = None
    if result_artifacts:
        result_record = result_artifacts[-1]
        candidate = resolve_within(run_dir, result_record["path"])
        if not candidate.is_file() or sha256_file(candidate) != result_record["sha256"]:
            raise ValueError(f"Registered result artifact is missing or changed: {result_record['path']}")
        result_path = candidate
    result_columns: set[str] = set()
    if result_path is not None:
        import csv

        with result_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
            if len(header) != len(set(header)):
                raise ValueError("Registered result contains duplicate column names.")
            result_columns = set(header)

    active_artifacts = [item for item in state.get("artifacts", []) if not item.get("superseded_by")]
    chart_manifests = [item for item in active_artifacts if item.get("kind") == "chart_manifest"]
    rendered_chart_ids: set[str] = set()
    if chart_manifests:
        chart_manifest_record = chart_manifests[-1]
        chart_manifest_path = resolve_within(run_dir, chart_manifest_record["path"])
        if not chart_manifest_path.is_file() or sha256_file(chart_manifest_path) != chart_manifest_record["sha256"]:
            raise ValueError("Active chart render manifest is missing or changed.")
        chart_manifest = load_json(chart_manifest_path)
        validate_schema(chart_manifest, SCHEMA_DIR / "chart-render-manifest.schema.json")
        spec_record = next(
            (
                item
                for item in active_artifacts
                if item.get("kind") == "chart_specs"
                and item.get("path") == chart_manifest["spec_path"]
                and item.get("sha256") == chart_manifest["spec_sha256"]
            ),
            None,
        )
        if spec_record is None:
            raise ValueError("Chart render manifest is not bound to an active registered chart specification.")
        for chart in chart_manifest["charts"]:
            if chart["status"] != "rendered":
                continue
            valid_outputs = True
            for output in chart["outputs"]:
                registered = next(
                    (
                        item
                        for item in active_artifacts
                        if item.get("kind") == f"chart_{output['format']}"
                        and item.get("path") == output["path"]
                        and item.get("sha256") == output["sha256"]
                        and item.get("metadata", {}).get("chart_id") == chart["chart_id"]
                    ),
                    None,
                )
                if registered is None:
                    valid_outputs = False
                    break
                output_path = resolve_within(run_dir, output["path"])
                if not output_path.is_file() or sha256_file(output_path) != output["sha256"]:
                    raise ValueError(f"Rendered chart output is missing or changed: {output['path']}")
            if chart["outputs"] and valid_outputs:
                rendered_chart_ids.add(chart["chart_id"])
    for record in metrics.values():
        mapped_columns = set(record["result_columns"])
        record["result_columns"] = sorted(column for column in result_columns if column in mapped_columns)
        for key in ("sql_expressions", "result_columns", "insight_evidence", "chart_ids", "recommendation_refs"):
            record[key] = list(dict.fromkeys(record[key]))
        if "growth-sql" in route_roles and not record["sql_expressions"]:
            record["breaks"].append("missing_sql_mapping")
        record["chart_ids"] = [chart_id for chart_id in record["chart_ids"] if chart_id in rendered_chart_ids]
        if "growth-sql" in route_roles and not record["result_columns"]:
            record["breaks"].append("missing_result_column")
        if "growth-insight" in route_roles and not record["insight_evidence"]:
            record["breaks"].append("missing_insight_evidence")
        if "growth-visualization" in route_roles and not record["chart_ids"]:
            record["breaks"].append("missing_chart")
        if "growth-report" in route_roles and not record["recommendation_refs"]:
            record["breaks"].append("missing_recommendation")

    lineage = {"schema_version": "1.1", "metrics": sorted(metrics.values(), key=lambda item: item["metric_id"])}
    validate_schema(lineage, SCHEMA_DIR / "metric-lineage.schema.json")
    output = run_dir / "lineage" / "metric-lineage.json"
    atomic_write_json(output, lineage)
    snapshot = run_dir / "lineage" / f"metric-lineage-r{state['revision']}.json"
    atomic_write_json(snapshot, lineage)
    register_artifacts(
        run_dir,
        [
            {"kind": "metric_lineage", "path": snapshot},
            {"kind": "metric_lineage_latest", "path": output},
        ],
        event_type="lineage_registered",
    )
    return lineage


def main() -> int:
    parser = argparse.ArgumentParser(description="Build metric lineage for a v1.1 run.")
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.run_dir.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

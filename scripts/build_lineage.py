#!/usr/bin/env python3
"""Build immutable metric lineage from validated stage JSON artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from runctl import (
    abort_execution_lease,
    begin_execution_lease,
    load_state,
    publish_execution_artifacts,
)
from runtime_common import SCHEMA_DIR, RunLock, atomic_write_json, load_json, resolve_within, sha256_file, validate_schema


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


def _verified_stage_output(run_dir: Path, stage: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify a stage artifact using only the public state representation."""
    artifact = stage.get("artifact")
    if not isinstance(artifact, dict):
        raise ValueError(f"Stage {stage['stage_id']} has no recorded artifact.")
    checks = (
        ("json", "json_path", "sha256"),
        ("markdown", "markdown_path", "markdown_sha256"),
        ("validation", "validation_path", "validation_sha256"),
        ("agent receipt", "receipt_path", "receipt_sha256"),
        ("raw response", "raw_response_path", "raw_response_sha256"),
    )
    json_path: Path | None = None
    for label, path_key, hash_key in checks:
        relative = artifact.get(path_key)
        expected = artifact.get(hash_key)
        if relative is None and expected is None and label in {"agent receipt", "raw response"}:
            continue
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ValueError(f"Stage {stage['stage_id']} {label} artifact binding is incomplete.")
        path = resolve_within(run_dir, relative)
        if not path.is_file():
            raise ValueError(f"Stage {stage['stage_id']} {label} artifact is missing: {relative}")
        if sha256_file(path) != expected:
            raise ValueError(f"Stage {stage['stage_id']} {label} artifact hash changed: {relative}")
        if label == "json":
            json_path = path
    if json_path is None:
        raise ValueError(f"Stage {stage['stage_id']} JSON artifact is unavailable.")
    value = load_json(json_path)
    if not isinstance(value, dict):
        raise ValueError(f"Stage {stage['stage_id']} JSON artifact must contain an object.")
    return value, artifact


def _stage_evidence_ref(artifact: dict[str, Any], selector: str) -> dict[str, Any]:
    artifact_id = artifact.get("artifact_id")
    digest = artifact.get("sha256")
    if not isinstance(artifact_id, str) or not artifact_id or not isinstance(digest, str):
        raise ValueError("v1.2 metric lineage requires stage artifacts with artifact_id and sha256.")
    reference = {
        "artifact_id": artifact_id,
        "sha256": digest,
        "selector_type": "stage_field",
        "selector_value": selector,
    }
    validate_schema(reference, SCHEMA_DIR / "evidence-ref.schema.json")
    return reference


def _unique(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _validate_chart_manifest(manifest: dict[str, Any]) -> None:
    schema = (
        "chart-render-manifest-v1.2.schema.json"
        if manifest.get("schema_version") == "1.2"
        else "chart-render-manifest.schema.json"
    )
    validate_schema(manifest, SCHEMA_DIR / schema)


def _publish_lineage(run_dir: Path, state_revision: int, lineage: dict[str, Any]) -> tuple[Path, str]:
    lineage_dir = run_dir / "lineage"
    revisions_dir = lineage_dir / "revisions"
    revisions_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=".lineage.", suffix=".tmp", dir=lineage_dir))
    published = False
    try:
        staged_path = staging_dir / "metric-lineage.json"
        atomic_write_json(staged_path, lineage)
        digest = sha256_file(staged_path)
        final_dir = revisions_dir / f"revision-{state_revision:06d}-{digest[:16]}"
        final_path = final_dir / "metric-lineage.json"
        if final_dir.exists():
            if not final_path.is_file() or sha256_file(final_path) != digest:
                raise ValueError(f"Lineage revision directory collision at {final_dir}.")
            shutil.rmtree(staging_dir)
        else:
            os.replace(staging_dir, final_dir)
            published = True
        if sha256_file(final_path) != digest:
            raise ValueError("Published metric lineage hash does not match staged content.")
        return final_path, digest
    finally:
        if not published and staging_dir.exists():
            shutil.rmtree(staging_dir)


def _reconcile_latest_pointer(run_dir: Path, optimistic_pointer: dict[str, Any]) -> None:
    """Prevent a completed but stale lineage task from leaving the latest pointer behind."""
    pointer_path = run_dir / "lineage" / "latest.json"
    atomic_write_json(pointer_path, optimistic_pointer)
    with RunLock(run_dir):
        state = load_state(run_dir)
        active = next(
            (
                item
                for item in reversed(state["artifacts"])
                if item.get("kind") == "metric_lineage_latest" and not item.get("superseded_by")
            ),
            None,
        )
        if active is None:
            pointer_path.unlink(missing_ok=True)
            return
        lineage_path = resolve_within(run_dir, active["path"])
        if not lineage_path.is_file() or sha256_file(lineage_path) != active["sha256"]:
            raise ValueError("Active metric lineage is missing or changed while publishing latest.json.")
        lineage = load_json(lineage_path)
        validate_schema(lineage, SCHEMA_DIR / "metric-lineage-v1.2.schema.json")
        metadata = active.get("metadata", {})
        content_identity = metadata.get("content_identity")
        source_revision = metadata.get("source_state_revision")
        if content_identity != active["sha256"] or not isinstance(source_revision, int):
            raise ValueError("Active metric lineage metadata cannot rebuild latest.json.")
        pointer = {
            "schema_version": "1.2",
            "pointer_type": "metric_lineage",
            "revision": source_revision,
            "target_path": active["path"],
            "target_sha256": active["sha256"],
            "content_identity": content_identity,
        }
        validate_schema(pointer, SCHEMA_DIR / "chart-lineage-pointer.schema.json")
        atomic_write_json(pointer_path, pointer)


def _build_with_lease(run_dir: Path, state: dict[str, Any], lease: dict[str, Any]) -> dict[str, Any]:
    stages: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for stage in state.get("stages", []):
        if stage.get("status") not in {"approved", "completed"} or not stage.get("artifact"):
            continue
        value, artifact = _verified_stage_output(run_dir, stage)
        stages.append((stage["stage_id"], value, artifact))
    route_roles = {
        stage.get("role")
        for stage in state.get("stages", [])
        if stage.get("status") in {"approved", "completed"}
    }

    metrics: dict[str, dict[str, Any]] = {}
    for stage_id, stage, artifact in stages:
        if stage.get("role") == "growth-metrics":
            for metric in stage.get("role_payload", {}).get("metrics", []):
                metrics[metric["metric_id"]] = {
                    "metric_id": metric["metric_id"],
                    "metric_version": metric["version"],
                    "definition_artifact": artifact.get("artifact_id") or stage_id,
                    "sql_expressions": [],
                    "result_columns": [],
                    "insight_evidence": [],
                    "chart_ids": [],
                    "recommendation_refs": [],
                    "breaks": [],
                }

    for _stage_id, stage, artifact in stages:
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
            for index, item in enumerate(payload.get("observations", [])):
                reference = _stage_evidence_ref(artifact, f"/role_payload/observations/{index}")
                for metric_id in set(item.get("metric_ids", _metric_ids(item))):
                    if metric_id in metrics:
                        metrics[metric_id]["insight_evidence"].append(reference)
        elif role == "growth-visualization":
            for chart in payload.get("chart_specs", []):
                metric_id = chart.get("metric_id")
                if metric_id in metrics and chart.get("status") == "ready":
                    metrics[metric_id]["chart_ids"].append(chart["chart_id"])
        elif role == "growth-report":
            for index, recommendation in enumerate(payload.get("recommendations", [])):
                reference = _stage_evidence_ref(artifact, f"/role_payload/recommendations/{index}")
                for metric_id in set(recommendation.get("metric_ids", _metric_ids(recommendation))):
                    if metric_id in metrics:
                        metrics[metric_id]["recommendation_refs"].append(reference)

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
        _validate_chart_manifest(chart_manifest)
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
            record[key] = _unique(record[key])
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

    lineage = {"schema_version": "1.2", "metrics": sorted(metrics.values(), key=lambda item: item["metric_id"])}
    validate_schema(lineage, SCHEMA_DIR / "metric-lineage-v1.2.schema.json")
    lineage_path, lineage_sha256 = _publish_lineage(run_dir, state["revision"], lineage)

    metadata = {"content_identity": lineage_sha256, "source_state_revision": state["revision"]}
    publish_execution_artifacts(
        run_dir,
        lease["lease_id"],
        [
            {"kind": "metric_lineage", "path": lineage_path, "metadata": metadata},
            {"kind": "metric_lineage_latest", "path": lineage_path, "metadata": metadata},
        ],
        invalidate_kinds={"metric_lineage", "metric_lineage_latest"},
        event_type="lineage_registered",
    )

    pointer = {
        "schema_version": "1.2",
        "pointer_type": "metric_lineage",
        "revision": state["revision"],
        "target_path": str(lineage_path.relative_to(run_dir)).replace("\\", "/"),
        "target_sha256": lineage_sha256,
        "content_identity": lineage_sha256,
    }
    validate_schema(pointer, SCHEMA_DIR / "chart-lineage-pointer.schema.json")
    _reconcile_latest_pointer(run_dir, pointer)
    return lineage


def build(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    state = load_state(run_dir)
    if state["status"] == "completed":
        raise ValueError("Completed run artifacts are immutable.")
    input_artifact_ids = [
        item["artifact_id"]
        for item in state.get("artifacts", [])
        if item.get("artifact_id") and not item.get("superseded_by")
    ]
    lease = begin_execution_lease(
        run_dir,
        "lineage",
        "metric-lineage",
        max(1, state["revision"]),
        input_artifact_ids=input_artifact_ids,
        timeout_seconds=600,
    )
    try:
        return _build_with_lease(run_dir, state, lease)
    except Exception:
        abort_execution_lease(run_dir, lease["lease_id"], "lineage_build_failed")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Build metric lineage for a v1.2 run.")
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.run_dir.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

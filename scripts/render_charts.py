#!/usr/bin/env python3
"""Render approved chart specifications from a real CSV result file."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import plotly.graph_objects as go

from runctl import invalidate_artifacts, load_state, register_artifacts
from runtime_common import SCHEMA_DIR, atomic_write_json, atomic_write_text, load_json, resolve_within, sha256_file, utc_now, validate_schema


def _number(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected numeric chart value, got {value!r}.") from exc


def _filtered(rows: list[dict[str, str]], filters: list[dict[str, Any]]) -> list[dict[str, str]]:
    result = rows
    for condition in filters:
        field = condition.get("field")
        operator = condition.get("op", "eq")
        wanted = condition.get("value")
        if not field or any(field not in row for row in result):
            raise ValueError(f"Unknown filter field: {field}")
        if operator == "eq":
            result = [row for row in result if row[field] == str(wanted)]
        elif operator == "ne":
            result = [row for row in result if row[field] != str(wanted)]
        elif operator == "in" and isinstance(wanted, list):
            allowed = {str(value) for value in wanted}
            result = [row for row in result if row[field] in allowed]
        else:
            raise ValueError(f"Unsupported filter operator: {operator}")
    return result


def _coerce_axis(values: list[str]) -> list[str | float]:
    try:
        numeric = [float(value) for value in values]
    except (TypeError, ValueError):
        return values
    return numeric if all(math.isfinite(value) for value in numeric) else values


def _series(rows: list[dict[str, str]], x_field: str, y_field: str, aggregation: str) -> tuple[list[str | float], list[float]]:
    if not rows:
        raise ValueError("No rows remain after applying chart filters.")
    for field in (x_field, y_field):
        if field not in rows[0]:
            raise ValueError(f"Chart field is missing from result data: {field}")
    if aggregation == "none":
        return _coerce_axis([row[x_field] for row in rows]), [_number(row[y_field]) for row in rows]
    groups: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        groups[row[x_field]].append(1.0 if aggregation == "count" else _number(row[y_field]))
    x_values: list[str | float] = _coerce_axis(list(groups))
    if x_values and isinstance(x_values[0], float):
        ordered = [key for _, key in sorted(zip(x_values, groups, strict=True))]
        x_values = [float(key) for key in ordered]
    else:
        ordered = sorted(groups)
        x_values = ordered
    if aggregation == "sum":
        y_values = [sum(groups[key]) for key in ordered]
    elif aggregation == "mean":
        y_values = [sum(groups[key]) / len(groups[key]) for key in ordered]
    elif aggregation == "count":
        y_values = [float(len(groups[key])) for key in ordered]
    else:
        raise ValueError(f"Unsupported aggregation: {aggregation}")
    return x_values, y_values


def _plotly_figure(spec: dict[str, Any], x_values: list[str | float], y_values: list[float]) -> go.Figure:
    if spec["type"] == "bar":
        trace = go.Bar(x=x_values, y=y_values)
    elif spec["type"] == "scatter":
        trace = go.Scatter(x=x_values, y=y_values, mode="markers")
    else:
        trace = go.Scatter(x=x_values, y=y_values, mode="lines+markers")
    figure = go.Figure(data=[trace])
    figure.update_layout(title=spec["title"], xaxis_title=spec["x"], yaxis_title=spec["y"], template="plotly_white")
    return figure


def render(run_dir: Path, spec_file: Path) -> dict[str, Any]:
    spec_file = resolve_within(run_dir, spec_file)
    specs = load_json(spec_file)
    validate_schema(specs, SCHEMA_DIR / "chart-specs.schema.json")
    source_path = resolve_within(run_dir, specs["source_file"])
    if not source_path.is_file():
        raise ValueError(f"Chart source file does not exist: {source_path}")
    if sha256_file(source_path) != specs["source_sha256"]:
        raise ValueError("Chart source file hash does not match the approved specification.")
    state = load_state(run_dir)
    if state["status"] == "completed":
        raise ValueError("Completed run artifacts are immutable.")
    if not any(
        item.get("kind") in {"query_result", "user_result"}
        and not item.get("superseded_by")
        and item.get("path") == specs["source_file"]
        and item.get("sha256") == specs["source_sha256"]
        for item in state["artifacts"]
    ):
        raise ValueError("Chart source must be an active registered query_result or user_result artifact.")
    chart_ids = [item["chart_id"] for item in specs["charts"]]
    if len(chart_ids) != len(set(chart_ids)):
        raise ValueError("Chart IDs must be unique.")
    with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    charts_dir = run_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    invalidate_artifacts(
        run_dir,
        {"chart_specs", "chart_manifest", "chart_png", "chart_html"},
        "chart_rerender",
    )
    records = []
    for spec in specs["charts"]:
        record = {"chart_id": spec["chart_id"], "status": spec["status"], "outputs": [], "error": None}
        if spec["status"] == "blocked_by_missing_data":
            records.append(record)
            continue
        generated_paths: list[Path] = []
        try:
            selected = _filtered(rows, spec["filters"])
            x_values, y_values = _series(selected, spec["x"], spec["y"], spec["aggregation"])
            if "png" in spec["output_formats"]:
                output = charts_dir / f"{spec['chart_id']}.png"
                figure, axis = plt.subplots(figsize=(10, 5.625), dpi=120)
                if spec["type"] == "bar":
                    axis.bar(x_values, y_values)
                elif spec["type"] == "scatter":
                    axis.scatter(x_values, y_values)
                else:
                    axis.plot(x_values, y_values, marker="o")
                axis.set_title(spec["title"])
                axis.set_xlabel(spec["x"])
                axis.set_ylabel(spec["y"])
                axis.grid(axis="y", alpha=0.25)
                figure.tight_layout()
                figure.savefig(output, bbox_inches="tight")
                plt.close(figure)
                generated_paths.append(output)
                record["outputs"].append({"format": "png", "path": str(output.relative_to(run_dir)).replace("\\", "/"), "sha256": sha256_file(output)})
            if "html" in spec["output_formats"]:
                output = charts_dir / f"{spec['chart_id']}.html"
                html = _plotly_figure(spec, x_values, y_values).to_html(full_html=True, include_plotlyjs=True)
                atomic_write_text(output, html)
                generated_paths.append(output)
                record["outputs"].append({"format": "html", "path": str(output.relative_to(run_dir)).replace("\\", "/"), "sha256": sha256_file(output)})
            record["status"] = "rendered"
        except Exception as exc:
            for output in generated_paths:
                output.unlink(missing_ok=True)
            record["status"] = "failed"
            record["outputs"] = []
            record["error"] = f"{type(exc).__name__}: {exc}"
        records.append(record)
    manifest = {
        "schema_version": "1.1",
        "spec_path": str(spec_file.relative_to(run_dir.resolve())).replace("\\", "/"),
        "spec_sha256": sha256_file(spec_file),
        "source_file": specs["source_file"],
        "source_sha256": sha256_file(source_path),
        "rendered_at": utc_now(),
        "charts": records,
    }
    validate_schema(manifest, SCHEMA_DIR / "chart-render-manifest.schema.json")
    manifest_path = charts_dir / "render-manifest.json"
    atomic_write_json(manifest_path, manifest)
    registrations = [{"kind": "chart_specs", "path": spec_file}, {"kind": "chart_manifest", "path": manifest_path}]
    for record in records:
        for output in record["outputs"]:
            registrations.append({"kind": f"chart_{output['format']}", "path": output["path"], "metadata": {"chart_id": record["chart_id"]}})
    register_artifacts(run_dir, registrations, event_type="charts_registered")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Render chart specifications from real query results.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--spec-file", type=Path, required=True)
    args = parser.parse_args()
    manifest = render(args.run_dir.resolve(), args.spec_file.resolve())
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] != "failed" for item in manifest["charts"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Render approved chart specifications with bounded, auditable data handling."""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import math
import os
import shutil
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, Inexact, InvalidOperation, Rounded, localcontext
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import plotly.graph_objects as go

from runctl import (
    abort_execution_lease,
    begin_execution_lease,
    load_state,
    publish_execution_artifacts,
)
from runtime_common import (
    SCHEMA_DIR,
    RunLock,
    atomic_copy_file,
    atomic_write_json,
    atomic_write_text,
    load_json,
    resolve_within,
    sha256_file,
    sha256_json,
    utc_now,
    validate_schema,
)


DEFAULT_MAX_POINTS = 5_000
DECIMAL_CONTEXT_PRECISION = 50
RENDERER_VERSION = "1.2"
TEMPORAL_FIELD_TOKENS = ("date", "day", "time", "timestamp", "week", "month", "year")


def _number_decimal(value: str | None) -> Decimal:
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Expected numeric chart value, got {value!r}.") from exc
    if not number.is_finite():
        raise ValueError(f"Expected finite numeric chart value, got {value!r}.")
    return number


def _number(value: str) -> float:
    return float(_number_decimal(value))


def _filtered(rows: list[dict[str, str]], filters: list[dict[str, Any]]) -> list[dict[str, str]]:
    if filters and rows:
        _validate_filter_fields(set(rows[0]), filters)
    return [row for row in rows if _matches_filters(row, filters)]


def _validate_filter_fields(fieldnames: set[str], filters: list[dict[str, Any]]) -> None:
    for condition in filters:
        field = condition.get("field")
        if not isinstance(field, str) or field not in fieldnames:
            raise ValueError(f"Unknown filter field: {field}")
        operator = condition.get("op", "eq")
        if operator not in {"eq", "ne", "in"} or (operator == "in" and not isinstance(condition.get("value"), list)):
            raise ValueError(f"Unsupported filter operator: {operator}")


def _matches_filters(row: dict[str, str], filters: list[dict[str, Any]]) -> bool:
    for condition in filters:
        field = condition["field"]
        operator = condition.get("op", "eq")
        wanted = condition.get("value")
        if operator == "eq" and row.get(field) != str(wanted):
            return False
        if operator == "ne" and row.get(field) == str(wanted):
            return False
        if operator == "in" and row.get(field) not in {str(value) for value in wanted}:
            return False
    return True


def _coerce_axis(values: list[str]) -> list[str | float]:
    try:
        numeric = [float(_number_decimal(value)) for value in values]
    except (OverflowError, ValueError):
        return values
    return numeric if all(math.isfinite(value) for value in numeric) else values


def _series(
    rows: list[dict[str, str]],
    x_field: str,
    y_field: str,
    aggregation: str,
) -> tuple[list[str | float], list[float]]:
    """Compatibility helper for focused unit tests; file rendering uses streaming paths."""
    if not rows:
        raise ValueError("No rows remain after applying chart filters.")
    for field in (x_field, y_field):
        if field not in rows[0]:
            raise ValueError(f"Chart field is missing from result data: {field}")
    if aggregation == "none":
        return _coerce_axis([row[x_field] for row in rows]), [_number(row[y_field]) for row in rows]

    groups: dict[str, tuple[Decimal, int]] = {}
    with localcontext() as context:
        context.prec = DECIMAL_CONTEXT_PRECISION
        for row in rows:
            key = row[x_field]
            total, count = groups.get(key, (Decimal(0), 0))
            value = Decimal(1) if aggregation == "count" else _number_decimal(row[y_field])
            groups[key] = (total + value, count + 1)
        ordered = sorted(groups, key=lambda key: _axis_sort_key(key))
        if aggregation == "sum":
            y_values = [float(groups[key][0]) for key in ordered]
        elif aggregation == "mean":
            y_values = [float(groups[key][0] / groups[key][1]) for key in ordered]
        elif aggregation == "count":
            y_values = [float(groups[key][1]) for key in ordered]
        else:
            raise ValueError(f"Unsupported aggregation: {aggregation}")
    x_values = _coerce_axis(ordered)
    return x_values, y_values


def _hash_rank(seed: str, value: str) -> int:
    digest = hashlib.sha256(f"{seed}\0{value}".encode("utf-8")).digest()
    return int.from_bytes(digest, "big")


def _bottom_k_add(
    heap: list[tuple[int, str]],
    retained: set[str],
    key: str,
    rank: int,
    limit: int,
) -> bool:
    """Keep the lexicographically stable bottom-k (rank, key) pairs."""
    if key in retained:
        return False
    entry = (-rank, key)
    if len(heap) < limit:
        heapq.heappush(heap, entry)
        retained.add(key)
        return False
    worst_rank = -heap[0][0]
    worst_key = heap[0][1]
    if (rank, key) < (worst_rank, worst_key):
        removed = heapq.heapreplace(heap, entry)
        retained.remove(removed[1])
        retained.add(key)
    return True


def _csv_reader(handle: Any, spec: dict[str, Any]) -> csv.DictReader:
    reader = csv.DictReader(handle)
    fieldnames = reader.fieldnames
    if not fieldnames:
        raise ValueError("Chart source CSV has no header row.")
    if any(field is None or field == "" for field in fieldnames) or len(fieldnames) != len(set(fieldnames)):
        raise ValueError("Chart source CSV has blank or duplicate column names.")
    required = {spec["x"], spec["y"]}
    missing = sorted(required - set(fieldnames))
    if missing:
        raise ValueError("Chart field is missing from result data: " + ", ".join(missing))
    _validate_filter_fields(set(fieldnames), spec["filters"])
    return reader


def _read_unaggregated(
    source_path: Path,
    spec: dict[str, Any],
    max_points: int,
    seed: str,
) -> tuple[list[str], list[Decimal], dict[str, Any]]:
    heap: list[tuple[int, int, int, str, Decimal]] = []
    rows_read = 0
    rows_matched = 0
    with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = _csv_reader(handle, spec)
        for row_number, row in enumerate(reader, start=1):
            rows_read += 1
            if None in row:
                raise ValueError(f"Chart source CSV row {row_number} has more fields than its header.")
            if not _matches_filters(row, spec["filters"]):
                continue
            rows_matched += 1
            x_value = row[spec["x"]]
            y_value = _number_decimal(row[spec["y"]])
            rank = _hash_rank(seed, str(row_number))
            entry = (-rank, -row_number, row_number, x_value, y_value)
            if len(heap) < max_points:
                heapq.heappush(heap, entry)
            elif (rank, row_number) < (-heap[0][0], -heap[0][1]):
                heapq.heapreplace(heap, entry)

    retained = sorted(heap, key=lambda item: item[2])
    profile = _base_processing_profile(
        max_points=max_points,
        rows_read=rows_read,
        rows_matched=rows_matched,
        input_points=rows_matched,
        output_points=len(retained),
        passes=1,
        method="streaming_deterministic_hash_sample" if rows_matched > max_points else "streaming_bounded_read",
        sampling_applied=rows_matched > max_points,
        seed=seed,
        aggregation="none",
    )
    return [item[3] for item in retained], [item[4] for item in retained], profile


def _read_aggregated(
    source_path: Path,
    spec: dict[str, Any],
    max_points: int,
    seed: str,
) -> tuple[list[str], list[Decimal], dict[str, Any]]:
    heap: list[tuple[int, str]] = []
    retained: set[str] = set()
    sampled = False
    rows_read = 0
    rows_matched = 0

    with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = _csv_reader(handle, spec)
        for row_number, row in enumerate(reader, start=1):
            rows_read += 1
            if None in row:
                raise ValueError(f"Chart source CSV row {row_number} has more fields than its header.")
            if not _matches_filters(row, spec["filters"]):
                continue
            rows_matched += 1
            if spec["aggregation"] != "count":
                _number_decimal(row[spec["y"]])
            key = row[spec["x"]]
            sampled = _bottom_k_add(heap, retained, key, _hash_rank(seed, key), max_points) or sampled

    groups = {key: [Decimal(0), 0] for key in retained}
    arithmetic_inexact = False
    if retained:
        with localcontext() as context:
            context.prec = DECIMAL_CONTEXT_PRECISION
            with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = _csv_reader(handle, spec)
                for row in reader:
                    if not _matches_filters(row, spec["filters"]):
                        continue
                    key = row[spec["x"]]
                    if key not in groups:
                        continue
                    value = Decimal(1) if spec["aggregation"] == "count" else _number_decimal(row[spec["y"]])
                    groups[key][0] += value
                    groups[key][1] += 1
            y_values: list[Decimal] = []
            x_values = list(retained)
            for key in x_values:
                total, count = groups[key]
                if spec["aggregation"] == "sum":
                    y_values.append(total)
                elif spec["aggregation"] == "mean":
                    y_values.append(total / Decimal(count))
                elif spec["aggregation"] == "count":
                    y_values.append(Decimal(count))
                else:
                    raise ValueError(f"Unsupported aggregation: {spec['aggregation']}")
            arithmetic_inexact = bool(context.flags[Inexact] or context.flags[Rounded])
    else:
        x_values = []
        y_values = []

    method = "streaming_group_hash_sample_then_exact_aggregation" if sampled else "streaming_exact_aggregation"
    profile = _base_processing_profile(
        max_points=max_points,
        rows_read=rows_read,
        rows_matched=rows_matched,
        input_points=None if sampled else len(retained),
        output_points=len(retained),
        passes=2,
        method=method,
        sampling_applied=sampled,
        seed=seed,
        aggregation=spec["aggregation"],
    )
    profile["population_lower_bound"] = max_points + 1 if sampled else len(retained)
    profile["precision"]["arithmetic_inexact"] = arithmetic_inexact
    return x_values, y_values, profile


def _base_processing_profile(
    *,
    max_points: int,
    rows_read: int,
    rows_matched: int,
    input_points: int | None,
    output_points: int,
    passes: int,
    method: str,
    sampling_applied: bool,
    seed: str,
    aggregation: str,
) -> dict[str, Any]:
    return {
        "max_points": max_points,
        "rows_read": rows_read,
        "rows_matched": rows_matched,
        "input_points": input_points,
        "population_lower_bound": input_points,
        "output_points": output_points,
        "passes": passes,
        "method": method,
        "sampling_applied": sampling_applied,
        "sampling_seed_sha256": seed,
        "aggregation": aggregation,
        "axis": None,
        "precision": {
            "arithmetic": "decimal",
            "decimal_context_precision": DECIMAL_CONTEXT_PRECISION,
            "arithmetic_inexact": False,
            "x_render": None,
            "y_render": None,
        },
    }


def _parse_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _axis_sort_key(value: str) -> tuple[int, Any, str]:
    try:
        return (0, _number_decimal(value), value)
    except ValueError:
        pass
    try:
        parsed = _parse_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (1, parsed.astimezone(timezone.utc), value)
    except ValueError:
        return (2, value, value)


def _decimal_render(values: list[Decimal]) -> tuple[list[float], dict[str, Any]]:
    rendered: list[float] = []
    lossy_values = 0
    max_error = Decimal(0)
    for value in values:
        try:
            converted = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"Decimal chart value cannot be rendered as binary64: {value}.") from exc
        if not math.isfinite(converted):
            raise ValueError(f"Decimal chart value exceeds the finite binary64 range: {value}.")
        error = abs(Decimal.from_float(converted) - value)
        if error:
            lossy_values += 1
            max_error = max(max_error, error)
        rendered.append(converted)
    return rendered, {
        "source_representation": "decimal_text",
        "render_representation": "binary64",
        "lossy_values": lossy_values,
        "max_absolute_error": str(max_error),
    }


def _prepare_axis(
    raw_values: list[str],
    *,
    field_name: str,
    sort_values: bool,
) -> tuple[list[str | float], list[int], dict[str, Any], dict[str, Any] | None]:
    if not raw_values:
        return [], [], {
            "kind": "empty",
            "ordering": "none",
            "timezone": None,
            "degradations": [],
        }, None

    try:
        decimal_values = [_number_decimal(value) for value in raw_values]
    except ValueError:
        decimal_values = []
    if decimal_values:
        rendered, precision = _decimal_render(decimal_values)
        order = (
            sorted(range(len(raw_values)), key=lambda index: (decimal_values[index], index))
            if sort_values
            else list(range(len(raw_values)))
        )
        return (
            [rendered[index] for index in order],
            order,
            {
                "kind": "numeric",
                "ordering": "numeric_ascending" if sort_values else "source_order",
                "timezone": None,
                "degradations": ["decimal_to_binary64"] if precision["lossy_values"] else [],
            },
            precision,
        )

    parsed_values: list[datetime] = []
    temporal_failures = 0
    naive_count = 0
    for value in raw_values:
        try:
            parsed = _parse_datetime(value)
        except ValueError:
            temporal_failures += 1
            continue
        if parsed.tzinfo is None:
            naive_count += 1
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed_values.append(parsed.astimezone(timezone.utc))
    if temporal_failures == 0:
        order = sorted(range(len(raw_values)), key=lambda index: (parsed_values[index], index))
        degradations = ["naive_datetime_assumed_utc"] if naive_count else []
        display = [parsed_values[index].isoformat().replace("+00:00", "Z") for index in order]
        return display, order, {
            "kind": "temporal",
            "ordering": "utc_ascending",
            "timezone": "UTC",
            "degradations": degradations,
        }, None

    order = (
        sorted(range(len(raw_values)), key=lambda index: (raw_values[index], index))
        if sort_values
        else list(range(len(raw_values)))
    )
    temporal_hint = any(token in field_name.lower() for token in TEMPORAL_FIELD_TOKENS) or bool(parsed_values)
    degradations = ["temporal_parse_failed_fallback_to_category"] if temporal_hint else []
    return [raw_values[index] for index in order], order, {
        "kind": "categorical",
        "ordering": "lexical_ascending" if sort_values else "source_order",
        "timezone": None,
        "degradations": degradations,
    }, None


def _chart_series(
    source_path: Path,
    spec: dict[str, Any],
    max_points: int,
    source_sha256: str,
) -> tuple[list[str | float], list[float], dict[str, Any]]:
    seed = sha256_json(
        {
            "source_sha256": source_sha256,
            "chart_id": spec["chart_id"],
            "max_points": max_points,
            "method_version": RENDERER_VERSION,
        }
    )
    if spec["aggregation"] == "none":
        raw_x, decimal_y, profile = _read_unaggregated(source_path, spec, max_points, seed)
    else:
        raw_x, decimal_y, profile = _read_aggregated(source_path, spec, max_points, seed)
    if not raw_x:
        profile["axis"] = {
            "kind": "empty",
            "ordering": "none",
            "timezone": None,
            "degradations": [],
        }
        profile["precision"]["y_render"] = _decimal_render([])[1]
        return [], [], profile

    should_sort = spec["aggregation"] != "none" or spec["type"] == "line"
    x_values, order, axis_metadata, x_precision = _prepare_axis(raw_x, field_name=spec["x"], sort_values=should_sort)
    decimal_y = [decimal_y[index] for index in order]
    y_values, y_precision = _decimal_render(decimal_y)
    profile["axis"] = axis_metadata
    profile["precision"]["x_render"] = x_precision
    profile["precision"]["y_render"] = y_precision
    return x_values, y_values, profile


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


def _render_png(output: Path, spec: dict[str, Any], x_values: list[str | float], y_values: list[float]) -> None:
    figure, axis = plt.subplots(figsize=(10, 5.625), dpi=120)
    try:
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
        figure.savefig(output, bbox_inches="tight", metadata={"Software": "multi-agent-data-analysis-skill"})
    finally:
        plt.close(figure)


def _published_relative(run_dir: Path, revision_dir: Path, name: str) -> str:
    return str((revision_dir / name).relative_to(run_dir)).replace("\\", "/")


def _verify_published_manifest(run_dir: Path, manifest_path: Path, render_identity: str) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    validate_schema(manifest, SCHEMA_DIR / "chart-render-manifest-v1.2.schema.json")
    if manifest["render_identity"] != render_identity:
        raise ValueError(f"Chart revision directory collision at {manifest_path.parent}.")
    spec_path = resolve_within(run_dir, manifest["spec_path"])
    if not spec_path.is_file() or sha256_file(spec_path) != manifest["spec_sha256"]:
        raise ValueError("Published chart specification is missing or changed.")
    for chart in manifest["charts"]:
        for output in chart["outputs"]:
            output_path = resolve_within(run_dir, output["path"])
            if not output_path.is_file() or sha256_file(output_path) != output["sha256"]:
                raise ValueError(f"Published chart output is missing or changed: {output['path']}")
    return manifest


def _reconcile_latest_pointer(run_dir: Path, optimistic_pointer: dict[str, Any]) -> None:
    """Publish quickly, then make the pointer agree with the active artifact under the run lock."""
    pointer_path = run_dir / "charts" / "latest.json"
    atomic_write_json(pointer_path, optimistic_pointer)
    with RunLock(run_dir):
        state = load_state(run_dir)
        active = next(
            (
                item
                for item in reversed(state["artifacts"])
                if item.get("kind") == "chart_manifest" and not item.get("superseded_by")
            ),
            None,
        )
        if active is None:
            pointer_path.unlink(missing_ok=True)
            return
        manifest_path = resolve_within(run_dir, active["path"])
        if not manifest_path.is_file() or sha256_file(manifest_path) != active["sha256"]:
            raise ValueError("Active chart render manifest is missing or changed while publishing latest.json.")
        manifest = load_json(manifest_path)
        validate_schema(manifest, SCHEMA_DIR / "chart-render-manifest-v1.2.schema.json")
        pointer = {
            "schema_version": "1.2",
            "pointer_type": "chart_render",
            "revision": manifest["revision"],
            "target_path": active["path"],
            "target_sha256": active["sha256"],
            "content_identity": manifest["render_identity"],
        }
        validate_schema(pointer, SCHEMA_DIR / "chart-lineage-pointer.schema.json")
        atomic_write_json(pointer_path, pointer)


def _render_with_lease(
    run_dir: Path,
    spec_file: Path,
    lease: dict[str, Any],
    *,
    max_points: int = DEFAULT_MAX_POINTS,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    if not isinstance(max_points, int) or isinstance(max_points, bool) or max_points < 1:
        raise ValueError("max_points must be a positive integer.")
    spec_file = resolve_within(run_dir, spec_file)
    specs = load_json(spec_file)
    validate_schema(specs, SCHEMA_DIR / "chart-specs.schema.json")
    source_path = resolve_within(run_dir, specs["source_file"])
    if not source_path.is_file():
        raise ValueError(f"Chart source file does not exist: {source_path}")
    source_sha256 = sha256_file(source_path)
    if source_sha256 != specs["source_sha256"]:
        raise ValueError("Chart source file hash does not match the approved specification.")

    state = load_state(run_dir)
    if state["status"] == "completed":
        raise ValueError("Completed run artifacts are immutable.")
    source_records = [
        item
        for item in state["artifacts"]
        if item.get("kind") in {"query_result", "user_result"}
        and not item.get("superseded_by")
        and item.get("path") == specs["source_file"]
        and item.get("sha256") == specs["source_sha256"]
    ]
    if not source_records:
        raise ValueError("Chart source must be an active registered query_result or user_result artifact.")
    source_record = source_records[-1]
    chart_ids = [item["chart_id"] for item in specs["charts"]]
    if len(chart_ids) != len(set(chart_ids)):
        raise ValueError("Chart IDs must be unique.")

    spec_sha256 = sha256_file(spec_file)
    render_identity = sha256_json(
        {
            "renderer_version": RENDERER_VERSION,
            "state_revision": state["revision"],
            "spec_sha256": spec_sha256,
            "source_artifact_id": source_record["artifact_id"],
            "source_sha256": source_sha256,
            "max_points": max_points,
        }
    )
    revision_name = f"revision-{state['revision']:06d}-{render_identity[:16]}"
    charts_dir = run_dir / "charts"
    revisions_dir = charts_dir / "revisions"
    final_dir = revisions_dir / revision_name
    revisions_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{revision_name}.", suffix=".tmp", dir=charts_dir))
    published = False

    try:
        immutable_spec = staging_dir / "chart-specs.json"
        atomic_copy_file(spec_file, immutable_spec)
        if sha256_file(immutable_spec) != spec_sha256:
            raise ValueError("Chart specification changed while it was being snapshotted.")
        records: list[dict[str, Any]] = []
        for spec in specs["charts"]:
            record: dict[str, Any] = {
                "chart_id": spec["chart_id"],
                "status": spec["status"],
                "outputs": [],
                "error": None,
                "data_processing": None,
            }
            if spec["status"] == "blocked_by_missing_data":
                records.append(record)
                continue
            generated_paths: list[Path] = []
            try:
                x_values, y_values, profile = _chart_series(source_path, spec, max_points, source_sha256)
                record["data_processing"] = profile
                if not x_values:
                    raise ValueError("No rows remain after applying chart filters.")
                if "png" in spec["output_formats"]:
                    output = staging_dir / f"{spec['chart_id']}.png"
                    _render_png(output, spec, x_values, y_values)
                    generated_paths.append(output)
                    record["outputs"].append(
                        {
                            "format": "png",
                            "path": _published_relative(run_dir, final_dir, output.name),
                            "sha256": sha256_file(output),
                        }
                    )
                if "html" in spec["output_formats"]:
                    output = staging_dir / f"{spec['chart_id']}.html"
                    html = _plotly_figure(spec, x_values, y_values).to_html(full_html=True, include_plotlyjs="cdn")
                    atomic_write_text(output, html)
                    generated_paths.append(output)
                    record["outputs"].append(
                        {
                            "format": "html",
                            "path": _published_relative(run_dir, final_dir, output.name),
                            "sha256": sha256_file(output),
                        }
                    )
                record["status"] = "rendered"
            except Exception as exc:
                for output in generated_paths:
                    output.unlink(missing_ok=True)
                record["status"] = "failed"
                record["outputs"] = []
                record["error"] = f"{type(exc).__name__}: {exc}"
            records.append(record)

        if sha256_file(source_path) != source_sha256:
            raise ValueError("Chart source file changed while charts were being rendered.")
        manifest = {
            "schema_version": "1.2",
            "revision": state["revision"],
            "render_identity": render_identity,
            "spec_path": _published_relative(run_dir, final_dir, immutable_spec.name),
            "spec_sha256": spec_sha256,
            "source_file": specs["source_file"],
            "source_sha256": source_sha256,
            "source_artifact": {
                "artifact_id": source_record["artifact_id"],
                "kind": source_record["kind"],
                "path": source_record["path"],
                "sha256": source_record["sha256"],
            },
            "render_policy": {
                "max_points": max_points,
                "unaggregated_method": "streaming_deterministic_hash_sample",
                "aggregated_method": "streaming_group_hash_sample_then_exact_aggregation",
                "decimal_context_precision": DECIMAL_CONTEXT_PRECISION,
                "plotly_js": "cdn",
            },
            "rendered_at": utc_now(),
            "charts": records,
        }
        validate_schema(manifest, SCHEMA_DIR / "chart-render-manifest-v1.2.schema.json")
        staged_manifest = staging_dir / "render-manifest.json"
        atomic_write_json(staged_manifest, manifest)

        if final_dir.exists():
            shutil.rmtree(staging_dir)
            manifest = _verify_published_manifest(run_dir, final_dir / "render-manifest.json", render_identity)
        else:
            os.replace(staging_dir, final_dir)
            published = True
            manifest = _verify_published_manifest(run_dir, final_dir / "render-manifest.json", render_identity)
    finally:
        if not published and staging_dir.exists():
            shutil.rmtree(staging_dir)

    manifest_path = final_dir / "render-manifest.json"
    registrations: list[dict[str, Any]] = [
        {
            "kind": "chart_specs",
            "path": manifest["spec_path"],
            "metadata": {"render_identity": render_identity, "source_artifact_id": source_record["artifact_id"]},
        },
        {
            "kind": "chart_manifest",
            "path": manifest_path,
            "metadata": {"render_identity": render_identity, "source_artifact_id": source_record["artifact_id"]},
        },
    ]
    for record in manifest["charts"]:
        for output in record["outputs"]:
            registrations.append(
                {
                    "kind": f"chart_{output['format']}",
                    "path": output["path"],
                    "metadata": {
                        "chart_id": record["chart_id"],
                        "render_identity": render_identity,
                        "source_artifact_id": source_record["artifact_id"],
                        "source_sha256": source_sha256,
                        "data_processing": record["data_processing"],
                    },
                }
            )
    publish_execution_artifacts(
        run_dir,
        lease["lease_id"],
        registrations,
        invalidate_kinds={"chart_specs", "chart_manifest", "chart_png", "chart_html"},
        event_type="charts_registered",
    )

    pointer = {
        "schema_version": "1.2",
        "pointer_type": "chart_render",
        "revision": state["revision"],
        "target_path": str(manifest_path.relative_to(run_dir)).replace("\\", "/"),
        "target_sha256": sha256_file(manifest_path),
        "content_identity": render_identity,
    }
    validate_schema(pointer, SCHEMA_DIR / "chart-lineage-pointer.schema.json")
    _reconcile_latest_pointer(run_dir, pointer)
    return manifest


def render(run_dir: Path, spec_file: Path, *, max_points: int = DEFAULT_MAX_POINTS) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    spec_file = resolve_within(run_dir, spec_file)
    specs = load_json(spec_file)
    validate_schema(specs, SCHEMA_DIR / "chart-specs.schema.json")
    state = load_state(run_dir)
    source_record = next(
        (
            item
            for item in reversed(state["artifacts"])
            if item.get("kind") in {"query_result", "user_result"}
            and not item.get("superseded_by")
            and item.get("path") == specs.get("source_file")
        ),
        None,
    )
    if source_record is None:
        raise ValueError("Chart source must be an active registered query_result or user_result artifact.")
    if source_record.get("sha256") != specs.get("source_sha256"):
        raise ValueError("Chart source hash does not match the active registered artifact.")
    lease = begin_execution_lease(
        run_dir,
        "charts",
        "chart-render",
        max(1, state["revision"]),
        input_artifact_ids=[source_record["artifact_id"]],
        input_files=[spec_file],
        timeout_seconds=1800,
    )
    try:
        return _render_with_lease(run_dir, spec_file, lease, max_points=max_points)
    except Exception:
        abort_execution_lease(run_dir, lease["lease_id"], "chart_render_failed")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Render chart specifications from real query results.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--spec-file", type=Path, required=True)
    parser.add_argument("--max-points", type=int, default=DEFAULT_MAX_POINTS)
    args = parser.parse_args()
    manifest = render(args.run_dir.resolve(), args.spec_file.resolve(), max_points=args.max_points)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] != "failed" for item in manifest["charts"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())

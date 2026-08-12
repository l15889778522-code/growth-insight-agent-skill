#!/usr/bin/env python3
"""Resolve structured evidence references against immutable run artifacts."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from runtime_common import SCHEMA_DIR, load_json, resolve_within, sha256_file, validate_schema


def _artifact_path(record: dict[str, Any]) -> str:
    path = record.get("path") or record.get("json_path")
    if not isinstance(path, str) or not path:
        raise ValueError(f"Artifact {record.get('artifact_id')!r} has no resolvable file path.")
    return path


def _json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must be empty or start with '/'.")
    current = value
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                index = int(part)
            except ValueError as exc:
                raise ValueError(f"JSON Pointer list index is not an integer: {part!r}.") from exc
            if 0 <= index < len(current):
                current = current[index]
                continue
        raise ValueError(f"JSON Pointer does not resolve at token {part!r}.")
    return current


def _find_calculation(value: Any, calculation_id: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if value.get("calculation_id") == calculation_id:
            return value
        for nested in value.values():
            found = _find_calculation(nested, calculation_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_calculation(nested, calculation_id)
            if found is not None:
                return found
    return None


def artifact_by_id(state: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    matches = [item for item in state.get("artifacts", []) if item.get("artifact_id") == artifact_id]
    if len(matches) != 1:
        raise ValueError(f"Evidence artifact_id must identify exactly one registered artifact: {artifact_id!r}.")
    return matches[0]


def resolve_evidence_reference(
    run_dir: Path,
    state: dict[str, Any],
    reference: dict[str, Any] | str,
    *,
    allow_legacy: bool = False,
) -> Any:
    if isinstance(reference, str):
        if allow_legacy:
            raise ValueError("Legacy string evidence references are read-only and cannot be resolved safely; migrate and rebind them.")
        raise ValueError("v1.2 evidence references must be structured objects.")
    validate_schema(reference, SCHEMA_DIR / "evidence-ref.schema.json")
    record = artifact_by_id(state, reference["artifact_id"])
    if record.get("sha256") != reference["sha256"]:
        raise ValueError(f"Evidence hash does not match artifact {reference['artifact_id']!r}.")
    path = resolve_within(run_dir, _artifact_path(record))
    if not path.is_file() or sha256_file(path) != reference["sha256"]:
        raise ValueError(f"Evidence artifact is missing or changed: {reference['artifact_id']!r}.")

    selector_type = reference["selector_type"]
    selector_value = reference["selector_value"]
    if selector_type == "file":
        return path
    if selector_type in {"json_pointer", "stage_field", "query_manifest_field", "calculation_id"}:
        value = load_json(path)
        if selector_type == "stage_field" and "stage_id" not in record:
            raise ValueError("stage_field selector requires a registered stage artifact.")
        if selector_type == "query_manifest_field" and record.get("kind") != "query_manifest":
            raise ValueError("query_manifest_field selector requires a query_manifest artifact.")
        if selector_type == "calculation_id":
            calculation = _find_calculation(value, selector_value)
            if calculation is None:
                raise ValueError(f"Calculation ID does not resolve: {selector_value!r}.")
            return calculation
        return _json_pointer(value, selector_value)

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            raise ValueError("CSV evidence artifact has no header row.")
        row_number = selector_value if selector_type == "csv_row" else selector_value["row"]
        row = next((item for index, item in enumerate(reader, start=1) if index == row_number), None)
        if row is None:
            raise ValueError(f"CSV evidence row does not exist: {row_number}.")
        if selector_type == "csv_row":
            return dict(zip(header, row, strict=False))
        column = selector_value["column"]
        if isinstance(column, str):
            if column not in header:
                raise ValueError(f"CSV evidence column does not exist: {column!r}.")
            column_index = header.index(column)
        else:
            column_index = column
        if column_index >= len(row):
            raise ValueError(f"CSV evidence column index is outside row {row_number}: {column_index}.")
        return row[column_index]


def format_evidence_reference(reference: Any) -> str:
    if not isinstance(reference, dict):
        return str(reference)
    selector = reference.get("selector_type", "?")
    value = reference.get("selector_value")
    suffix = "" if selector == "file" else f"#{selector}={value}"
    return f"{reference.get('artifact_id', '?')}@{str(reference.get('sha256', ''))[:12]}{suffix}"

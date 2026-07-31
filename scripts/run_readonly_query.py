#!/usr/bin/env python3
"""Execute one hash-approved query and persist auditable result artifacts."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from db_common import data_source_fingerprint, get_adapter, load_config
from runctl import load_state, record_query_result
from runtime_common import SCHEMA_DIR, atomic_write_json, atomic_write_text, load_json, read_jsonl, resolve_within, sha256_file, utc_now, validate_schema
from sql_guard import validate_and_rewrite


def _serializable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _profile(columns: list[dict[str, str]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    profiles = []
    for column in columns:
        name = column["name"]
        raw_values = [row.get(name) for row in rows]
        values = [_serializable(value) for value in raw_values]
        non_null = [value for value in values if value is not None]
        numeric_raw = [value for value in raw_values if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)]
        numeric = [Decimal(str(value)) for value in numeric_raw]
        exact_decimal = any(isinstance(value, Decimal) for value in numeric_raw)
        integer_only = bool(numeric_raw) and all(isinstance(value, int) and not isinstance(value, bool) for value in numeric_raw)
        minimum = min(numeric) if numeric else None
        maximum = max(numeric) if numeric else None
        if minimum is None:
            minimum_value = None
            maximum_value = None
        elif exact_decimal:
            minimum_value = format(minimum, "f")
            maximum_value = format(maximum, "f")
        elif integer_only:
            minimum_value = int(minimum)
            maximum_value = int(maximum)
        else:
            minimum_value = float(minimum)
            maximum_value = float(maximum)
        profiles.append(
            {
                "name": name,
                "type": column["type"],
                "null_count": len(values) - len(non_null),
                "distinct_count": len({json.dumps(value, ensure_ascii=False, sort_keys=True) for value in non_null}),
                "minimum": minimum_value,
                "maximum": maximum_value,
            }
        )
    return {"schema_version": "1.1", "row_count": len(rows), "columns": profiles}


def _matching_approval(run_dir: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    for approval in reversed(read_jsonl(run_dir / "approvals.jsonl")):
        if (
            approval.get("approval_type") == "query"
            and approval.get("subject_id") == request["query_id"]
            and approval.get("subject_revision") == request["revision"]
            and approval.get("subject_sha256") == request["subject_sha256"]
        ):
            query = approval.get("query") or {}
            if (
                query.get("query_sha256") == request["sql_sha256"]
                and query.get("data_source_id") == request["data_source_id"]
                and query.get("data_source_fingerprint") == request["data_source_fingerprint"]
                and query.get("dialect") == request["dialect"]
                and query.get("timeout_seconds") == request["timeout_seconds"]
                and query.get("max_rows") == request["max_rows"]
                and query.get("max_result_bytes") == request["max_result_bytes"]
            ):
                return approval
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute the approved query request in a v1.1 run directory.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--db", choices=["sqlite", "mysql"])
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    request_path = run_dir / "data" / "query-request.json"
    request = load_json(request_path)
    validate_schema(request, SCHEMA_DIR / "query-request.schema.json")
    state = load_state(run_dir)
    approval = _matching_approval(run_dir, request)
    pending = state.get("pending_query") or {}
    if (
        approval is None
        or state.get("status") != "running"
        or not pending.get("approved")
        or pending.get("approval_id") != approval.get("approval_id")
    ):
        print("Query execution blocked: no matching hash-bound query approval.", file=sys.stderr)
        return 2

    sql_path = resolve_within(run_dir, request["sql_path"])
    if sha256_file(sql_path) != request["sql_sha256"]:
        print("Query execution blocked: SQL file hash changed after approval.", file=sys.stderr)
        return 2
    sql = sql_path.read_text(encoding="utf-8")
    validation = validate_and_rewrite(sql, request["dialect"], request["max_rows"])
    if not validation.ok or validation.rewritten:
        print("Query execution blocked: approved SQL no longer matches the safe executable form.", file=sys.stderr)
        for error in validation.errors:
            print(f"- {error}", file=sys.stderr)
        return 2

    config = load_config(args.db)
    if config.data_source_id != request["data_source_id"]:
        print("Query execution blocked: configured data source does not match approval.", file=sys.stderr)
        return 2
    configured_fingerprint = data_source_fingerprint(config)
    if configured_fingerprint != request["data_source_fingerprint"]:
        print("Query execution blocked: physical data source does not match approval.", file=sys.stderr)
        return 2
    if request["dialect"].lower() != config.db_type:
        print("Query execution blocked: approved SQL dialect does not match the configured adapter.", file=sys.stderr)
        return 2
    adapter = get_adapter(config)
    started_at = utc_now()
    try:
        result = adapter.execute_readonly(sql, request["max_rows"], request["timeout_seconds"])
    except Exception as exc:
        print(f"Read-only query failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    completed_at = utc_now()

    serial_rows = [{key: _serializable(value) for key, value in row.items()} for row in result.rows]
    output = io.StringIO(newline="")
    fieldnames = [column["name"] for column in result.columns]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(serial_rows)
    csv_text = output.getvalue()
    result_bytes = len(csv_text.encode("utf-8"))
    if result_bytes > request["max_result_bytes"]:
        print(
            f"Query execution blocked: result size {result_bytes} exceeds approved maximum {request['max_result_bytes']} bytes.",
            file=sys.stderr,
        )
        return 2
    result_path = run_dir / "data" / "result.csv"
    atomic_write_text(result_path, csv_text)

    quality_warnings = list(request.get("warnings", []))
    if result.truncated:
        quality_warnings.append("Result was truncated at the approved max_rows limit.")
    if not serial_rows:
        quality_warnings.append("Query returned no rows.")
    profile_path = run_dir / "data" / "result-profile.json"
    profile = _profile(result.columns, result.rows)
    atomic_write_json(profile_path, profile)
    manifest = {
        "schema_version": "1.1",
        "query_id": request["query_id"],
        "data_source_type": config.db_type,
        "data_source_id": config.data_source_id,
        "data_source_fingerprint": configured_fingerprint,
        "sql_sha256": request["sql_sha256"],
        "dialect": request["dialect"],
        "started_at": started_at,
        "completed_at": completed_at,
        "elapsed_ms": result.elapsed_ms,
        "timeout_seconds": request["timeout_seconds"],
        "max_rows": request["max_rows"],
        "max_result_bytes": request["max_result_bytes"],
        "returned_rows": len(serial_rows),
        "columns": profile["columns"],
        "result_path": "data/result.csv",
        "result_bytes": result_bytes,
        "result_sha256": sha256_file(result_path),
        "profile_path": "data/result-profile.json",
        "profile_sha256": sha256_file(profile_path),
        "truncated": result.truncated,
        "sampled": False,
        "quality_warnings": quality_warnings,
    }
    validate_schema(manifest, SCHEMA_DIR / "query-manifest.schema.json")
    manifest_path = run_dir / "data" / "query-manifest.json"
    atomic_write_json(manifest_path, manifest)
    record_query_result(run_dir, manifest_path, result_path, profile_path)
    print(json.dumps({"manifest": str(manifest_path), "result": str(result_path), "profile": str(profile_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

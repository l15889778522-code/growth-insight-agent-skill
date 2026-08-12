#!/usr/bin/env python3
"""Execute one hash-approved query and persist auditable result artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import io
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

from db_common import QueryStream, get_adapter, load_config, redact_db_error
from runctl import abort_execution_lease, begin_execution_lease, load_state, record_query_result
from runtime_common import SCHEMA_DIR, atomic_write_json, load_json, read_jsonl, resolve_within, sha256_file, utc_now, validate_schema
from sql_guard import validate_and_rewrite


DISTINCT_EXACT_MAX_VALUES = 4096
DISTINCT_EXACT_MAX_UTF8_BYTES = 256 * 1024
DISTINCT_KMV_HASHES = 1024
PROFILE_SAMPLE_MAX_VALUES = 20
PROFILE_SAMPLE_MAX_VALUE_UTF8_BYTES = 1024
PROFILE_SAMPLE_MAX_TOTAL_UTF8_BYTES = 16 * 1024


class ResultSizeLimitExceeded(RuntimeError):
    def __init__(self, attempted_bytes: int, maximum_bytes: int):
        super().__init__(f"result size would be at least {attempted_bytes} bytes; maximum is {maximum_bytes} bytes")
        self.attempted_bytes = attempted_bytes
        self.maximum_bytes = maximum_bytes


@dataclass
class PendingQueryOutput:
    temporary_result_path: Path
    result_bytes: int
    returned_rows: int
    profile: dict[str, Any]
    elapsed_ms: int
    truncated: bool
    data_source_fingerprint: str


def _serializable(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _profile_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, bytes):
        return "binary"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date):
        return "date"
    return "string"


def _type_family(value_type: str) -> str:
    if value_type in {"integer", "number", "decimal"}:
        return "number"
    return value_type


def _canonical_profile_value(value: Any) -> bytes:
    if isinstance(value, Decimal):
        type_name = "decimal"
    elif isinstance(value, bytes):
        type_name = "binary"
    elif isinstance(value, (date, datetime)):
        type_name = type(value).__name__
    else:
        type_name = type(value).__name__
    return json.dumps(
        [type_name, _serializable(value)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class _DistinctTracker:
    """Exact until fixed value/byte caps, then fixed-size KMV estimation."""

    def __init__(self) -> None:
        self._exact_values: set[bytes] = set()
        self._exact_utf8_bytes = 0
        self._approximate = False
        self._kmv_values: set[int] = set()
        self._kmv_max_heap: list[int] = []

    @staticmethod
    def _hash(value: bytes) -> int:
        return int.from_bytes(hashlib.blake2b(value, digest_size=8).digest(), "big")

    def _add_kmv_hash(self, value_hash: int) -> None:
        if value_hash in self._kmv_values:
            return
        if len(self._kmv_values) < DISTINCT_KMV_HASHES:
            self._kmv_values.add(value_hash)
            heapq.heappush(self._kmv_max_heap, -value_hash)
            return
        largest = -self._kmv_max_heap[0]
        if value_hash >= largest:
            return
        removed = -heapq.heapreplace(self._kmv_max_heap, -value_hash)
        self._kmv_values.remove(removed)
        self._kmv_values.add(value_hash)

    def _switch_to_approximate(self) -> None:
        for value in self._exact_values:
            self._add_kmv_hash(self._hash(value))
        self._exact_values.clear()
        self._exact_utf8_bytes = 0
        self._approximate = True

    def observe(self, value: Any) -> None:
        canonical = _canonical_profile_value(value)
        if not self._approximate:
            if canonical in self._exact_values:
                return
            if (
                len(self._exact_values) < DISTINCT_EXACT_MAX_VALUES
                and self._exact_utf8_bytes + len(canonical) <= DISTINCT_EXACT_MAX_UTF8_BYTES
            ):
                self._exact_values.add(canonical)
                self._exact_utf8_bytes += len(canonical)
                return
            self._switch_to_approximate()
        self._add_kmv_hash(self._hash(canonical))

    def finish(self, non_null_count: int) -> tuple[int, dict[str, Any]]:
        common = {
            "max_exact_values": DISTINCT_EXACT_MAX_VALUES,
            "max_exact_utf8_bytes": DISTINCT_EXACT_MAX_UTF8_BYTES,
            "max_approximate_hashes": DISTINCT_KMV_HASHES,
        }
        if not self._approximate:
            return len(self._exact_values), {
                "mode": "exact",
                "algorithm": "bounded_canonical_value_set",
                "retained_values": len(self._exact_values),
                "retained_utf8_bytes": self._exact_utf8_bytes,
                **common,
            }

        retained = len(self._kmv_values)
        if retained < DISTINCT_KMV_HASHES:
            estimate = retained
        else:
            kth_hash = max(self._kmv_values)
            estimate = round((DISTINCT_KMV_HASHES - 1) * (2**64) / (kth_hash + 1))
        estimate = max(retained, min(non_null_count, estimate))
        return estimate, {
            "mode": "approximate",
            "algorithm": "kmv_64",
            "retained_hashes": retained,
            "relative_standard_error": round(1 / math.sqrt(DISTINCT_KMV_HASHES - 2), 6),
            **common,
        }


def _sample_preview(value: Any) -> tuple[Any, int, bool]:
    if isinstance(value, bytes):
        original_size = len(value) * 2
        prefix = value[: PROFILE_SAMPLE_MAX_VALUE_UTF8_BYTES // 2].hex()
        return prefix, original_size, original_size > len(prefix)

    serialized = _serializable(value)
    if not isinstance(serialized, str):
        encoded = json.dumps(serialized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return serialized, len(encoded), False
    encoded = serialized.encode("utf-8")
    if len(encoded) <= PROFILE_SAMPLE_MAX_VALUE_UTF8_BYTES:
        return serialized, len(encoded), False
    preview = encoded[:PROFILE_SAMPLE_MAX_VALUE_UTF8_BYTES].decode("utf-8", errors="ignore")
    return preview, len(encoded), True


class _SampleTracker:
    def __init__(self) -> None:
        self.values: list[dict[str, Any]] = []
        self.total_utf8_bytes = 0
        self.population_count = 0
        self.truncated_value_count = 0
        self.skipped_for_total_bound = 0

    def observe(self, value: Any) -> None:
        self.population_count += 1
        if len(self.values) >= PROFILE_SAMPLE_MAX_VALUES:
            return
        preview, original_size, truncated = _sample_preview(value)
        record = {
            "value": preview,
            "truncated": truncated,
            "original_utf8_bytes": original_size,
        }
        record_size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if self.total_utf8_bytes + record_size > PROFILE_SAMPLE_MAX_TOTAL_UTF8_BYTES:
            self.skipped_for_total_bound += 1
            return
        self.values.append(record)
        self.total_utf8_bytes += record_size
        if truncated:
            self.truncated_value_count += 1

    def metadata(self) -> dict[str, Any]:
        return {
            "mode": "bounded_sample",
            "algorithm": "first_n_non_null",
            "representative": False,
            "population_non_null_count": self.population_count,
            "returned_values": len(self.values),
            "max_values": PROFILE_SAMPLE_MAX_VALUES,
            "max_value_utf8_bytes": PROFILE_SAMPLE_MAX_VALUE_UTF8_BYTES,
            "max_total_utf8_bytes": PROFILE_SAMPLE_MAX_TOTAL_UTF8_BYTES,
            "retained_utf8_bytes": self.total_utf8_bytes,
            "truncated_value_count": self.truncated_value_count,
            "skipped_for_total_bound": self.skipped_for_total_bound,
        }


class _ColumnProfile:
    def __init__(self, column: dict[str, str]):
        self.name = column["name"]
        self.value_type = column.get("type", "null")
        self.observed_types: set[str] = set()
        self.null_count = 0
        self.non_null_count = 0
        self.non_finite_count = 0
        self.empty_string_count = 0
        self.timezone_aware_count = 0
        self.timezone_naive_count = 0
        self.binary_count = 0
        self.distinct = _DistinctTracker()
        self.samples = _SampleTracker()
        self.minimum: Decimal | None = None
        self.maximum: Decimal | None = None
        self.exact_decimal = False
        self.integer_only = True

    def observe(self, value: Any) -> None:
        if value is None:
            self.null_count += 1
            return
        self.non_null_count += 1
        observed_type = _profile_value_type(value)
        self.observed_types.add(observed_type)
        if self.value_type == "null":
            self.value_type = observed_type
        if value == "":
            self.empty_string_count += 1
        if isinstance(value, bytes):
            self.binary_count += 1
        if isinstance(value, datetime):
            if value.tzinfo is not None and value.utcoffset() is not None:
                self.timezone_aware_count += 1
            else:
                self.timezone_naive_count += 1
        if isinstance(value, float) and not math.isfinite(value):
            self.non_finite_count += 1
        elif isinstance(value, Decimal) and not value.is_finite():
            self.non_finite_count += 1
        self.distinct.observe(value)
        self.samples.observe(value)

        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            numeric = Decimal(str(value))
            if numeric.is_finite():
                self.minimum = numeric if self.minimum is None else min(self.minimum, numeric)
                self.maximum = numeric if self.maximum is None else max(self.maximum, numeric)
            self.exact_decimal = self.exact_decimal or isinstance(value, Decimal)
            self.integer_only = self.integer_only and isinstance(value, int)

    def _numeric_bounds(self) -> tuple[Any, Any]:
        if self.minimum is None:
            return None, None
        if self.exact_decimal:
            return format(self.minimum, "f"), format(self.maximum, "f")
        if self.integer_only:
            return int(self.minimum), int(self.maximum)
        return float(self.minimum), float(self.maximum)

    def finish(self) -> dict[str, Any]:
        minimum, maximum = self._numeric_bounds()
        distinct_count, distinct_metadata = self.distinct.finish(self.non_null_count)
        observed_types = sorted(self.observed_types)
        return {
            "name": self.name,
            "type": self.value_type,
            "observed_types": observed_types,
            "type_conflict": len({_type_family(value_type) for value_type in observed_types}) > 1,
            "null_count": self.null_count,
            "non_null_count": self.non_null_count,
            "non_finite_count": self.non_finite_count,
            "empty_string_count": self.empty_string_count,
            "timezone_aware_count": self.timezone_aware_count,
            "timezone_naive_count": self.timezone_naive_count,
            "binary_count": self.binary_count,
            "distinct_count": distinct_count,
            "minimum": minimum,
            "maximum": maximum,
            "distinct_count_metadata": distinct_metadata,
            "samples": self.samples.values,
            "sample_metadata": self.samples.metadata(),
        }


class _ProfileBuilder:
    def __init__(self, columns: list[dict[str, str]]):
        self.columns = [_ColumnProfile(column) for column in columns]

    def observe(self, row: dict[str, Any]) -> None:
        for column in self.columns:
            column.observe(row.get(column.name))

    def finish(self, row_count: int) -> dict[str, Any]:
        return {
            "schema_version": "1.2",
            "row_count": row_count,
            "profiling": {
                "memory_model": "bounded_per_column",
                "distinct_exact_max_values": DISTINCT_EXACT_MAX_VALUES,
                "distinct_exact_max_utf8_bytes": DISTINCT_EXACT_MAX_UTF8_BYTES,
                "distinct_approximate_max_hashes": DISTINCT_KMV_HASHES,
                "sample_max_values": PROFILE_SAMPLE_MAX_VALUES,
                "sample_max_value_utf8_bytes": PROFILE_SAMPLE_MAX_VALUE_UTF8_BYTES,
                "sample_max_total_utf8_bytes": PROFILE_SAMPLE_MAX_TOTAL_UTF8_BYTES,
            },
            "columns": [column.finish() for column in self.columns],
        }


def _profile(columns: list[dict[str, str]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compatibility helper that uses the same bounded incremental profiler."""

    builder = _ProfileBuilder(columns)
    for row in rows:
        builder.observe(row)
    return builder.finish(len(rows))


def _csv_record_bytes(values: list[Any]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([_serializable(value) for value in values])
    return output.getvalue().encode("utf-8")


def _write_checked(handle: BinaryIO, payload: bytes) -> None:
    written = handle.write(payload)
    if written != len(payload):
        raise OSError(f"Short CSV write: expected {len(payload)} bytes, wrote {written!r}.")


def _append_with_limit(handle: BinaryIO, payload: bytes, current_bytes: int, maximum_bytes: int) -> int:
    attempted = current_bytes + len(payload)
    if attempted > maximum_bytes:
        raise ResultSizeLimitExceeded(attempted, maximum_bytes)
    _write_checked(handle, payload)
    return attempted


def _write_stream_to_temporary_csv(
    stream: QueryStream,
    result_path: Path,
    maximum_bytes: int,
) -> tuple[Path, int, int, dict[str, Any]]:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{result_path.name}.", suffix=".tmp", dir=result_path.parent)
    temp_path = Path(temp_name)
    fieldnames = [column["name"] for column in stream.columns]
    profiler = _ProfileBuilder(stream.columns)
    result_bytes = 0
    returned_rows = 0
    try:
        handle = os.fdopen(fd, "wb")
        fd = -1
        with handle:
            result_bytes = _append_with_limit(
                handle,
                _csv_record_bytes(fieldnames),
                result_bytes,
                maximum_bytes,
            )
            for batch in stream.batches:
                for row in batch:
                    payload = _csv_record_bytes([row.get(name) for name in fieldnames])
                    result_bytes = _append_with_limit(handle, payload, result_bytes, maximum_bytes)
                    profiler.observe(row)
                    returned_rows += 1
            handle.flush()
            os.fsync(handle.fileno())
        return temp_path, result_bytes, returned_rows, profiler.finish(returned_rows)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)
        raise


def _execute_to_temporary_result(
    adapter: Any,
    sql: str,
    *,
    max_rows: int,
    timeout_seconds: int,
    max_result_bytes: int,
    expected_data_source_fingerprint: str,
    result_path: Path,
) -> PendingQueryOutput:
    with adapter.stream_readonly(
        sql,
        max_rows,
        timeout_seconds,
        expected_data_source_fingerprint=expected_data_source_fingerprint,
    ) as stream:
        temp_path, result_bytes, returned_rows, profile = _write_stream_to_temporary_csv(
            stream,
            result_path,
            max_result_bytes,
        )
    if not stream.data_source_fingerprint:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("Database adapter did not bind the execution connection to a data-source fingerprint.")
    return PendingQueryOutput(
        temporary_result_path=temp_path,
        result_bytes=result_bytes,
        returned_rows=returned_rows,
        profile=profile,
        elapsed_ms=stream.elapsed_ms,
        truncated=stream.truncated,
        data_source_fingerprint=stream.data_source_fingerprint,
    )


def _manifest_columns(profile: dict[str, Any]) -> list[dict[str, Any]]:
    keys = ("name", "type", "null_count", "distinct_count", "minimum", "maximum")
    return [{key: column[key] for key in keys} for column in profile["columns"]]


def _query_quality_warnings(request_warnings: list[str], output: PendingQueryOutput) -> list[str]:
    warnings = list(request_warnings)
    if output.truncated:
        warnings.append("Result was truncated at the approved max_rows limit.")
    if output.returned_rows == 0:
        warnings.append("Query returned no rows.")
    elif output.returned_rows < 30:
        warnings.append("Query returned fewer than 30 rows; Review must assess sample-size risk.")
    high_null_columns = sorted(
        column["name"]
        for column in output.profile["columns"]
        if column.get("null_count", 0) / max(1, output.returned_rows) >= 0.2
    )
    if high_null_columns:
        warnings.append("Result columns with null rate at or above 20%: " + ", ".join(high_null_columns) + ".")
    if any(column["distinct_count_metadata"]["mode"] == "approximate" for column in output.profile["columns"]):
        warnings.append("One or more profile distinct counts use bounded-memory approximation.")
    if any(column.get("type_conflict") for column in output.profile["columns"]):
        warnings.append("One or more result columns contain mixed incompatible value types.")
    if any(column.get("non_finite_count", 0) for column in output.profile["columns"]):
        warnings.append("One or more numeric columns contain NaN or infinite values.")
    if any(column.get("empty_string_count", 0) for column in output.profile["columns"]):
        warnings.append("One or more string columns contain empty strings distinct from null values.")
    if any(
        column.get("timezone_aware_count", 0) and column.get("timezone_naive_count", 0)
        for column in output.profile["columns"]
    ):
        warnings.append("One or more datetime columns mix timezone-aware and timezone-naive values.")
    return list(dict.fromkeys(warnings))


def _abort_lease_quietly(run_dir: Path, lease: dict[str, Any] | None, reason: str) -> None:
    if not lease:
        return
    try:
        abort_execution_lease(run_dir, lease["lease_id"], reason)
    except Exception:
        pass


def _query_input_artifact_ids(state: dict[str, Any], request: dict[str, Any]) -> list[str]:
    by_kind: dict[str, str] = {}
    for artifact in state.get("artifacts", []):
        metadata = artifact.get("metadata") or {}
        kind = artifact.get("kind")
        if (
            kind in {"query_request", "query_sql"}
            and metadata.get("query_id") == request["query_id"]
            and metadata.get("query_revision") == request["revision"]
            and not artifact.get("superseded_by")
        ):
            if kind in by_kind:
                raise ValueError(f"Multiple active {kind} artifacts match the current query revision.")
            by_kind[kind] = artifact["artifact_id"]
    missing = sorted({"query_request", "query_sql"} - by_kind.keys())
    if missing:
        raise ValueError("Current query revision is missing input artifacts: " + ", ".join(missing))
    return [by_kind["query_request"], by_kind["query_sql"]]


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
    parser = argparse.ArgumentParser(description="Execute the approved query request in a v1.2 run directory.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--db", choices=["sqlite", "mysql"])
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    state = load_state(run_dir)
    pending = state.get("pending_query") or {}
    if not pending.get("request_path"):
        print("Query execution blocked: pending query has no versioned request path.", file=sys.stderr)
        return 2
    request_path = resolve_within(run_dir, pending["request_path"])
    if pending.get("request_sha256") and (
        not request_path.is_file() or sha256_file(request_path) != pending["request_sha256"]
    ):
        print("Query execution blocked: versioned query request is missing or changed.", file=sys.stderr)
        return 2
    try:
        request = load_json(request_path)
        validate_schema(request, SCHEMA_DIR / "query-request.schema.json")
    except Exception as exc:
        print(f"Query execution blocked: invalid query request: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    approval = _matching_approval(run_dir, request)
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

    config = None
    try:
        config = load_config(args.db)
        if config.data_source_id != request["data_source_id"]:
            print("Query execution blocked: configured data source does not match approval.", file=sys.stderr)
            return 2
        if request["dialect"].lower() != config.db_type:
            print("Query execution blocked: approved SQL dialect does not match the configured adapter.", file=sys.stderr)
            return 2
        adapter = get_adapter(config)
    except Exception as exc:
        print(f"Database configuration failed: {type(exc).__name__}: {redact_db_error(exc, config)}", file=sys.stderr)
        return 2

    lease = None
    try:
        input_artifact_ids = _query_input_artifact_ids(state, request)
        lease = begin_execution_lease(
            run_dir,
            "query",
            request["query_id"],
            request["revision"],
            input_artifact_ids=input_artifact_ids,
            timeout_seconds=min(86400, max(300, request["timeout_seconds"] + 60)),
        )
        staging_dir = resolve_within(run_dir, lease["staging_path"])
    except Exception as exc:
        print(f"Query execution lease failed: {type(exc).__name__}: {redact_db_error(exc, config)}", file=sys.stderr)
        return 2

    result_path = staging_dir / "result.csv"
    started_at = utc_now()
    try:
        output = _execute_to_temporary_result(
            adapter,
            sql,
            max_rows=request["max_rows"],
            timeout_seconds=request["timeout_seconds"],
            max_result_bytes=request["max_result_bytes"],
            expected_data_source_fingerprint=request["data_source_fingerprint"],
            result_path=result_path,
        )
    except ResultSizeLimitExceeded as exc:
        _abort_lease_quietly(run_dir, lease, "result_size_limit_exceeded")
        print(
            f"Query execution blocked: result size {exc.attempted_bytes} exceeds approved maximum "
            f"{exc.maximum_bytes} bytes.",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        _abort_lease_quietly(run_dir, lease, f"database_execution_failed:{type(exc).__name__}")
        print(f"Read-only query failed: {type(exc).__name__}: {redact_db_error(exc, config)}", file=sys.stderr)
        return 2
    completed_at = utc_now()

    quality_warnings = _query_quality_warnings(request.get("warnings", []), output)

    profile_path = staging_dir / "result-profile.json"
    manifest_path = staging_dir / "query-manifest.json"
    final_dir = resolve_within(run_dir, lease["output_path"])
    final_result_path = final_dir / "result.csv"
    final_profile_path = final_dir / "result-profile.json"
    final_manifest_path = final_dir / "query-manifest.json"
    try:
        validate_schema(output.profile, SCHEMA_DIR / "result-profile.schema.json")
        atomic_write_json(profile_path, output.profile)
        os.replace(output.temporary_result_path, result_path)
        manifest = {
            "schema_version": "1.2",
            "query_id": request["query_id"],
            "data_source_type": config.db_type,
            "data_source_id": config.data_source_id,
            "data_source_fingerprint": output.data_source_fingerprint,
            "sql_sha256": request["sql_sha256"],
            "dialect": request["dialect"],
            "started_at": started_at,
            "completed_at": completed_at,
            "elapsed_ms": output.elapsed_ms,
            "timeout_seconds": request["timeout_seconds"],
            "max_rows": request["max_rows"],
            "max_result_bytes": request["max_result_bytes"],
            "returned_rows": output.returned_rows,
            "columns": _manifest_columns(output.profile),
            "result_path": str(final_result_path.relative_to(run_dir)).replace("\\", "/"),
            "result_bytes": output.result_bytes,
            "result_sha256": sha256_file(result_path),
            "profile_path": str(final_profile_path.relative_to(run_dir)).replace("\\", "/"),
            "profile_sha256": sha256_file(profile_path),
            "truncated": output.truncated,
            "sampled": False,
            "quality_warnings": quality_warnings,
        }
        validate_schema(manifest, SCHEMA_DIR / "query-manifest.schema.json")
        atomic_write_json(manifest_path, manifest)
        record_query_result(run_dir, manifest_path, result_path, profile_path, lease_id=lease["lease_id"])
    except Exception as exc:
        _abort_lease_quietly(run_dir, lease, f"artifact_publication_failed:{type(exc).__name__}")
        print(f"Query artifact publication failed: {type(exc).__name__}: {redact_db_error(exc, config)}", file=sys.stderr)
        return 2
    finally:
        output.temporary_result_path.unlink(missing_ok=True)

    print(
        json.dumps(
            {"manifest": str(final_manifest_path), "result": str(final_result_path), "profile": str(final_profile_path)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

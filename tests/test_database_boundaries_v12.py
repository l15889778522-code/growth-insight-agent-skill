from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from db_common import (
    MYSQL_IDENTITY_SQL,
    DbConfig,
    MySQLAdapter,
    QueryStream,
    _iter_cursor_batches,
    data_source_descriptor,
)
from run_readonly_query import _profile, _write_stream_to_temporary_csv
from runtime_common import SCHEMA_DIR, sha256_json, validate_schema


def test_one_row_at_exact_result_byte_limit_is_accepted(tmp_path: Path) -> None:
    expected = b"value\nx\n"
    stream = QueryStream(
        columns=[{"name": "value", "type": "string"}],
        batches=iter([[{"value": "x"}]]),
        data_source_fingerprint="a" * 64,
    )

    temporary_path, result_bytes, returned_rows, profile = _write_stream_to_temporary_csv(
        stream,
        tmp_path / "result.csv",
        maximum_bytes=len(expected),
    )
    try:
        assert temporary_path.read_bytes() == expected
        assert result_bytes == len(expected)
        assert returned_rows == 1
        assert profile["row_count"] == 1
    finally:
        temporary_path.unlink(missing_ok=True)


class _NeverFetchCursor:
    def __init__(self) -> None:
        self.fetch_calls = 0

    def fetchmany(self, _count: int):
        self.fetch_calls += 1
        raise AssertionError("an expired deadline must cancel before fetching")


def test_expired_deadline_cancels_before_fetch() -> None:
    cursor = _NeverFetchCursor()
    stream = QueryStream(columns=[{"name": "value", "type": "integer"}], batches=iter(()))
    stream.batches = _iter_cursor_batches(
        cursor,
        stream,
        max_rows=1,
        batch_size=1,
        deadline=0.0,
        row_factory=dict,
    )

    with pytest.raises(TimeoutError, match="approved timeout"):
        list(stream.iter_rows())

    assert cursor.fetch_calls == 0
    assert stream.exhausted is False


class _TimeoutCursor:
    def __init__(self, connection: "_TimeoutConnection") -> None:
        self.connection = connection
        self.description = [("value",)]
        self.rows: list[dict[str, object]] = []
        self.user_query = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self) -> None:
        self.closed = True

    def execute(self, sql: str, params=None) -> None:
        self.connection.statements.append((sql, params))
        if sql == MYSQL_IDENTITY_SQL:
            self.rows = [dict(self.connection.identity)]
            self.description = [
                ("server_uuid",),
                ("server_version",),
                ("account_identifier",),
            ]
        elif sql.startswith("SELECT 1 AS value"):
            self.user_query = True

    def fetchmany(self, count: int):
        if self.user_query:
            raise TimeoutError("simulated slow fetch cancellation")
        rows = self.rows[:count]
        self.rows = self.rows[count:]
        return rows


class _TimeoutConnection:
    def __init__(self) -> None:
        self.identity = {
            "server_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "server_version": "8.4.2",
            "account_identifier": "readonly@%",
        }
        self._read_timeout = None
        self._write_timeout = None
        self._sock = None
        self.ctx = None
        self.statements: list[tuple[str, object]] = []
        self.cursors: list[_TimeoutCursor] = []
        self.closed = False

    def cursor(self) -> _TimeoutCursor:
        cursor = _TimeoutCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


def _mysql_config() -> DbConfig:
    return DbConfig(
        db_type="mysql",
        data_source_id="mysql-boundary-test",
        mysql_host="db.example.test",
        mysql_user="readonly",
        mysql_password="test-only-secret",
        mysql_database="analytics",
    )


def test_mysql_fetch_timeout_cancels_stream_and_closes_resources(monkeypatch) -> None:
    config = _mysql_config()
    approved_connection = _TimeoutConnection()
    expected_fingerprint = sha256_json(data_source_descriptor(config, connection=approved_connection))
    execution_connection = _TimeoutConnection()

    import pymysql

    monkeypatch.setattr(pymysql, "connect", lambda **_kwargs: execution_connection)
    adapter = MySQLAdapter(config)

    with pytest.raises(TimeoutError, match="slow fetch cancellation"):
        with adapter.stream_readonly(
            "SELECT 1 AS value LIMIT 2",
            max_rows=1,
            timeout_seconds=5,
            expected_data_source_fingerprint=expected_fingerprint,
        ) as stream:
            list(stream.iter_rows())

    assert execution_connection.closed is True
    assert execution_connection.cursors
    assert all(cursor.closed for cursor in execution_connection.cursors)


def test_profile_normalizes_mixed_values_and_records_type_metadata() -> None:
    values = [
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("9007199254740993.0100"),
        date(2026, 8, 11),
        datetime(2026, 8, 11, 12, 34, 56, tzinfo=timezone.utc),
        b"\x00\xff",
        "",
    ]
    profile = _profile(
        [{"name": "mixed_value", "type": "null"}],
        [{"mixed_value": value} for value in values],
    )
    column = profile["columns"][0]

    assert profile["row_count"] == len(values)
    assert column["type"] == "number"
    assert column["observed_types"] == ["binary", "date", "datetime", "decimal", "number", "string"]
    assert column["type_conflict"] is True
    assert column["non_finite_count"] == 3
    assert column["empty_string_count"] == 1
    assert column["timezone_aware_count"] == 1
    assert column["timezone_naive_count"] == 0
    assert column["binary_count"] == 1
    assert column["distinct_count"] == len(values)
    assert column["minimum"] == "9007199254740993.0100"
    assert column["maximum"] == "9007199254740993.0100"
    assert [sample["value"] for sample in column["samples"]] == [
        "NaN",
        "Infinity",
        "-Infinity",
        "9007199254740993.0100",
        "2026-08-11",
        "2026-08-11T12:34:56+00:00",
        "00ff",
        "",
    ]
    assert column["samples"][-1]["original_utf8_bytes"] == 0
    json.dumps(profile, ensure_ascii=False, allow_nan=False)
    validate_schema(profile, SCHEMA_DIR / "result-profile.schema.json")

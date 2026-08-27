from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import ssl
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from build_lineage import build
from conftest import ROOT, approve_pending, materialize_stage, stage_output
from db_common import (
    MYSQL_IDENTITY_SQL,
    DbConfig,
    MySQLAdapter,
    SQLiteAdapter,
    data_source_descriptor,
    data_source_fingerprint,
    load_config,
    redact_db_error,
)
from runctl import load_state, prepare_query, start_stage
import run_readonly_query as query_runner
from run_readonly_query import (
    PendingQueryOutput,
    _csv_record_bytes,
    _execute_to_temporary_result,
    _profile,
    _query_quality_warnings,
)
from runtime_common import sha256_json
from sql_guard import validate_and_rewrite


MYSQL_INTEGRATION_SQL = (
    "SELECT CAST('9007199254740993.01' AS DECIMAL(20,2)) AS exact_decimal, "
    "'数据库-🙂' AS unicode_text"
)


def _create_values_database(path: Path, values: list[tuple[str, float]]) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE sales(day TEXT NOT NULL, revenue REAL)")
        connection.executemany("INSERT INTO sales(day, revenue) VALUES (?, ?)", values)
        connection.commit()
    finally:
        connection.close()
    return path


def _run_stage(run_dir: Path, role: str, stage_id: str, **kwargs) -> None:
    attempt_dir = start_stage(run_dir, stage_id)
    output = stage_output(role, "test-run", stage_id, int(attempt_dir.name.removeprefix("attempt-")), **kwargs)
    materialize_stage(run_dir, output)


def _prepare_approved_query(
    approved_run,
    tmp_path: Path,
    database: Path,
    sql: str,
    *,
    max_rows: int,
    max_result_bytes: int,
) -> Path:
    run_dir, _ = approved_run(["growth-metrics", "growth-sql", "growth-insight", "growth-review"])
    _run_stage(run_dir, "growth-metrics", "s01-metrics")
    approve_pending(run_dir, "stage", "db-v12-metrics")
    _run_stage(run_dir, "growth-sql", "s02-sql", sql=sql)
    approve_pending(run_dir, "stage", "db-v12-sql")
    sql_path = tmp_path / "proposed-v12.sql"
    sql_path.write_text(sql, encoding="utf-8")
    config = DbConfig(db_type="sqlite", data_source_id="sqlite-v12", sqlite_path=str(database))
    prepare_query(
        run_dir,
        sql_path,
        "q-revenue",
        "sqlite-v12",
        "sqlite",
        10,
        max_rows,
        max_result_bytes,
        data_source_fingerprint(config),
    )
    approve_pending(run_dir, "query", "db-v12-query")
    return run_dir


def _execute_sqlite(run_dir: Path, database: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "DB_TYPE": "sqlite",
            "DB_SOURCE_ID": "sqlite-v12",
            "SQLITE_PATH": str(database),
        }
    )
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_readonly_query.py"), "--run-dir", str(run_dir), "--db", "sqlite"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _artifact_path(run_dir: Path, kind: str) -> Path:
    active = [
        artifact
        for artifact in load_state(run_dir)["artifacts"]
        if artifact.get("kind") == kind and not artifact.get("superseded_by")
    ]
    assert active
    return run_dir / active[-1]["path"]


def test_sqlite_streams_fixed_size_batches_and_row_sentinel(tmp_path: Path) -> None:
    database = _create_values_database(
        tmp_path / "batches.db",
        [(f"2026-01-{index:02d}", float(index)) for index in range(1, 9)],
    )
    adapter = SQLiteAdapter(DbConfig(db_type="sqlite", data_source_id="batches", sqlite_path=str(database)))
    prepared = validate_and_rewrite("SELECT day, revenue FROM sales ORDER BY day", "sqlite", max_rows=5)
    assert prepared.ok
    with adapter.stream_readonly(prepared.sql, 5, 5, batch_size=2) as stream:
        batches = list(stream.batches)
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert stream.truncated is True
    assert stream.exhausted is True


def test_unicode_bytes_and_row_limit_are_exact_in_published_csv(approved_run, tmp_path: Path) -> None:
    database = _create_values_database(
        tmp_path / "unicode.db",
        [("北京🙂", 1.0), ("上海数据", 2.0), ("深圳", 3.0), ("杭州", 4.0)],
    )
    sql = "SELECT day, SUM(revenue) AS revenue_total FROM sales GROUP BY day ORDER BY day"
    run_dir = _prepare_approved_query(
        approved_run,
        tmp_path,
        database,
        sql,
        max_rows=2,
        max_result_bytes=4096,
    )
    executed = _execute_sqlite(run_dir, database)
    assert executed.returncode == 0, executed.stderr

    result_path = _artifact_path(run_dir, "query_result")
    manifest = json.loads(_artifact_path(run_dir, "query_manifest").read_text(encoding="utf-8"))
    profile = json.loads(_artifact_path(run_dir, "result_profile").read_text(encoding="utf-8"))
    raw = result_path.read_bytes()
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8"), newline="")))
    rebuilt = io.StringIO(newline="")
    writer = csv.writer(rebuilt, lineterminator="\n")
    writer.writerows(rows)

    assert manifest["returned_rows"] == 2
    assert manifest["query_revision"] == 1
    assert manifest["metric_ids"] == ["revenue_total"]
    assert manifest["sql_canonicalization"] == "sqlglot-pretty-v1"
    assert len(manifest["source_sql_sha256"]) == 64
    assert manifest["truncated"] is True
    assert manifest["result_bytes"] == len(raw) == len(rebuilt.getvalue().encode("utf-8"))
    assert any(character.encode("utf-8") in raw for character in ("北", "上", "杭", "深"))
    assert profile["profiling"]["memory_model"] == "bounded_per_column"
    assert profile["columns"][0]["sample_metadata"]["max_values"] == 20
    state = load_state(run_dir)
    lease = state["execution_leases"][-1]
    bound_artifact_ids = {
        key.removeprefix("artifact:")
        for key in lease["input_hashes"]
        if key.startswith("artifact:")
    }
    bound_kinds = {item["kind"] for item in state["artifacts"] if item["artifact_id"] in bound_artifact_ids}
    assert lease["state"] == "completed"
    assert bound_kinds == {"query_request", "query_sql"}


def test_lineage_rejects_a_prepared_query_without_published_result(approved_run, tmp_path: Path) -> None:
    database = _create_values_database(tmp_path / "missing-result.db", [("2026-01-01", 1.0)])
    run_dir = _prepare_approved_query(
        approved_run,
        tmp_path,
        database,
        "SELECT day, SUM(revenue) AS revenue_total FROM sales GROUP BY day",
        max_rows=10,
        max_result_bytes=4096,
    )
    with pytest.raises(ValueError, match="requires exactly one query manifest"):
        build(run_dir)


def test_oversized_single_row_aborts_lease_without_publication(approved_run, tmp_path: Path) -> None:
    huge_value = "界🙂" * (1024 * 512)
    database = _create_values_database(tmp_path / "huge-row.db", [(huge_value, 1.0)])
    sql = "SELECT day, revenue AS revenue_total FROM sales"
    run_dir = _prepare_approved_query(
        approved_run,
        tmp_path,
        database,
        sql,
        max_rows=10,
        max_result_bytes=1024,
    )
    executed = _execute_sqlite(run_dir, database)
    assert executed.returncode == 2
    assert "exceeds approved maximum" in executed.stderr

    state = load_state(run_dir)
    assert state["pending_query"]["approved"] is True
    assert state["execution_leases"][-1]["state"] == "aborted"
    assert not any(item.get("kind") == "query_result" for item in state["artifacts"])
    assert not (run_dir / "data" / "queries" / "q-revenue" / "revision-1" / "result").exists()
    assert not list((run_dir / "data").rglob(".result.csv.*.tmp"))


def test_profile_distinct_and_samples_have_explicit_memory_bounds() -> None:
    rows = [{"dimension": f"value-{index}"} for index in range(5000)]
    rows[0]["dimension"] = "数" * 5000
    profile = _profile([{"name": "dimension", "type": "string"}], rows)
    column = profile["columns"][0]

    assert profile["row_count"] == 5000
    assert column["distinct_count_metadata"]["mode"] == "approximate"
    assert column["distinct_count_metadata"]["algorithm"] == "kmv_64"
    assert column["distinct_count_metadata"]["retained_hashes"] <= 1024
    assert len(column["samples"]) <= 20
    assert column["sample_metadata"]["retained_utf8_bytes"] <= 16 * 1024
    assert column["sample_metadata"]["truncated_value_count"] == 1

    exact = _profile([{"name": "dimension", "type": "string"}], [{"dimension": "a"}, {"dimension": "b"}])
    assert exact["columns"][0]["distinct_count"] == 2
    assert exact["columns"][0]["distinct_count_metadata"]["mode"] == "exact"


def test_profile_records_mixed_types_non_finite_values_and_timezone_state() -> None:
    rows = [
        {"value": 1, "timestamp": datetime(2026, 1, 1)},
        {"value": Decimal("1.5"), "timestamp": datetime(2026, 1, 2, tzinfo=timezone.utc)},
        {"value": float("nan"), "timestamp": None},
        {"value": float("inf"), "timestamp": None},
        {"value": "", "timestamp": None},
        {"value": date(2026, 1, 3), "timestamp": None},
        {"value": b"\x00\xff", "timestamp": None},
    ]
    profile = _profile(
        [{"name": "value", "type": "null"}, {"name": "timestamp", "type": "datetime"}],
        rows,
    )
    value = profile["columns"][0]
    timestamp = profile["columns"][1]

    assert value["observed_types"] == ["binary", "date", "decimal", "integer", "number", "string"]
    assert value["type_conflict"] is True
    assert value["non_finite_count"] == 2
    assert value["empty_string_count"] == 1
    assert value["binary_count"] == 1
    assert timestamp["observed_types"] == ["datetime"]
    assert timestamp["type_conflict"] is False
    assert timestamp["timezone_aware_count"] == 1
    assert timestamp["timezone_naive_count"] == 1
    json.dumps(profile, allow_nan=False)

    csv_record = _csv_record_bytes([float("nan"), float("inf"), float("-inf")]).decode("utf-8")
    assert csv_record == "NaN,Infinity,-Infinity\n"


def test_quality_warnings_include_small_samples_and_high_null_columns(tmp_path: Path) -> None:
    profile = _profile(
        [{"name": "metric_value", "type": "number"}],
        [
            {"metric_value": None},
            {"metric_value": None},
            {"metric_value": 1.0},
            {"metric_value": 2.0},
            {"metric_value": 3.0},
        ],
    )
    output = PendingQueryOutput(
        temporary_result_path=tmp_path / "unused.csv",
        result_bytes=0,
        returned_rows=5,
        profile=profile,
        elapsed_ms=0,
        truncated=False,
        data_source_fingerprint="a" * 64,
    )

    warnings = _query_quality_warnings(["upstream warning", "upstream warning"], output)

    assert warnings.count("upstream warning") == 1
    assert "Query returned fewer than 30 rows; Review must assess sample-size risk." in warnings
    assert "Result columns with null rate at or above 20%: metric_value." in warnings


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection"):
        self.connection = connection
        self.description = [("value",)]
        self.rows: list[dict[str, object]] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.closed = True

    def execute(self, sql: str, params=None):
        self.connection.statements.append((sql, params))
        if sql == MYSQL_IDENTITY_SQL:
            self.rows = [dict(self.connection.identity)]
            self.description = [("server_uuid",), ("server_version",), ("account_identifier",)]
        elif sql.startswith("SELECT 1 AS value"):
            self.rows = [{"value": 1}]
            self.description = [("value",)]
        else:
            self.rows = []

    def fetchmany(self, count: int):
        values = self.rows[:count]
        self.rows = self.rows[count:]
        return values

    def fetchone(self):
        values = self.fetchmany(1)
        return values[0] if values else None


class _FakeConnection:
    def __init__(self, identity: dict[str, str] | None = None, *, tls_socket=None, tls_context=None):
        self.identity = identity or {
            "server_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "server_version": "8.4.2",
            "account_identifier": "readonly@%",
        }
        self._sock = tls_socket
        self.ctx = tls_context
        self._read_timeout = None
        self._write_timeout = None
        self.statements: list[tuple[str, object]] = []
        self.cursors: list[_FakeCursor] = []
        self.closed = False

    def cursor(self):
        cursor = _FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


def _mysql_config(**overrides) -> DbConfig:
    values = {
        "db_type": "mysql",
        "data_source_id": "mysql-v12",
        "mysql_host": "db.example.test",
        "mysql_user": "readonly",
        "mysql_password": "do-not-leak",
        "mysql_database": "analytics",
    }
    values.update(overrides)
    return DbConfig(**values)


def test_csv_write_failure_removes_temp_and_closes_mysql_cursor(monkeypatch, tmp_path: Path) -> None:
    connection = _FakeConnection()
    config = _mysql_config()
    descriptor_connection = _FakeConnection()
    expected = sha256_json(data_source_descriptor(config, connection=descriptor_connection))

    import pymysql

    connect_kwargs: dict[str, object] = {}

    def fake_connect(**kwargs):
        connect_kwargs.update(kwargs)
        return connection

    monkeypatch.setattr(pymysql, "connect", fake_connect)
    monkeypatch.setattr(query_runner, "_write_checked", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
    adapter = MySQLAdapter(config)
    result_path = tmp_path / "result.csv"
    with pytest.raises(OSError, match="disk full"):
        _execute_to_temporary_result(
            adapter,
            "SELECT 1 AS value LIMIT 1",
            max_rows=1,
            timeout_seconds=7,
            max_result_bytes=4096,
            expected_data_source_fingerprint=expected,
            result_path=result_path,
        )

    assert connect_kwargs["cursorclass"] is pymysql.cursors.SSDictCursor
    assert connect_kwargs["connect_timeout"] == 7
    assert connect_kwargs["read_timeout"] == 7
    assert connection.closed is True
    assert connection.cursors and all(cursor.closed for cursor in connection.cursors)
    assert not result_path.exists()
    assert not list(tmp_path.glob(".result.csv.*.tmp"))


def test_aborted_mysql_stream_discards_unbuffered_result(monkeypatch) -> None:
    result = SimpleNamespace(unbuffered_active=True)

    class AbortCursor(_FakeCursor):
        def __init__(self, connection):
            super().__init__(connection)
            self._result = result

        def _clear_result(self):
            self._result = None

    class AbortConnection(_FakeConnection):
        def __init__(self):
            super().__init__()
            self._result = result
            self.close_count = 0

        def cursor(self):
            cursor = AbortCursor(self)
            self.cursors.append(cursor)
            return cursor

        def close(self):
            self.close_count += 1
            self.closed = True

    connection = AbortConnection()
    config = _mysql_config()
    expected = sha256_json(data_source_descriptor(config, connection=_FakeConnection()))

    import pymysql

    monkeypatch.setattr(pymysql, "connect", lambda **_kwargs: connection)
    adapter = MySQLAdapter(config)
    with pytest.raises(RuntimeError, match="abort consumer"):
        with adapter.stream_readonly(
            "SELECT 1 AS value LIMIT 1",
            1,
            5,
            expected_data_source_fingerprint=expected,
        ):
            raise RuntimeError("abort consumer")

    assert connection.close_count == 1
    assert connection._result is None
    assert result.unbuffered_active is False
    assert connection.cursors and all(cursor.closed for cursor in connection.cursors)


class _FakeTlsSocket:
    def cipher(self):
        return ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)

    def version(self):
        return "TLSv1.3"

    def settimeout(self, _value):
        return None


def test_mysql_fingerprint_binds_server_account_and_tls_without_password() -> None:
    base_config = _mysql_config(mysql_ssl_mode="preferred")
    base_connection = _FakeConnection()
    base_descriptor = data_source_descriptor(base_config, connection=base_connection)
    base_fingerprint = sha256_json(base_descriptor)
    assert "do-not-leak" not in json.dumps(base_descriptor, ensure_ascii=False)
    assert "do-not-leak" not in repr(base_config)
    assert "do-not-leak" not in redact_db_error(RuntimeError("connection failed: do-not-leak"), base_config)

    changed_identity = dict(base_connection.identity, server_uuid="ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee")
    changed_version = dict(base_connection.identity, server_version="8.4.3")
    changed_account = dict(base_connection.identity, account_identifier="other_readonly@%")
    assert sha256_json(data_source_descriptor(base_config, connection=_FakeConnection(changed_identity))) != base_fingerprint
    assert sha256_json(data_source_descriptor(base_config, connection=_FakeConnection(changed_version))) != base_fingerprint
    assert sha256_json(data_source_descriptor(base_config, connection=_FakeConnection(changed_account))) != base_fingerprint

    tls_context = SimpleNamespace(verify_mode=ssl.CERT_REQUIRED, check_hostname=True)
    verified_config = _mysql_config(mysql_ssl_mode="verify_identity", mysql_ssl_ca="test-ca.pem")
    verified_connection = _FakeConnection(tls_socket=_FakeTlsSocket(), tls_context=tls_context)
    verified_descriptor = data_source_descriptor(verified_config, connection=verified_connection)
    assert verified_descriptor["tls"]["active"] is True
    assert verified_descriptor["tls"]["identity_verified"] is True
    assert sha256_json(verified_descriptor) != base_fingerprint

    changed_password_config = _mysql_config(mysql_password="another-secret")
    same_identity_descriptor = data_source_descriptor(changed_password_config, connection=_FakeConnection())
    assert sha256_json(same_identity_descriptor) == base_fingerprint
    assert "another-secret" not in json.dumps(same_identity_descriptor, ensure_ascii=False)


def test_mysql_identity_change_fails_before_user_sql(monkeypatch) -> None:
    config = _mysql_config()
    approved_connection = _FakeConnection()
    expected = sha256_json(data_source_descriptor(config, connection=approved_connection))
    changed_identity = dict(approved_connection.identity, server_uuid="ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee")
    execution_connection = _FakeConnection(changed_identity)

    import pymysql

    monkeypatch.setattr(pymysql, "connect", lambda **_kwargs: execution_connection)
    adapter = MySQLAdapter(config)
    with pytest.raises(ValueError, match="physical data source identity"):
        with adapter.stream_readonly(
            "SELECT 1 AS value LIMIT 1",
            1,
            5,
            expected_data_source_fingerprint=expected,
        ) as stream:
            list(stream.iter_rows())

    assert not any(sql.startswith("SELECT 1 AS value") for sql, _params in execution_connection.statements)
    assert execution_connection.closed is True
    assert execution_connection.cursors and all(cursor.closed for cursor in execution_connection.cursors)


def test_mysql_integration_smoke_query_is_readonly_and_bounded() -> None:
    prepared = validate_and_rewrite(MYSQL_INTEGRATION_SQL, "mysql", max_rows=1)
    assert prepared.ok, prepared.errors
    assert prepared.rewritten is True
    assert "LIMIT 2" in prepared.sql


@pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="real MySQL integration is opt-in")
def test_real_mysql_readonly_integration_entry() -> None:
    expected = os.getenv("MYSQL_EXPECTED_FINGERPRINT", "")
    assert re.fullmatch(r"[A-Fa-f0-9]{64}", expected), "MYSQL_EXPECTED_FINGERPRINT is required"
    config = load_config("mysql")
    assert data_source_fingerprint(config) == expected.lower()
    adapter = MySQLAdapter(config)
    prepared = validate_and_rewrite(MYSQL_INTEGRATION_SQL, "mysql", max_rows=1)
    assert prepared.ok
    with adapter.stream_readonly(
        prepared.sql,
        1,
        10,
        expected_data_source_fingerprint=expected.lower(),
    ) as stream:
        rows = list(stream.iter_rows())
    assert rows == [{"exact_decimal": Decimal("9007199254740993.01"), "unicode_text": "数据库-🙂"}]

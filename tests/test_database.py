from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import ROOT, approve_pending, materialize_stage, stage_output
from db_common import DbConfig, MySQLAdapter, SQLiteAdapter, data_source_fingerprint
from runctl import audit_run, load_state, prepare_query, revise, start_stage
from run_readonly_query import _profile, _serializable
from sql_guard import validate_and_rewrite


def create_database(path: Path, *, large: bool = False) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE sales(day TEXT NOT NULL, revenue REAL)")
        rows = [
            ("2026-01-01", 10.0),
            ("2026-01-01", 5.0),
            ("2026-01-02", None),
        ]
        if large:
            rows.extend(("x" * 200 + str(index), float(index)) for index in range(1, 29))
        connection.executemany("INSERT INTO sales(day, revenue) VALUES (?, ?)", rows)
        connection.commit()
    finally:
        connection.close()
    return path


def run_stage(run_dir: Path, role: str, stage_id: str, **kwargs) -> None:
    attempt_dir = start_stage(run_dir, stage_id)
    output = stage_output(role, "test-run", stage_id, int(attempt_dir.name.removeprefix("attempt-")), **kwargs)
    materialize_stage(run_dir, output)


def advance_to_approved_sql(run_dir: Path, sql: str) -> None:
    run_stage(run_dir, "growth-metrics", "s01-metrics")
    approve_pending(run_dir, "stage", "db-metrics-key")
    run_stage(run_dir, "growth-sql", "s02-sql", sql=sql)
    approve_pending(run_dir, "stage", "db-sql-key")


def query_environment(database: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DB_TYPE": "sqlite",
            "DB_SOURCE_ID": "sqlite-test",
            "SQLITE_PATH": str(database),
        }
    )
    return env


def sqlite_fingerprint(database: Path) -> str:
    return data_source_fingerprint(DbConfig(db_type="sqlite", data_source_id="sqlite-test", sqlite_path=str(database)))


def execute_query_script(run_dir: Path, database: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_readonly_query.py"), "--run-dir", str(run_dir), "--db", "sqlite"],
        cwd=ROOT,
        env=query_environment(database),
        text=True,
        capture_output=True,
        check=False,
    )


def current_artifact_path(run_dir: Path, kind: str) -> Path:
    artifacts = [
        item
        for item in load_state(run_dir)["artifacts"]
        if item.get("kind") == kind and not item.get("superseded_by")
    ]
    assert artifacts, f"Missing active artifact kind: {kind}"
    return run_dir / artifacts[-1]["path"]


def test_sqlite_adapter_is_read_only_and_inspects_schema(tmp_path: Path) -> None:
    database = create_database(tmp_path / "analytics.db")
    adapter = SQLiteAdapter(DbConfig(db_type="sqlite", data_source_id="sqlite-test", sqlite_path=str(database)))
    assert adapter.test_connection() == 1
    schema = adapter.inspect_schema()
    assert schema[0]["table"] == "sales"
    assert [item["name"] for item in schema[0]["columns"]] == ["day", "revenue"]
    result = adapter.execute_readonly("SELECT day, revenue FROM sales LIMIT 2", max_rows=2, timeout_seconds=5)
    assert len(result.rows) == 2
    with pytest.raises(ValueError, match="read-only"):
        adapter.execute_readonly("UPDATE sales SET revenue = 0", max_rows=2, timeout_seconds=5)
    connection = adapter.connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM sales")
    finally:
        connection.close()


def test_sqlite_adapter_rejects_duplicate_result_columns(tmp_path: Path) -> None:
    database = create_database(tmp_path / "duplicates.db")
    adapter = SQLiteAdapter(DbConfig(db_type="sqlite", data_source_id="sqlite-test", sqlite_path=str(database)))
    with pytest.raises(ValueError, match="duplicate column names"):
        adapter.execute_readonly(
            "SELECT day AS duplicate, revenue AS duplicate FROM sales LIMIT 2",
            max_rows=2,
            timeout_seconds=5,
        )


def test_adapter_detects_output_truncation_with_sentinel_limit(tmp_path: Path) -> None:
    database = create_database(tmp_path / "truncation.db")
    adapter = SQLiteAdapter(DbConfig(db_type="sqlite", data_source_id="sqlite-test", sqlite_path=str(database)))
    prepared = validate_and_rewrite("SELECT day, revenue FROM sales", dialect="sqlite", max_rows=2)
    assert prepared.ok and "LIMIT 3" in prepared.sql
    result = adapter.execute_readonly(prepared.sql, max_rows=2, timeout_seconds=5)
    assert result.truncated is True
    assert len(result.rows) == 2


def test_query_requires_separate_hash_approval_and_writes_manifest(approved_run, tmp_path: Path) -> None:
    sql = "SELECT day, SUM(revenue) AS revenue_total FROM sales GROUP BY day"
    database = create_database(tmp_path / "analytics.db")
    run_dir, _ = approved_run(["growth-metrics", "growth-sql", "growth-insight", "growth-review"])
    advance_to_approved_sql(run_dir, sql)
    sql_path = tmp_path / "proposed.sql"
    sql_path.write_text(sql, encoding="utf-8")
    request = prepare_query(run_dir, sql_path, "q-revenue", "sqlite-test", "sqlite", 10, 100, 1024 * 1024, sqlite_fingerprint(database))
    assert load_state(run_dir)["status"] == "awaiting_query_confirmation"

    denied = execute_query_script(run_dir, database)
    assert denied.returncode == 2
    assert "no matching" in denied.stderr
    assert not (run_dir / "data" / "result.csv").exists()

    approval = approve_pending(run_dir, "query", "db-query-key")
    assert approval["query"]["query_sha256"] == request["sql_sha256"]
    with pytest.raises(ValueError, match="prepared query"):
        start_stage(run_dir, "s03-insight")

    executed = execute_query_script(run_dir, database)
    assert executed.returncode == 0, executed.stderr
    manifest_path = current_artifact_path(run_dir, "query_manifest")
    result_path = current_artifact_path(run_dir, "query_result")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["returned_rows"] == 2
    assert manifest["result_sha256"]
    assert manifest["profile_sha256"]
    assert manifest["result_bytes"] == result_path.stat().st_size
    revenue_profile = next(item for item in manifest["columns"] if item["name"] == "revenue_total")
    assert revenue_profile["null_count"] == 1
    assert load_state(run_dir)["pending_query"] is None
    assert audit_run(run_dir) == []


def test_sql_tampering_after_approval_is_blocked(approved_run, tmp_path: Path) -> None:
    sql = "SELECT day, SUM(revenue) AS revenue_total FROM sales GROUP BY day"
    database = create_database(tmp_path / "analytics.db")
    run_dir, _ = approved_run(["growth-metrics", "growth-sql", "growth-insight", "growth-review"])
    advance_to_approved_sql(run_dir, sql)
    sql_path = tmp_path / "proposed.sql"
    sql_path.write_text(sql, encoding="utf-8")
    prepare_query(run_dir, sql_path, "q-revenue", "sqlite-test", "sqlite", 10, 100, 1024 * 1024, sqlite_fingerprint(database))
    approve_pending(run_dir, "query", "tamper-query-key")
    approved_sql_path = run_dir / load_state(run_dir)["pending_query"]["sql_path"]
    approved_sql_path.write_text("SELECT 999 AS revenue_total LIMIT 1\n", encoding="utf-8")
    result = execute_query_script(run_dir, database)
    assert result.returncode == 2
    assert "hash changed" in result.stderr


def test_physical_data_source_change_after_approval_is_blocked(approved_run, tmp_path: Path) -> None:
    sql = "SELECT day, SUM(revenue) AS revenue_total FROM sales GROUP BY day"
    approved_database = create_database(tmp_path / "approved.db")
    other_database = create_database(tmp_path / "other.db")
    run_dir, _ = approved_run(["growth-metrics", "growth-sql", "growth-insight", "growth-review"])
    advance_to_approved_sql(run_dir, sql)
    sql_path = tmp_path / "proposed.sql"
    sql_path.write_text(sql, encoding="utf-8")
    prepare_query(
        run_dir,
        sql_path,
        "q-revenue",
        "sqlite-test",
        "sqlite",
        10,
        100,
        1024 * 1024,
        sqlite_fingerprint(approved_database),
    )
    approve_pending(run_dir, "query", "physical-source-key")
    result = execute_query_script(run_dir, other_database)
    assert result.returncode == 2
    assert "physical data source" in result.stderr


def test_result_larger_than_approved_byte_limit_is_blocked(approved_run, tmp_path: Path) -> None:
    sql = "SELECT day, SUM(revenue) AS revenue_total FROM sales GROUP BY day"
    database = create_database(tmp_path / "large.db", large=True)
    run_dir, _ = approved_run(["growth-metrics", "growth-sql", "growth-insight", "growth-review"])
    advance_to_approved_sql(run_dir, sql)
    sql_path = tmp_path / "proposed.sql"
    sql_path.write_text(sql, encoding="utf-8")
    prepare_query(run_dir, sql_path, "q-revenue", "sqlite-test", "sqlite", 10, 100, 1024, sqlite_fingerprint(database))
    approve_pending(run_dir, "query", "size-query-key")
    result = execute_query_script(run_dir, database)
    assert result.returncode == 2
    assert "exceeds approved maximum" in result.stderr
    assert not (run_dir / "data" / "result.csv").exists()
    assert load_state(run_dir)["pending_query"]["approved"] is True


def test_unapproved_query_can_return_to_sql_revision(approved_run, tmp_path: Path) -> None:
    sql = "SELECT day, SUM(revenue) AS revenue_total FROM sales GROUP BY day"
    run_dir, _ = approved_run(["growth-metrics", "growth-sql", "growth-insight", "growth-review"])
    advance_to_approved_sql(run_dir, sql)
    sql_path = tmp_path / "proposed.sql"
    sql_path.write_text(sql, encoding="utf-8")
    database = create_database(tmp_path / "revision.db")
    prepare_query(run_dir, sql_path, "q-revenue", "sqlite-test", "sqlite", 10, 100, 4096, sqlite_fingerprint(database))
    state = revise(run_dir, "s02-sql", "Do not execute; revise the query scope.")
    assert state["status"] == "revising"
    assert state["pending_query"] is None
    assert state["pending_stage"] == "s02-sql"
    assert state["stages"][2]["status"] == "pending"


class FakeCursor:
    def __init__(self, statements: list[tuple[str, object]]):
        self.statements = statements
        self.description = [("value",)]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchone(self):
        return {"ok": 1}

    def fetchmany(self, count):
        return [{"value": 1}]


class FakeConnection:
    def __init__(self):
        self.statements: list[tuple[str, object]] = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self.statements)

    def close(self):
        self.closed = True


def test_mysql_adapter_contract_sets_read_only_and_timeout(monkeypatch) -> None:
    connections: list[FakeConnection] = []

    def fake_connect(**kwargs):
        assert kwargs["host"] == "localhost"
        assert kwargs["read_timeout"] == 7
        connection = FakeConnection()
        connections.append(connection)
        return connection

    import pymysql

    monkeypatch.setattr(pymysql, "connect", fake_connect)
    adapter = MySQLAdapter(
        DbConfig(
            db_type="mysql",
            data_source_id="mysql-test",
            mysql_host="localhost",
            mysql_user="readonly",
            mysql_password="secret",
            mysql_database="analytics",
        )
    )
    result = adapter.execute_readonly("SELECT 1 AS value LIMIT 1", max_rows=1, timeout_seconds=7)
    assert result.rows == [{"value": 1}]
    statements = [sql for connection in connections for sql, _ in connection.statements]
    assert "SET SESSION TRANSACTION READ ONLY" in statements
    assert "SET SESSION MAX_EXECUTION_TIME = %s" in statements
    assert all(connection.closed for connection in connections)


def test_mysql_timeout_configuration_fails_closed(monkeypatch) -> None:
    class TimeoutCursor(FakeCursor):
        def execute(self, sql, params=None):
            self.statements.append((sql, params))
            if sql == "SET SESSION MAX_EXECUTION_TIME = %s":
                raise RuntimeError("timeout setting denied")

    class TimeoutConnection(FakeConnection):
        def cursor(self):
            return TimeoutCursor(self.statements)

    connection = TimeoutConnection()
    import pymysql

    monkeypatch.setattr(pymysql, "connect", lambda **_kwargs: connection)
    adapter = MySQLAdapter(
        DbConfig(
            db_type="mysql",
            data_source_id="mysql-test",
            mysql_host="localhost",
            mysql_user="readonly",
            mysql_password="secret",
            mysql_database="analytics",
        )
    )
    with pytest.raises(RuntimeError, match="timeout setting denied"):
        adapter.execute_readonly("SELECT 1 AS value LIMIT 1", max_rows=1, timeout_seconds=1)
    assert not any(sql == "SELECT 1 AS value LIMIT 1" for sql, _ in connection.statements)
    assert connection.closed is True


def test_decimal_results_preserve_exact_text_and_profile_bounds() -> None:
    value = Decimal("9007199254740993.01")
    assert _serializable(value) == "9007199254740993.01"
    profile = _profile([{"name": "amount", "type": "number"}], [{"amount": value}])
    assert profile["columns"][0]["minimum"] == "9007199254740993.01"
    assert profile["columns"][0]["maximum"] == "9007199254740993.01"
    integer_profile = _profile([{"name": "amount", "type": "integer"}], [{"amount": 9007199254740993}])
    assert integer_profile["columns"][0]["minimum"] == 9007199254740993

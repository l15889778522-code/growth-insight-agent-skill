from __future__ import annotations

import os
import re
import time
from decimal import Decimal
from pathlib import Path

import pymysql
import pytest

from db_common import MySQLAdapter, data_source_descriptor, data_source_fingerprint, load_config
from run_readonly_query import ResultSizeLimitExceeded, _execute_to_temporary_result
from sql_guard import validate_and_rewrite


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1",
    reason="real MySQL integration is opt-in",
)


def prepared_sql(sql: str, max_rows: int) -> str:
    prepared = validate_and_rewrite(sql, "mysql", max_rows=max_rows)
    assert prepared.ok, prepared.errors
    return prepared.sql


def direct_connection(*, admin: bool = False) -> pymysql.Connection:
    prefix = "MYSQL_CI_ADMIN_" if admin else "MYSQL_"
    return pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ[f"{prefix}USER"],
        password=os.environ[f"{prefix}PASSWORD"],
        database=None if admin else os.environ["MYSQL_DATABASE"],
        charset="utf8mb4",
        autocommit=True,
        ssl_disabled=False,
        ssl={"check_hostname": False},
    )


def kill_connection(connection_id: int) -> None:
    connection = direct_connection(admin=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"KILL CONNECTION {int(connection_id)}")
    finally:
        connection.close()


def test_real_mysql_release_gate(tmp_path: Path) -> None:
    expected = os.environ.get("MYSQL_EXPECTED_FINGERPRINT", "")
    assert re.fullmatch(r"[A-Fa-f0-9]{64}", expected), "MYSQL_EXPECTED_FINGERPRINT is required"

    config = load_config("mysql")
    descriptor = data_source_descriptor(config)
    assert descriptor["tls"]["active"] is True
    assert descriptor["tls"]["requested_mode"] == "required"
    assert descriptor["tls"]["cipher"]
    assert descriptor["account_identifier"].startswith("ci_readonly@")
    assert data_source_fingerprint(config) == expected.lower()
    adapter = MySQLAdapter(config)

    unicode_value = "\u6570\u636e\u5e93-\U0001f642"
    exact_sql = prepared_sql(
        "SELECT CAST('9007199254740993.01' AS DECIMAL(20,2)) AS exact_decimal, "
        f"'{unicode_value}' AS unicode_text",
        1,
    )
    with adapter.stream_readonly(
        exact_sql,
        1,
        10,
        expected_data_source_fingerprint=expected,
    ) as stream:
        exact_rows = list(stream.iter_rows())
    assert exact_rows == [
        {"exact_decimal": Decimal("9007199254740993.01"), "unicode_text": unicode_value}
    ]

    bounded_sql = prepared_sql(
        "SELECT id, exact_decimal, unicode_text FROM ci_values ORDER BY id",
        1000,
    )
    with adapter.stream_readonly(
        bounded_sql,
        1000,
        10,
        batch_size=127,
        expected_data_source_fingerprint=expected,
    ) as stream:
        bounded_rows = list(stream.iter_rows())
    assert len(bounded_rows) == 1000
    assert stream.truncated is True
    assert bounded_rows[0]["exact_decimal"] == Decimal("9007199254740994.01")
    assert bounded_rows[0]["unicode_text"] == f"{unicode_value}-1"

    oversized_sql = prepared_sql(
        "SELECT id, payload FROM ci_values ORDER BY id",
        20,
    )
    result_path = tmp_path / "oversized.csv"
    with pytest.raises(ResultSizeLimitExceeded):
        _execute_to_temporary_result(
            adapter,
            oversized_sql,
            max_rows=20,
            timeout_seconds=10,
            max_result_bytes=100,
            expected_data_source_fingerprint=expected,
            result_path=result_path,
        )
    assert not result_path.exists()
    assert not list(tmp_path.glob(".oversized.csv.*.tmp"))

    readonly_connection = direct_connection()
    try:
        with readonly_connection.cursor() as cursor:
            cursor.execute("SHOW GRANTS")
            grants = "\n".join(str(value) for row in cursor.fetchall() for value in row)
            assert "GRANT SELECT ON `analytics`.*" in grants
            assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b", grants)
            with pytest.raises(pymysql.MySQLError):
                cursor.execute("UPDATE ci_values SET payload = 'forbidden' WHERE id = 1")
    finally:
        readonly_connection.close()

    lock_connection = direct_connection(admin=True)
    try:
        with lock_connection.cursor() as cursor:
            cursor.execute("LOCK TABLES analytics.ci_values WRITE")
        timeout_sql = prepared_sql("SELECT COUNT(*) AS row_count FROM ci_values", 1)
        started = time.monotonic()
        with pytest.raises((pymysql.MySQLError, TimeoutError, OSError)):
            with adapter.stream_readonly(
                timeout_sql,
                1,
                1,
                expected_data_source_fingerprint=expected,
            ) as stream:
                list(stream.iter_rows())
        assert time.monotonic() - started < 3
    finally:
        try:
            with lock_connection.cursor() as cursor:
                cursor.execute("UNLOCK TABLES")
        finally:
            lock_connection.close()

    disconnect_sql = prepared_sql(
        "SELECT CONNECTION_ID() AS connection_id, id, "
        "REPEAT(payload, 250) AS large_payload "
        "FROM ci_values",
        1000,
    )
    with pytest.raises((pymysql.MySQLError, TimeoutError, OSError)):
        with adapter.stream_readonly(
            disconnect_sql,
            1000,
            10,
            batch_size=1,
            expected_data_source_fingerprint=expected,
        ) as stream:
            iterator = stream.iter_rows()
            first_row = next(iterator)
            kill_connection(first_row["connection_id"])
            list(iterator)

    retry_sql = prepared_sql("SELECT COUNT(*) AS row_count FROM ci_values", 1)
    with adapter.stream_readonly(
        retry_sql,
        1,
        10,
        expected_data_source_fingerprint=expected,
    ) as stream:
        retry_rows = list(stream.iter_rows())
    assert retry_rows == [{"row_count": 2505}]

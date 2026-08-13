from __future__ import annotations

import pytest

from sql_guard import validate_and_rewrite


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE sales SET revenue = 0",
        "DELETE FROM sales",
        "DROP TABLE sales",
        "SELECT * FROM sales; DELETE FROM sales",
        "SELECT * FROM sales FOR UPDATE",
        "SELECT SLEEP(1)",
        "SELECT LOAD_FILE('/etc/passwd')",
        "SELECT * FROM sales LIMIT ?",
        "SELECT * INTO OUTFILE '/tmp/x' FROM sales",
        "SELECT /*!50000 SLEEP(10), */ 1 LIMIT 1",
    ],
)
def test_blocks_writes_multiple_statements_locks_and_risky_functions(sql: str) -> None:
    result = validate_and_rewrite(sql, dialect="mysql", max_rows=100)
    assert not result.ok, sql
    assert result.errors


def test_adds_limit_and_is_idempotent() -> None:
    first = validate_and_rewrite("SELECT day, revenue FROM sales", dialect="sqlite", max_rows=25)
    assert first.ok
    assert first.rewritten
    assert "LIMIT 26" in first.sql
    second = validate_and_rewrite(first.sql, dialect="sqlite", max_rows=25)
    assert second.ok
    assert not second.rewritten
    assert second.sql == first.sql


def test_reduces_oversized_limit() -> None:
    result = validate_and_rewrite("SELECT * FROM sales LIMIT 5000", dialect="sqlite", max_rows=100)
    assert result.ok and result.rewritten
    assert "LIMIT 101" in result.sql


@pytest.mark.parametrize(
    "sql",
    [
        "WITH totals AS (SELECT SUM(revenue) AS value FROM sales) SELECT * FROM totals LIMIT 5",
        "SHOW TABLES",
        "DESCRIBE sales",
        "EXPLAIN SELECT * FROM sales",
    ],
)
def test_allows_explicit_read_only_statements(sql: str) -> None:
    result = validate_and_rewrite(sql, dialect="mysql", max_rows=100)
    assert result.ok, result.errors

#!/usr/bin/env python3
"""Read-only database adapters used by deterministic query scripts."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from sql_guard import validate_and_rewrite
from runtime_common import sha256_json


@dataclass(frozen=True)
class DbConfig:
    db_type: str
    data_source_id: str
    sqlite_path: str | None = None
    mysql_host: str | None = None
    mysql_port: int = 3306
    mysql_user: str | None = None
    mysql_password: str | None = None
    mysql_database: str | None = None


@dataclass
class QueryResult:
    columns: list[dict[str, str]]
    rows: list[dict[str, Any]]
    elapsed_ms: int
    truncated: bool


class DatabaseAdapter(Protocol):
    db_type: str

    def connect(self) -> Any: ...

    def test_connection(self) -> Any: ...

    def inspect_schema(self) -> list[dict[str, Any]]: ...

    def execute_readonly(self, sql: str, max_rows: int, timeout_seconds: int) -> QueryResult: ...

    def quote_identifier(self, value: str) -> str: ...

    def normalize_type(self, value: str) -> str: ...


def load_config(db_type: str | None = None) -> DbConfig:
    resolved = (db_type or os.getenv("DB_TYPE") or "sqlite").lower()
    data_source_id = os.getenv("DB_SOURCE_ID")
    if not data_source_id:
        raise ValueError("DB_SOURCE_ID is required and must be a stable, user-chosen data source label.")
    return DbConfig(
        db_type=resolved,
        data_source_id=data_source_id,
        sqlite_path=os.getenv("SQLITE_PATH"),
        mysql_host=os.getenv("MYSQL_HOST"),
        mysql_port=int(os.getenv("MYSQL_PORT", "3306")),
        mysql_user=os.getenv("MYSQL_USER"),
        mysql_password=os.getenv("MYSQL_PASSWORD"),
        mysql_database=os.getenv("MYSQL_DATABASE"),
    )


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, (float, Decimal)):
        return "number"
    if isinstance(value, bytes):
        return "binary"
    return "string"


def data_source_fingerprint(config: DbConfig) -> str:
    if config.db_type == "sqlite":
        if not config.sqlite_path:
            raise ValueError("SQLITE_PATH is required to fingerprint a sqlite data source.")
        descriptor = {
            "db_type": "sqlite",
            "path": str(Path(config.sqlite_path).expanduser().resolve()),
        }
    elif config.db_type == "mysql":
        if not config.mysql_host or not config.mysql_database:
            raise ValueError("MYSQL_HOST and MYSQL_DATABASE are required to fingerprint a MySQL data source.")
        descriptor = {
            "db_type": "mysql",
            "host": config.mysql_host.strip().lower(),
            "port": config.mysql_port,
            "database": config.mysql_database,
        }
    else:
        raise ValueError(f"Unsupported DB_TYPE: {config.db_type}")
    return sha256_json(descriptor)


def _column_names(description: Any) -> list[str]:
    names = [str(item[0]) for item in (description or [])]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError("Query result contains duplicate column names; add explicit unique aliases: " + ", ".join(duplicates))
    return names


def _assert_prepared_readonly(sql: str, dialect: str, max_rows: int, timeout_seconds: int) -> None:
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be at least 1.")
    result = validate_and_rewrite(sql, dialect=dialect, max_rows=max_rows)
    if not result.ok:
        raise ValueError("SQL failed adapter read-only validation: " + "; ".join(result.errors))
    if result.rewritten:
        raise ValueError("SQL must be prepared with an explicit bounded LIMIT before adapter execution.")


class SQLiteAdapter:
    db_type = "sqlite"

    def __init__(self, config: DbConfig):
        if not config.sqlite_path:
            raise ValueError("SQLITE_PATH is required for sqlite connections.")
        self.config = config
        self.path = Path(config.sqlite_path).expanduser().resolve()
        if not self.path.is_file():
            raise ValueError(f"SQLite database does not exist: {self.path}")

    def connect(self) -> sqlite3.Connection:
        normalized = str(self.path).replace("\\", "/")
        uri = f"file:{quote(normalized, safe='/:')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def test_connection(self) -> Any:
        with closing(self.connect()) as connection:
            return connection.execute("SELECT 1").fetchone()[0]

    def quote_identifier(self, value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    def normalize_type(self, value: str) -> str:
        upper = value.upper()
        if "INT" in upper:
            return "integer"
        if any(token in upper for token in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
            return "number"
        if "BLOB" in upper:
            return "binary"
        if any(token in upper for token in ("DATE", "TIME")):
            return "datetime"
        return "string"

    def inspect_schema(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with closing(self.connect()) as connection:
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            for table_row in tables:
                table = table_row[0]
                columns = connection.execute(f"PRAGMA table_info({self.quote_identifier(table)})").fetchall()
                records.append(
                    {
                        "table": table,
                        "columns": [
                            {
                                "name": row[1],
                                "native_type": row[2] or "",
                                "normalized_type": self.normalize_type(row[2] or ""),
                                "nullable": not bool(row[3]),
                                "default": row[4],
                                "primary_key": bool(row[5]),
                            }
                            for row in columns
                        ],
                    }
                )
        return records

    def execute_readonly(self, sql: str, max_rows: int, timeout_seconds: int) -> QueryResult:
        _assert_prepared_readonly(sql, "sqlite", max_rows, timeout_seconds)
        started = time.monotonic()
        deadline = started + timeout_seconds
        with closing(self.connect()) as connection:
            connection.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1000)
            cursor = connection.execute(sql)
            raw_rows = cursor.fetchmany(max_rows + 1)
            column_names = _column_names(cursor.description)
        truncated = len(raw_rows) > max_rows
        raw_rows = raw_rows[:max_rows]
        rows = [dict(row) for row in raw_rows]
        columns = []
        for name in column_names:
            sample = next((row[name] for row in rows if row.get(name) is not None), None)
            columns.append({"name": name, "type": _value_type(sample)})
        return QueryResult(columns, rows, int((time.monotonic() - started) * 1000), truncated)


class MySQLAdapter:
    db_type = "mysql"

    def __init__(self, config: DbConfig):
        missing = [name for name, value in {"MYSQL_HOST": config.mysql_host, "MYSQL_USER": config.mysql_user, "MYSQL_DATABASE": config.mysql_database}.items() if not value]
        if missing:
            raise ValueError(f"Missing required MySQL environment variables: {', '.join(missing)}")
        self.config = config

    def connect(self, timeout_seconds: int | None = None) -> Any:
        import pymysql

        socket_timeout = timeout_seconds or 30
        connection = pymysql.connect(
            host=self.config.mysql_host,
            port=self.config.mysql_port,
            user=self.config.mysql_user,
            password=self.config.mysql_password or "",
            database=self.config.mysql_database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            read_timeout=socket_timeout,
            write_timeout=socket_timeout,
            autocommit=True,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
        except Exception:
            connection.close()
            raise
        return connection

    def test_connection(self) -> Any:
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                return cursor.fetchone()["ok"]
        finally:
            connection.close()

    def quote_identifier(self, value: str) -> str:
        return "`" + value.replace("`", "``") + "`"

    def normalize_type(self, value: str) -> str:
        lower = value.lower()
        if any(token in lower for token in ("int", "bit")):
            return "integer"
        if any(token in lower for token in ("decimal", "numeric", "float", "double", "real")):
            return "number"
        if any(token in lower for token in ("date", "time", "year")):
            return "datetime"
        if any(token in lower for token in ("blob", "binary")):
            return "binary"
        if "json" in lower:
            return "json"
        return "string"

    def inspect_schema(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT table_name, column_name, column_type, is_nullable, column_key, column_default
                    FROM information_schema.columns
                    WHERE table_schema = DATABASE()
                    ORDER BY table_name, ordinal_position
                    """
                )
                rows = cursor.fetchall()
        finally:
            connection.close()
        tables: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            tables.setdefault(row["table_name"], []).append(
                {
                    "name": row["column_name"],
                    "native_type": row["column_type"],
                    "normalized_type": self.normalize_type(row["column_type"]),
                    "nullable": row["is_nullable"] == "YES",
                    "default": row["column_default"],
                    "primary_key": row["column_key"] == "PRI",
                }
            )
        return [{"table": table, "columns": columns} for table, columns in tables.items()]

    def execute_readonly(self, sql: str, max_rows: int, timeout_seconds: int) -> QueryResult:
        _assert_prepared_readonly(sql, "mysql", max_rows, timeout_seconds)
        connection = self.connect(timeout_seconds=timeout_seconds)
        started = time.monotonic()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (timeout_seconds * 1000,))
                cursor.execute(sql)
                raw_rows = cursor.fetchmany(max_rows + 1)
                description = cursor.description or []
        finally:
            connection.close()
        truncated = len(raw_rows) > max_rows
        rows = raw_rows[:max_rows]
        column_names = _column_names(description)
        columns = []
        for name in column_names:
            sample = next((row[name] for row in rows if row.get(name) is not None), None)
            columns.append({"name": name, "type": _value_type(sample)})
        return QueryResult(columns, rows, int((time.monotonic() - started) * 1000), truncated)


def get_adapter(config: DbConfig) -> DatabaseAdapter:
    if config.db_type == "sqlite":
        return SQLiteAdapter(config)
    if config.db_type == "mysql":
        return MySQLAdapter(config)
    raise ValueError(f"Unsupported DB_TYPE: {config.db_type}")


def connect(config: DbConfig) -> Any:
    return get_adapter(config).connect()

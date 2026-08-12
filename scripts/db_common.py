#!/usr/bin/env python3
"""Read-only database adapters used by deterministic query scripts."""

from __future__ import annotations

import os
import ssl
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, ContextManager, Iterator, Protocol
from urllib.parse import quote

from runtime_common import sha256_json
from sql_guard import validate_and_rewrite


DEFAULT_FETCH_BATCH_SIZE = 1000
MYSQL_SSL_MODES = {"disabled", "preferred", "required", "verify_ca", "verify_identity"}
MYSQL_IDENTITY_SQL = (
    "SELECT @@server_uuid AS server_uuid, VERSION() AS server_version, "
    "CURRENT_USER() AS account_identifier"
)


@dataclass(frozen=True)
class DbConfig:
    db_type: str
    data_source_id: str
    sqlite_path: str | None = None
    mysql_host: str | None = None
    mysql_port: int = 3306
    mysql_user: str | None = None
    mysql_password: str | None = field(default=None, repr=False)
    mysql_database: str | None = None
    mysql_ssl_mode: str = "preferred"
    mysql_ssl_ca: str | None = None
    mysql_ssl_cert: str | None = None
    mysql_ssl_key: str | None = field(default=None, repr=False)
    mysql_ssl_key_password: str | None = field(default=None, repr=False)


@dataclass
class QueryResult:
    """Compatibility result for callers that explicitly request materialization."""

    columns: list[dict[str, str]]
    rows: list[dict[str, Any]]
    elapsed_ms: int
    truncated: bool


@dataclass
class QueryStream:
    """A bounded-batch result whose connection stays open inside its context."""

    columns: list[dict[str, str]]
    batches: Iterator[list[dict[str, Any]]]
    data_source_fingerprint: str | None = None
    elapsed_ms: int = 0
    truncated: bool = False
    exhausted: bool = False

    def iter_rows(self) -> Iterator[dict[str, Any]]:
        for batch in self.batches:
            yield from batch


class DatabaseAdapter(Protocol):
    db_type: str

    def connect(self) -> Any: ...

    def test_connection(self) -> Any: ...

    def inspect_schema(self) -> list[dict[str, Any]]: ...

    def stream_readonly(
        self,
        sql: str,
        max_rows: int,
        timeout_seconds: int,
        *,
        batch_size: int = DEFAULT_FETCH_BATCH_SIZE,
        expected_data_source_fingerprint: str | None = None,
    ) -> ContextManager[QueryStream]: ...

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
        mysql_ssl_mode=os.getenv("MYSQL_SSL_MODE", "preferred").strip().lower(),
        mysql_ssl_ca=os.getenv("MYSQL_SSL_CA"),
        mysql_ssl_cert=os.getenv("MYSQL_SSL_CERT"),
        mysql_ssl_key=os.getenv("MYSQL_SSL_KEY"),
        mysql_ssl_key_password=os.getenv("MYSQL_SSL_KEY_PASSWORD"),
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


def _sqlite_data_source_descriptor(config: DbConfig) -> dict[str, Any]:
    if not config.sqlite_path:
        raise ValueError("SQLITE_PATH is required to fingerprint a sqlite data source.")
    return {
        "db_type": "sqlite",
        "path": str(Path(config.sqlite_path).expanduser().resolve()),
    }


def _normalized_mysql_ssl_mode(config: DbConfig) -> str:
    mode = config.mysql_ssl_mode.strip().lower()
    if mode not in MYSQL_SSL_MODES:
        raise ValueError(
            "MYSQL_SSL_MODE must be one of: DISABLED, PREFERRED, REQUIRED, VERIFY_CA, VERIFY_IDENTITY."
        )
    return mode


def _mysql_tls_state(connection: Any, config: DbConfig) -> dict[str, Any]:
    mode = _normalized_mysql_ssl_mode(config)
    sock = getattr(connection, "_sock", None)
    cipher_method = getattr(sock, "cipher", None)
    cipher_info = cipher_method() if callable(cipher_method) else None
    cipher = cipher_info[0] if isinstance(cipher_info, tuple) and cipher_info else None
    protocol_method = getattr(sock, "version", None)
    protocol = protocol_method() if callable(protocol_method) else None
    if not protocol and isinstance(cipher_info, tuple) and len(cipher_info) > 1:
        protocol = cipher_info[1]
    active = bool(cipher)

    context = getattr(connection, "ctx", None)
    verify_mode = getattr(context, "verify_mode", ssl.CERT_NONE)
    certificate_verified = active and verify_mode != ssl.CERT_NONE
    identity_verified = certificate_verified and bool(getattr(context, "check_hostname", False))
    verification = "identity" if identity_verified else "certificate" if certificate_verified else "none"

    if mode == "disabled" and active:
        raise RuntimeError("MySQL TLS policy failed closed: TLS was active while MYSQL_SSL_MODE=DISABLED.")
    if mode in {"required", "verify_ca", "verify_identity"} and not active:
        raise RuntimeError(f"MySQL TLS policy failed closed: MYSQL_SSL_MODE={mode.upper()} was not negotiated.")
    if mode in {"verify_ca", "verify_identity"} and not certificate_verified:
        raise RuntimeError("MySQL TLS policy failed closed: the server certificate was not verified.")
    if mode == "verify_identity" and not identity_verified:
        raise RuntimeError("MySQL TLS policy failed closed: the server hostname was not verified.")

    return {
        "requested_mode": mode,
        "active": active,
        "protocol": protocol,
        "cipher": cipher,
        "certificate_verified": certificate_verified,
        "identity_verified": identity_verified,
        "verification": verification,
    }


def _mysql_connection_identity(connection: Any) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(MYSQL_IDENTITY_SQL)
        rows = cursor.fetchmany(2)
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError("MySQL identity query did not return exactly one mapping row.")
    row = rows[0]
    identity = {
        "server_identity": str(row.get("server_uuid") or "").strip().lower(),
        "server_version": str(row.get("server_version") or "").strip(),
        "account_identifier": str(row.get("account_identifier") or "").strip(),
    }
    missing = [name for name, value in identity.items() if not value]
    if missing:
        raise RuntimeError("MySQL identity query omitted required fields: " + ", ".join(missing))
    return identity


def _mysql_data_source_descriptor(config: DbConfig, connection: Any) -> dict[str, Any]:
    identity = _mysql_connection_identity(connection)
    return {
        "db_type": "mysql",
        "endpoint": {
            "host": str(config.mysql_host).strip().lower(),
            "port": config.mysql_port,
            "database": config.mysql_database,
        },
        **identity,
        "tls": _mysql_tls_state(connection, config),
    }


def data_source_descriptor(config: DbConfig, *, connection: Any | None = None) -> dict[str, Any]:
    """Return a password-free descriptor used for display and approval binding."""

    if config.db_type == "sqlite":
        return _sqlite_data_source_descriptor(config)
    if config.db_type != "mysql":
        raise ValueError(f"Unsupported DB_TYPE: {config.db_type}")
    if not config.mysql_host or not config.mysql_database:
        raise ValueError("MYSQL_HOST and MYSQL_DATABASE are required to fingerprint a MySQL data source.")
    if connection is not None:
        return _mysql_data_source_descriptor(config, connection)

    adapter = MySQLAdapter(config)
    owned_connection = adapter.connect()
    try:
        return _mysql_data_source_descriptor(config, owned_connection)
    finally:
        owned_connection.close()


def data_source_fingerprint(config: DbConfig, *, connection: Any | None = None) -> str:
    return sha256_json(data_source_descriptor(config, connection=connection))


def redact_db_error(exc: BaseException, config: DbConfig | None = None) -> str:
    """Render a connector error while removing configured credentials."""

    message = str(exc) or type(exc).__name__
    candidates = [os.getenv("MYSQL_PASSWORD"), os.getenv("MYSQL_SSL_KEY_PASSWORD")]
    if config is not None:
        candidates.extend((config.mysql_password, config.mysql_ssl_key_password))
    for secret in candidates:
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def _column_names(description: Any) -> list[str]:
    names = [str(item[0]) for item in (description or [])]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError("Query result contains duplicate column names; add explicit unique aliases: " + ", ".join(duplicates))
    return names


def _columns_with_inferred_types(column_names: list[str], rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    columns = []
    for name in column_names:
        sample = next((row.get(name) for row in rows if row.get(name) is not None), None)
        columns.append({"name": name, "type": _value_type(sample)})
    return columns


def _assert_prepared_readonly(sql: str, dialect: str, max_rows: int, timeout_seconds: int) -> None:
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be at least 1.")
    if max_rows < 1:
        raise ValueError("max_rows must be at least 1.")
    result = validate_and_rewrite(sql, dialect=dialect, max_rows=max_rows)
    if not result.ok:
        raise ValueError("SQL failed adapter read-only validation: " + "; ".join(result.errors))
    if result.rewritten:
        raise ValueError("SQL must be prepared with an explicit bounded LIMIT before adapter execution.")


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Database operation exceeded the approved timeout.")
    return remaining


def _iter_cursor_batches(
    cursor: Any,
    stream: QueryStream,
    *,
    max_rows: int,
    batch_size: int,
    deadline: float,
    row_factory: Any,
    before_fetch: Any | None = None,
) -> Iterator[list[dict[str, Any]]]:
    remaining = max_rows
    while remaining > 0:
        fetch_size = min(batch_size, remaining)
        if before_fetch is not None:
            before_fetch(_remaining_seconds(deadline))
        else:
            _remaining_seconds(deadline)
        raw_rows = cursor.fetchmany(fetch_size)
        _remaining_seconds(deadline)
        if not raw_rows:
            stream.exhausted = True
            return
        batch = [row_factory(row) for row in raw_rows[:fetch_size]]
        yield batch
        remaining -= len(batch)
        if len(raw_rows) < fetch_size:
            stream.exhausted = True
            return

    if before_fetch is not None:
        before_fetch(_remaining_seconds(deadline))
    else:
        _remaining_seconds(deadline)
    sentinel = cursor.fetchmany(1)
    _remaining_seconds(deadline)
    stream.truncated = bool(sentinel)
    stream.exhausted = True


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

    @contextmanager
    def stream_readonly(
        self,
        sql: str,
        max_rows: int,
        timeout_seconds: int,
        *,
        batch_size: int = DEFAULT_FETCH_BATCH_SIZE,
        expected_data_source_fingerprint: str | None = None,
    ) -> Iterator[QueryStream]:
        _assert_prepared_readonly(sql, "sqlite", max_rows, timeout_seconds)
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1.")
        fingerprint = data_source_fingerprint(self.config)
        if expected_data_source_fingerprint and fingerprint != expected_data_source_fingerprint.lower():
            raise ValueError("The physical data source identity does not match the approved fingerprint.")

        started = time.monotonic()
        deadline = started + timeout_seconds
        connection = self.connect()
        cursor: sqlite3.Cursor | None = None
        stream: QueryStream | None = None
        try:
            connection.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1000)
            cursor = connection.execute(sql)
            column_names = _column_names(cursor.description)
            stream = QueryStream(
                columns=[{"name": name, "type": "null"} for name in column_names],
                batches=iter(()),
                data_source_fingerprint=fingerprint,
            )
            stream.batches = _iter_cursor_batches(
                cursor,
                stream,
                max_rows=max_rows,
                batch_size=batch_size,
                deadline=deadline,
                row_factory=dict,
            )
            yield stream
        finally:
            if stream is not None:
                stream.elapsed_ms = int((time.monotonic() - started) * 1000)
            if cursor is not None:
                cursor.close()
            connection.close()

    def execute_readonly(self, sql: str, max_rows: int, timeout_seconds: int) -> QueryResult:
        with self.stream_readonly(sql, max_rows, timeout_seconds) as stream:
            rows = list(stream.iter_rows())
            column_names = [column["name"] for column in stream.columns]
        return QueryResult(
            _columns_with_inferred_types(column_names, rows),
            rows,
            stream.elapsed_ms,
            stream.truncated,
        )


def _mysql_ssl_kwargs(config: DbConfig) -> dict[str, Any]:
    mode = _normalized_mysql_ssl_mode(config)
    if bool(config.mysql_ssl_cert) != bool(config.mysql_ssl_key):
        raise ValueError("MYSQL_SSL_CERT and MYSQL_SSL_KEY must be configured together.")
    if mode == "disabled":
        if any((config.mysql_ssl_ca, config.mysql_ssl_cert, config.mysql_ssl_key)):
            raise ValueError("MySQL TLS certificate settings cannot be used with MYSQL_SSL_MODE=DISABLED.")
        return {"ssl_disabled": True}
    if mode in {"verify_ca", "verify_identity"} and not config.mysql_ssl_ca:
        raise ValueError(f"MYSQL_SSL_CA is required for MYSQL_SSL_MODE={mode.upper()}.")
    if mode == "preferred" and any((config.mysql_ssl_cert, config.mysql_ssl_key)):
        raise ValueError("Client certificates require MYSQL_SSL_MODE=REQUIRED, VERIFY_CA, or VERIFY_IDENTITY.")

    if mode == "preferred":
        return {"ssl_disabled": False}
    if mode == "required":
        ssl_options: dict[str, Any] = {"check_hostname": False}
        if config.mysql_ssl_ca:
            ssl_options["ca"] = config.mysql_ssl_ca
        if config.mysql_ssl_cert:
            ssl_options["cert"] = config.mysql_ssl_cert
            ssl_options["key"] = config.mysql_ssl_key
        if config.mysql_ssl_key_password:
            ssl_options["password"] = config.mysql_ssl_key_password
        return {"ssl_disabled": False, "ssl": ssl_options}

    return {
        "ssl_disabled": False,
        "ssl_ca": config.mysql_ssl_ca,
        "ssl_cert": config.mysql_ssl_cert,
        "ssl_key": config.mysql_ssl_key,
        "ssl_key_password": config.mysql_ssl_key_password,
        "ssl_verify_cert": True,
        "ssl_verify_identity": mode == "verify_identity",
    }


def _set_mysql_io_timeout(connection: Any, timeout_seconds: float) -> None:
    if timeout_seconds <= 0:
        raise TimeoutError("Database operation exceeded the approved timeout.")
    if hasattr(connection, "_read_timeout"):
        connection._read_timeout = timeout_seconds
    if hasattr(connection, "_write_timeout"):
        connection._write_timeout = timeout_seconds
    sock = getattr(connection, "_sock", None)
    settimeout = getattr(sock, "settimeout", None)
    if callable(settimeout):
        settimeout(timeout_seconds)


class MySQLAdapter:
    db_type = "mysql"

    def __init__(self, config: DbConfig):
        missing = [name for name, value in {"MYSQL_HOST": config.mysql_host, "MYSQL_USER": config.mysql_user, "MYSQL_DATABASE": config.mysql_database}.items() if not value]
        if missing:
            raise ValueError(f"Missing required MySQL environment variables: {', '.join(missing)}")
        _mysql_ssl_kwargs(config)
        self.config = config

    def connect(self, timeout_seconds: int | float | None = None) -> Any:
        import pymysql

        socket_timeout = float(timeout_seconds if timeout_seconds is not None else 30)
        if socket_timeout <= 0:
            raise ValueError("MySQL connection timeout must be greater than zero.")
        deadline = time.monotonic() + socket_timeout
        connection = pymysql.connect(
            host=self.config.mysql_host,
            port=self.config.mysql_port,
            user=self.config.mysql_user,
            password=self.config.mysql_password or "",
            database=self.config.mysql_database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.SSDictCursor,
            connect_timeout=socket_timeout,
            read_timeout=socket_timeout,
            write_timeout=socket_timeout,
            autocommit=True,
            **_mysql_ssl_kwargs(self.config),
        )
        try:
            _set_mysql_io_timeout(connection, _remaining_seconds(deadline))
            _mysql_tls_state(connection, self.config)
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
            _remaining_seconds(deadline)
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

    @contextmanager
    def stream_readonly(
        self,
        sql: str,
        max_rows: int,
        timeout_seconds: int,
        *,
        batch_size: int = DEFAULT_FETCH_BATCH_SIZE,
        expected_data_source_fingerprint: str | None = None,
    ) -> Iterator[QueryStream]:
        _assert_prepared_readonly(sql, "mysql", max_rows, timeout_seconds)
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1.")

        started = time.monotonic()
        deadline = started + timeout_seconds
        connection = self.connect(timeout_seconds=timeout_seconds)
        cursor: Any | None = None
        stream: QueryStream | None = None
        failed = False
        try:
            remaining = _remaining_seconds(deadline)
            _set_mysql_io_timeout(connection, remaining)
            fingerprint = None
            if expected_data_source_fingerprint:
                fingerprint = data_source_fingerprint(self.config, connection=connection)
                if fingerprint != expected_data_source_fingerprint.lower():
                    raise ValueError("The physical data source identity does not match the approved fingerprint.")

            remaining = _remaining_seconds(deadline)
            _set_mysql_io_timeout(connection, remaining)
            cursor = connection.cursor()
            cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (max(1, int(remaining * 1000)),))
            _remaining_seconds(deadline)
            cursor.execute(sql)
            _remaining_seconds(deadline)
            column_names = _column_names(cursor.description or [])
            stream = QueryStream(
                columns=[{"name": name, "type": "null"} for name in column_names],
                batches=iter(()),
                data_source_fingerprint=fingerprint,
            )
            stream.batches = _iter_cursor_batches(
                cursor,
                stream,
                max_rows=max_rows,
                batch_size=batch_size,
                deadline=deadline,
                row_factory=dict,
                before_fetch=lambda remaining_seconds: _set_mysql_io_timeout(connection, remaining_seconds),
            )
            yield stream
        except BaseException:
            failed = True
            raise
        finally:
            if stream is not None:
                stream.elapsed_ms = int((time.monotonic() - started) * 1000)
            abort_stream = stream is None or not stream.exhausted
            cleanup_error: Exception | None = None
            if abort_stream:
                try:
                    connection.close()
                except Exception as exc:
                    if not failed:
                        cleanup_error = exc
            if cursor is not None:
                close_cursor = getattr(cursor, "close", None)
                if callable(close_cursor):
                    try:
                        close_cursor()
                    except Exception as exc:
                        if not failed and cleanup_error is None:
                            cleanup_error = exc
            try:
                connection.close()
            except Exception as exc:
                if not failed and cleanup_error is None:
                    cleanup_error = exc
            if cleanup_error is not None:
                raise cleanup_error

    def execute_readonly(self, sql: str, max_rows: int, timeout_seconds: int) -> QueryResult:
        with self.stream_readonly(sql, max_rows, timeout_seconds) as stream:
            rows = list(stream.iter_rows())
            column_names = [column["name"] for column in stream.columns]
        return QueryResult(
            _columns_with_inferred_types(column_names, rows),
            rows,
            stream.elapsed_ms,
            stream.truncated,
        )


def get_adapter(config: DbConfig) -> DatabaseAdapter:
    if config.db_type == "sqlite":
        return SQLiteAdapter(config)
    if config.db_type == "mysql":
        return MySQLAdapter(config)
    raise ValueError(f"Unsupported DB_TYPE: {config.db_type}")


def connect(config: DbConfig) -> Any:
    return get_adapter(config).connect()

#!/usr/bin/env python3
"""Shared database helpers for GrowthInsight Agent scripts."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass
class DbConfig:
    db_type: str
    sqlite_path: str | None = None
    mysql_host: str | None = None
    mysql_port: int = 3306
    mysql_user: str | None = None
    mysql_password: str | None = None
    mysql_database: str | None = None


def load_config(db_type: str | None = None) -> DbConfig:
    resolved = (db_type or os.getenv("DB_TYPE") or "sqlite").lower()
    return DbConfig(
        db_type=resolved,
        sqlite_path=os.getenv("SQLITE_PATH"),
        mysql_host=os.getenv("MYSQL_HOST"),
        mysql_port=int(os.getenv("MYSQL_PORT", "3306")),
        mysql_user=os.getenv("MYSQL_USER"),
        mysql_password=os.getenv("MYSQL_PASSWORD"),
        mysql_database=os.getenv("MYSQL_DATABASE"),
    )


def connect(config: DbConfig) -> Any:
    if config.db_type == "sqlite":
        if not config.sqlite_path:
            raise SystemExit("SQLITE_PATH is required for sqlite connections.")
        conn = sqlite3.connect(config.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    if config.db_type == "mysql":
        try:
            import pymysql  # type: ignore
        except ImportError as exc:
            raise SystemExit("pymysql is required for MySQL. Install it with: pip install pymysql") from exc

        missing = [
            name
            for name, value in {
                "MYSQL_HOST": config.mysql_host,
                "MYSQL_USER": config.mysql_user,
                "MYSQL_DATABASE": config.mysql_database,
            }.items()
            if not value
        ]
        if missing:
            raise SystemExit(f"Missing required MySQL environment variables: {', '.join(missing)}")

        return pymysql.connect(
            host=config.mysql_host,
            port=config.mysql_port,
            user=config.mysql_user,
            password=config.mysql_password or "",
            database=config.mysql_database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            read_timeout=30,
            write_timeout=30,
        )

    raise SystemExit(f"Unsupported DB_TYPE: {config.db_type}")


#!/usr/bin/env python3
"""Inspect SQLite or MySQL schema and print Markdown tables."""

from __future__ import annotations

import argparse

from db_common import connect, load_config


def inspect_sqlite(conn) -> None:
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    for table in tables:
        print(f"## {table}\n")
        print("| Field | Type | Not Null | Default | Primary Key |")
        print("| --- | --- | --- | --- | --- |")
        cursor.execute(f"PRAGMA table_info({quote_sqlite_identifier(table)})")
        for row in cursor.fetchall():
            print(f"| {row[1]} | {row[2]} | {bool(row[3])} | {row[4] or ''} | {bool(row[5])} |")
        print()


def quote_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def inspect_mysql(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, column_name, column_type, is_nullable, column_key, column_default
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
            ORDER BY table_name, ordinal_position
            """
        )
        rows = cursor.fetchall()

    current = None
    for row in rows:
        table = row["table_name"]
        if table != current:
            current = table
            print(f"\n## {table}\n")
            print("| Field | Type | Nullable | Key | Default |")
            print("| --- | --- | --- | --- | --- |")
        print(
            f"| {row['column_name']} | {row['column_type']} | {row['is_nullable']} | "
            f"{row['column_key'] or ''} | {row['column_default'] or ''} |"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect database schema.")
    parser.add_argument("--db", choices=["sqlite", "mysql"], help="Database type.")
    args = parser.parse_args()

    config = load_config(args.db)
    conn = connect(config)
    try:
        if config.db_type == "sqlite":
            inspect_sqlite(conn)
        elif config.db_type == "mysql":
            inspect_mysql(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


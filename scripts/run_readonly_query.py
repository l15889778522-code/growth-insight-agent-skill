#!/usr/bin/env python3
"""Run a read-only SQL query after validating it."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from db_common import connect, load_config
from sql_guard import validate_sql


def read_sql(args: argparse.Namespace) -> str:
    if args.sql:
        return args.sql
    if args.sql_file:
        return Path(args.sql_file).read_text(encoding="utf-8")
    return sys.stdin.read()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a read-only SQL query.")
    parser.add_argument("--db", choices=["sqlite", "mysql"], help="Database type.")
    parser.add_argument("--sql", help="SQL string to execute.")
    parser.add_argument("--sql-file", help="Path to SQL file.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum rows to print.")
    args = parser.parse_args()

    sql = read_sql(args)
    ok, errors = validate_sql(sql)
    if not ok:
        print("Query blocked by SQL guard.", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 2

    config = load_config(args.db)
    conn = connect(config)
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchmany(args.limit)
        if not rows:
            print("No rows returned.")
            return 0

        if isinstance(rows[0], dict):
            fieldnames = list(rows[0].keys())
            writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        else:
            fieldnames = rows[0].keys() if hasattr(rows[0], "keys") else [f"col_{i+1}" for i in range(len(rows[0]))]
            writer = csv.writer(sys.stdout)
            writer.writerow(fieldnames)
            for row in rows:
                writer.writerow([row[key] for key in fieldnames] if hasattr(row, "keys") else list(row))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


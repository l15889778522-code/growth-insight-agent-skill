#!/usr/bin/env python3
"""Test a configured database connection without exposing secrets."""

from __future__ import annotations

import argparse

from db_common import connect, load_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Test database connectivity.")
    parser.add_argument("--db", choices=["sqlite", "mysql"], help="Database type.")
    args = parser.parse_args()

    config = load_config(args.db)
    conn = connect(config)
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
            print(f"OK: {config.db_type} connection works. Test result: {row}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


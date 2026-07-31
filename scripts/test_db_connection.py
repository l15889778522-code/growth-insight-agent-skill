#!/usr/bin/env python3
"""Test a configured read-only database connection without exposing secrets."""

from __future__ import annotations

import argparse
import json

from db_common import data_source_fingerprint, get_adapter, load_config
from runtime_common import utc_now


def main() -> int:
    parser = argparse.ArgumentParser(description="Test SQLite or MySQL connectivity.")
    parser.add_argument("--db", choices=["sqlite", "mysql"])
    args = parser.parse_args()

    config = load_config(args.db)
    adapter = get_adapter(config)
    value = adapter.test_connection()
    print(
        json.dumps(
            {
                "schema_version": "1.1",
                "ok": value == 1,
                "db_type": config.db_type,
                "data_source_id": config.data_source_id,
                "data_source_fingerprint": data_source_fingerprint(config),
                "checked_at": utc_now(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if value == 1 else 2


if __name__ == "__main__":
    raise SystemExit(main())

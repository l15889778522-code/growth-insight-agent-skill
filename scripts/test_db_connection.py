#!/usr/bin/env python3
"""Test a configured read-only database connection without exposing secrets."""

from __future__ import annotations

import argparse
import json
import os
import sys

from db_common import data_source_descriptor, get_adapter, load_config, redact_db_error
from runtime_common import sha256_json, utc_now


def main() -> int:
    parser = argparse.ArgumentParser(description="Test SQLite or MySQL connectivity.")
    parser.add_argument("--db", choices=["sqlite", "mysql"])
    parser.add_argument(
        "--expected-fingerprint",
        default=os.getenv("MYSQL_EXPECTED_FINGERPRINT"),
        help="Fail unless the live password-free identity descriptor hashes to this SHA-256 value.",
    )
    args = parser.parse_args()

    config = None
    try:
        config = load_config(args.db)
        adapter = get_adapter(config)
        value = adapter.test_connection()
        descriptor = data_source_descriptor(config)
        fingerprint = sha256_json(descriptor)
        expected = args.expected_fingerprint.lower() if args.expected_fingerprint else None
        fingerprint_matches = expected is None or fingerprint == expected
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": "1.2",
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": redact_db_error(exc, config),
                    "checked_at": utc_now(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            {
                "schema_version": "1.2",
                "ok": value == 1 and fingerprint_matches,
                "db_type": config.db_type,
                "data_source_id": config.data_source_id,
                "data_source_identity": descriptor,
                "data_source_fingerprint": fingerprint,
                "expected_fingerprint_matched": fingerprint_matches,
                "checked_at": utc_now(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if value == 1 and fingerprint_matches else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Inspect a configured read-only database and emit normalized schema metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from contracts import CURRENT_CONTRACT_VERSION
from db_common import data_source_fingerprint, get_adapter, load_config
from runtime_common import atomic_write_json, atomic_write_text, utc_now


def render_markdown(data: dict[str, object]) -> str:
    lines = ["# Database Schema", "", f"- data_source_id: `{data['data_source_id']}`", f"- db_type: `{data['db_type']}`", ""]
    for table in data["tables"]:
        lines.extend([f"## {table['table']}", "", "| Field | Native type | Normalized type | Nullable | Primary key |", "|---|---|---|---|---|"])
        for column in table["columns"]:
            lines.append(f"| {column['name']} | {column['native_type']} | {column['normalized_type']} | {column['nullable']} | {column['primary_key']} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect SQLite or MySQL schema through the read-only adapter.")
    parser.add_argument("--db", choices=["sqlite", "mysql"])
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    config = load_config(args.db)
    adapter = get_adapter(config)
    data = {
        "schema_version": CURRENT_CONTRACT_VERSION,
        "data_source_id": config.data_source_id,
        "data_source_fingerprint": data_source_fingerprint(config),
        "db_type": config.db_type,
        "inspected_at": utc_now(),
        "tables": adapter.inspect_schema(),
    }
    if args.format == "json":
        output = json.dumps(data, ensure_ascii=False, indent=2)
        if args.output:
            atomic_write_json(args.output, data)
    else:
        output = render_markdown(data)
        if args.output:
            atomic_write_text(args.output, output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

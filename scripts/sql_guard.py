#!/usr/bin/env python3
"""Validate that SQL is read-only enough for analysis use."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ALLOWED_STARTS = {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
BLOCKED_WORDS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "REPLACE",
    "MERGE",
    "GRANT",
    "REVOKE",
    "CALL",
    "EXEC",
    "EXECUTE",
    "LOAD",
    "COPY",
    "ATTACH",
    "DETACH",
    "VACUUM",
    "ANALYZE",
}


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n\r]*", " ", sql)
    sql = re.sub(r"#[^\n\r]*", " ", sql)
    return sql


def split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            current.append(ch)
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    current.append(sql[i + 1])
                    i += 1
                else:
                    quote = None
        else:
            if ch in {"'", '"', "`"}:
                quote = ch
                current.append(ch)
            elif ch == ";":
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
            else:
                current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def first_keyword(statement: str) -> str:
    match = re.search(r"[A-Za-z_]+", statement)
    return match.group(0).upper() if match else ""


def validate_sql(sql: str) -> tuple[bool, list[str]]:
    cleaned = strip_comments(sql)
    statements = split_statements(cleaned)
    errors: list[str] = []

    if not statements:
        return False, ["SQL is empty."]

    for index, statement in enumerate(statements, start=1):
        keyword = first_keyword(statement)
        if keyword not in ALLOWED_STARTS:
            errors.append(
                f"Statement {index} starts with {keyword or '<none>'}, not an allowed read-only statement."
            )

        tokens = {token.upper() for token in re.findall(r"\b[A-Za-z_]+\b", statement)}
        blocked = sorted(tokens & BLOCKED_WORDS)
        if blocked:
            errors.append(f"Statement {index} contains blocked keyword(s): {', '.join(blocked)}.")

    return not errors, errors


def read_sql(args: argparse.Namespace) -> str:
    if args.sql:
        return args.sql
    if args.sql_file:
        return Path(args.sql_file).read_text(encoding="utf-8")
    if args.path:
        return Path(args.path).read_text(encoding="utf-8")
    return sys.stdin.read()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate read-only SQL for GrowthInsight Agent.")
    parser.add_argument("path", nargs="?", help="Path to a SQL file.")
    parser.add_argument("--sql", help="SQL string to validate.")
    parser.add_argument("--sql-file", help="Path to a SQL file.")
    args = parser.parse_args()

    sql = read_sql(args)
    ok, errors = validate_sql(sql)
    if ok:
        print("OK: SQL passed read-only guard.")
        return 0

    print("BLOCKED: SQL failed read-only guard.", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


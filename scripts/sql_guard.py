#!/usr/bin/env python3
"""AST-based validation and bounded rewriting for read-only analytical SQL."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


BLOCKED_NODE_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Merge,
    exp.Command,
    exp.Transaction,
    exp.Grant,
    exp.Revoke,
    exp.Copy,
    exp.LoadData,
    exp.Into,
    exp.Lock,
    exp.Use,
    exp.Set,
    exp.TruncateTable,
    exp.Analyze,
)

BLOCKED_FUNCTION_NAMES = {
    "benchmark",
    "get_lock",
    "is_free_lock",
    "is_used_lock",
    "load_file",
    "master_pos_wait",
    "release_all_locks",
    "release_lock",
    "sleep",
    "sys_exec",
    "sys_eval",
}

MYSQL_EXECUTABLE_COMMENT = re.compile(r"/\*(?:!|M!)", re.IGNORECASE)


@dataclass
class SqlValidationResult:
    ok: bool
    sql: str
    errors: list[str]
    warnings: list[str]
    dialect: str
    rewritten: bool


def _read_only_root(root: exp.Expression) -> bool:
    return isinstance(root, (exp.Query, exp.Show, exp.Describe))


def _bounded_query(root: exp.Expression, max_rows: int, errors: list[str], warnings: list[str]) -> tuple[exp.Expression, bool]:
    if not isinstance(root, exp.Query):
        return root, False
    limit = root.args.get("limit")
    fetch_limit = max_rows + 1
    if limit is None:
        warnings.append(f"Added LIMIT {fetch_limit} to enforce the {max_rows}-row output cap and detect truncation.")
        return root.limit(fetch_limit), True
    expression = limit.args.get("expression")
    if not isinstance(expression, exp.Literal) or not expression.is_int:
        errors.append("LIMIT must be a numeric literal so the row bound can be verified.")
        return root, False
    current = int(expression.this)
    if current <= 0:
        errors.append("LIMIT must be greater than zero.")
        return root, False
    if current > fetch_limit:
        warnings.append(f"Reduced LIMIT from {current} to {fetch_limit} to enforce the {max_rows}-row output cap.")
        return root.limit(fetch_limit), True
    return root, False


def validate_and_rewrite(sql: str, dialect: str = "mysql", max_rows: int = 1000) -> SqlValidationResult:
    if max_rows < 1:
        return SqlValidationResult(False, sql, ["max_rows must be at least 1."], [], dialect, False)
    if not sql.strip():
        return SqlValidationResult(False, sql, ["SQL is empty."], [], dialect, False)
    if dialect.lower() in {"mysql", "mariadb"} and MYSQL_EXECUTABLE_COMMENT.search(sql):
        return SqlValidationResult(False, sql, ["MySQL executable comments are not allowed."], [], dialect, False)
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except (ParseError, ValueError) as exc:
        return SqlValidationResult(False, sql, [f"SQL parse failed: {exc}"], [], dialect, False)
    if len(statements) != 1:
        return SqlValidationResult(False, sql, [f"Exactly one statement is required; found {len(statements)}."], [], dialect, False)

    root = statements[0]
    errors: list[str] = []
    warnings: list[str] = []
    if not _read_only_root(root):
        errors.append(f"Top-level {type(root).__name__} is not an allowed read-only statement.")
    blocked = sorted({type(node).__name__ for node in root.walk() if isinstance(node, BLOCKED_NODE_TYPES)})
    if blocked:
        errors.append("Blocked SQL operation(s): " + ", ".join(blocked) + ".")
    blocked_functions: set[str] = set()
    for node in root.walk():
        if not isinstance(node, exp.Func):
            continue
        candidates = {str(node.sql_name()).lower(), str(getattr(node, "name", "")).lower()}
        blocked_functions.update(candidates & BLOCKED_FUNCTION_NAMES)
    if blocked_functions:
        errors.append("Blocked SQL function(s): " + ", ".join(sorted(blocked_functions)) + ".")
    if isinstance(root, exp.Query) and root.find(exp.Table) and not root.find(exp.Where):
        warnings.append("Query reads a table without a WHERE filter; review scan cost or run EXPLAIN first.")

    rewritten = False
    if not errors:
        root, rewritten = _bounded_query(root, max_rows, errors, warnings)
    normalized = root.sql(dialect=dialect, pretty=True) if not errors else sql.strip()
    return SqlValidationResult(not errors, normalized, errors, warnings, dialect, rewritten)


def validate_sql(sql: str, dialect: str = "mysql", max_rows: int = 1000) -> tuple[bool, list[str]]:
    result = validate_and_rewrite(sql, dialect=dialect, max_rows=max_rows)
    return result.ok, result.errors


def read_sql(args: argparse.Namespace) -> str:
    if args.sql:
        return args.sql
    if args.sql_file:
        return args.sql_file.read_text(encoding="utf-8")
    if args.path:
        return args.path.read_text(encoding="utf-8")
    return sys.stdin.read()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and safely bound one read-only SQL statement.")
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--sql")
    parser.add_argument("--sql-file", type=Path)
    parser.add_argument("--dialect", default="mysql")
    parser.add_argument("--max-rows", type=int, default=1000)
    parser.add_argument("--rewrite-out", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = validate_and_rewrite(read_sql(args), args.dialect, args.max_rows)
    if args.rewrite_out and result.ok:
        args.rewrite_out.parent.mkdir(parents=True, exist_ok=True)
        args.rewrite_out.write_text(result.sql + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    elif result.ok:
        print("OK: SQL passed AST read-only validation.")
        for warning in result.warnings:
            print(f"WARNING: {warning}")
        print(result.sql)
    else:
        print("BLOCKED: SQL failed AST read-only validation.", file=sys.stderr)
        for error in result.errors:
            print(f"- {error}", file=sys.stderr)
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

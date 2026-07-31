# SQL Standards

## Deterministic Safety

Validate one statement with `sqlglot` AST parsing:

```powershell
python scripts/sql_guard.py query.sql --dialect mysql --max-rows 1000 --json
```

Allow only query, SHOW, DESCRIBE, and EXPLAIN roots. Reject writes, DDL, transactions, grants, commands, `INTO`, locks, multiple statements, parse failures, unverifiable dynamic limits, MySQL executable comments, and risky side-effect functions such as `SLEEP`, `GET_LOCK`, and `LOAD_FILE`.

For query roots:

- add `LIMIT max_rows + 1` when absent so the adapter can detect truncation while emitting at most `max_rows`;
- reduce a numeric limit above that one-row sentinel bound;
- warn when a table query has no WHERE filter;
- use EXPLAIN or an explicit scan-risk warning before a potentially expensive query.

The SQL SHA-256 shown to the user must be calculated after safe rewriting. Any later SQL or limit change invalidates approval.

## Query Quality

- Use explicit date filters and aggregation grain.
- Exclude test users when supported.
- Exclude immature cohorts.
- Avoid raw PII and unnecessary `SELECT *`.
- Explain joins, denominator rules, null handling, and sample-size risks.
- Never invent tables or fields.

## Role Boundary

The SQL Agent proposes SQL only. The root prepares the final executable SQL, records the query request, waits for `确认执行查询：<sql_sha256>`, and invokes the deterministic runner.

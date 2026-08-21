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
- Map every approved metric or list its `metric_id` in `unsupported_metrics`; a supported metric must appear in an executable query.
- For rates, shares, ratios, and retention, document numerator grain, denominator population, time eligibility, exclusions, and zero-denominator behavior.
- Compute a shared denominator at its approved grain before joining dimensional numerators. Do not add `event_type`, channel, segment, or another display dimension to the denominator unless the metric definition explicitly requires it.
- Exclude test, internal, bot, or otherwise ineligible users when supported. When the schema cannot identify them, state that limitation in the field mapping and query risks rather than claiming an exclusion.
- Exclude immature cohorts.
- Distinguish a measured numeric zero from an absent period or insufficient source coverage. A query may return diagnostic rows for incomplete windows, but Insight and Review must not treat those rows as proof of a trend.
- Avoid raw PII and unnecessary `SELECT *`.
- Explain joins, denominator rules, null handling, and sample-size risks.
- Never invent tables or fields.

For example, when reporting event-active users as a share of all active users by `period_label` and `event_type`, calculate all active users once per `period_label`. Join that period-level denominator to event-type numerators; grouping the denominator by `event_type` changes the metric.

## Role Boundary

The SQL Agent proposes SQL only. The root prepares the final executable SQL, records the query request, waits for `确认执行查询：<sql_sha256>`, and invokes the deterministic runner.

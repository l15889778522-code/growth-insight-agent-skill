# SQL Standards

## Safety

Validate SQL with:

```bash
python scripts/sql_guard.py path/to/query.sql
```

or:

```bash
python scripts/sql_guard.py --sql "SELECT 1"
```

Do not execute live SQL before user confirmation.

## Allowed Statements

- SELECT
- WITH
- SHOW
- DESCRIBE
- EXPLAIN

## Blocked Statements

- INSERT
- UPDATE
- DELETE
- DROP
- ALTER
- CREATE
- TRUNCATE
- REPLACE
- MERGE
- GRANT
- REVOKE
- CALL
- EXEC

## Query Quality

SQL should:

- Use explicit date filters.
- Exclude test users when the schema supports it.
- Avoid selecting raw PII.
- Avoid `SELECT *` for analysis queries.
- Use clear CTE names.
- Include comments for non-obvious logic.
- Explain joins and aggregation level.
- Add `LIMIT` to exploratory detail queries.

## Schema Discipline

Do not invent tables or fields.

If required fields are missing:

- State the gap.
- Suggest a proxy metric if reasonable.
- Ask the user whether to continue with assumptions.

## Output Structure

For each query provide:

- Query name
- Business purpose
- SQL
- Required tables
- Required fields
- Grain
- Filters
- Risks


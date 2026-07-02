---
name: growth-insight-agent
description: Multi-agent data analysis workflow for Codex. Use when the user asks for business data analysis, metric framework design, SQL analysis, root-cause hypotheses, visualization planning, final analysis reports, or wants a staged data analysis team process with user confirmation gates and optional mock schema or read-only database access.
---

# GrowthInsight Agent

Use this skill to simulate a data analysis team inside Codex.

The skill turns a business analysis request into a staged, reviewable workflow:

1. Business understanding
2. Metric framework design
3. SQL analysis
4. Insight and root-cause hypotheses
5. Visualization planning
6. Review and risk assessment
7. Final report synthesis

This is not a data warehouse, ETL, BI platform, or dashboard builder skill. Do not design ODS/DWD/DWS/ADS layers, ETL pipelines, login systems, permission systems, or enterprise BI infrastructure unless the user explicitly asks.

## Required Behavior

Pause after every stage and wait for user confirmation before continuing.

Accept these user responses at each gate:

- `继续` or `确认，进入下一步`: proceed.
- `修改：...`: revise the current stage.
- `补充：...`: merge user context into the current stage.
- `重新生成这一阶段`: regenerate the current stage.
- `跳过当前阶段`: skip only if the skipped stage is not required for safety.

At the metric stage, also accept:

- `新增指标：...`
- `修改指标：...`
- `删除指标：...`
- `确认最终指标体系`

Never continue to the next role until the current role's output has been shown and confirmed.

## Quick Workflow

1. Capture the user's request in `docs/00_requirement.md` when writing project artifacts.
2. Run Business Agent and show `docs/01_business_analysis.md`.
3. Wait for confirmation.
4. Run Metrics Agent and show `docs/02_metrics_framework.md`.
5. Wait for confirmation, allowing metric additions, edits, and deletions.
6. Run SQL Agent using the final confirmed metrics and available schema/database context.
7. Validate SQL safety with `scripts/sql_guard.py` before suggesting live execution.
8. Wait for confirmation before any live database query.
9. Run Insight Agent and show `docs/04_insights.md`.
10. Wait for confirmation.
11. Run Visualization Agent and show `docs/05_visualization_plan.md`.
12. Wait for confirmation.
13. Run Review Agent and show `docs/06_review_report.md`.
14. Wait for confirmation.
15. Synthesize `docs/07_final_report.md`.

## Resource Navigation

Read only the reference files needed for the current stage:

- Stage flow and confirmation rules: `references/workflow.md`
- Agent responsibilities and output contracts: `references/agent-roles.md`
- Metric design rules: `references/metric-framework.md`
- SQL safety and query standards: `references/sql-standards.md`
- Mock schema and database connection guidance: `references/database-connectors.md`
- Final report structure: `references/report-template.md`

Use scripts when deterministic checks are useful:

- `scripts/sql_guard.py`: validate that SQL is read-only before execution.
- `scripts/inspect_schema.py`: inspect SQLite/MySQL schema from environment configuration.
- `scripts/test_db_connection.py`: test database connectivity without running analysis queries.
- `scripts/run_readonly_query.py`: execute a read-only query after user confirmation.

Use `assets/final-report-template.md` when creating the final report.

## Database Rules

Default to mock schema or user-provided schema when no database is configured.

For real databases:

- Treat all live database access as read-only.
- Never print secrets.
- Never write credentials into skill files.
- Prefer read replicas or analytics databases over production OLTP databases.
- Ask for explicit user confirmation before executing any query.
- Refuse or rewrite SQL that includes write, DDL, permission, or destructive statements.

Allowed SQL statement starts:

- `SELECT`
- `WITH`
- `SHOW`
- `DESCRIBE`
- `EXPLAIN`

Blocked SQL includes:

- `INSERT`
- `UPDATE`
- `DELETE`
- `DROP`
- `ALTER`
- `CREATE`
- `TRUNCATE`
- `REPLACE`
- `MERGE`
- `GRANT`
- `REVOKE`


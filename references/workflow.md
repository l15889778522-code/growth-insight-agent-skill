# Workflow

Use this workflow for every analysis request unless the user explicitly asks for a narrower task.

## Stage 0: Requirement Capture

Record:

- Original user request
- Business context provided by the user
- Available data sources
- Whether mock schema, uploaded schema, or live database access will be used
- Known constraints and non-goals

Output target:

```text
docs/00_requirement.md
```

## Stage 1: Business Understanding

Business Agent should produce:

- Business background
- Core business question
- Analysis objective
- Analysis population
- Analysis time window
- Key dimensions
- Assumptions
- Open questions
- Non-goals

Stop and ask the user to confirm before continuing.

## Stage 2: Metric Framework

Metrics Agent should produce an initial metric table.

Every metric should include:

- Metric name
- Metric type
- Formula
- Business meaning
- Analysis dimensions
- Required fields
- Required tables
- Whether it is required or optional
- Risk notes

Stop and allow user edits:

- Add metric
- Edit metric
- Delete metric
- Confirm final metric framework

After edits, rewrite the metric table as the final confirmed metric framework.

## Stage 3: SQL Analysis

SQL Agent should:

- Read the confirmed metric framework.
- Read mock schema, uploaded schema, or inspected database schema.
- Check whether required fields exist.
- Generate SQL for each measurable metric or analysis path.
- Explain purpose, filters, joins, grouping, and risks.
- Validate SQL with `scripts/sql_guard.py`.

Stop before live query execution.

## Stage 4: Insights

Insight Agent should produce:

- Findings, if query results are available
- Hypotheses, if query results are not available
- Dimension drill-down paths
- Verification methods
- Business interpretations
- Recommended next analysis

Stop and ask for confirmation.

## Stage 5: Visualization Plan

Visualization Agent should produce:

- Chart list
- Chart type
- Target metric
- Dimensions
- Question answered by the chart
- Suggested layout order
- Caveats

Stop and ask for confirmation.

## Stage 6: Review

Review Agent should produce:

- Pass, conditional pass, or fail
- P0/P1/P2/P3 issues
- Metric consistency review
- SQL risk review
- Data quality review
- Missing dimension review
- Report readiness recommendation

Stop and ask for confirmation.

## Stage 7: Final Report

Main Agent synthesizes the final report only after review confirmation.

Use:

```text
assets/final-report-template.md
```

Output target:

```text
docs/07_final_report.md
```


# Metric Framework

## Metric Types

- North-star metric: primary business outcome.
- Core metric: directly supports the analysis objective.
- Process metric: explains movement through the user journey.
- Guardrail metric: prevents misleading optimization.
- Diagnostic metric: helps identify root causes.

## Required Metric Fields

Use this table structure:

| Metric | Type | Formula | Business Meaning | Dimensions | Tables | Fields | Required | Risks |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |

## User Customization

When the user adds a metric, normalize it into the table.

If the user gives incomplete information, infer a reasonable formula and mark assumptions.

If the metric cannot be computed from available schema:

- Keep it in the framework.
- Mark it as "not directly computable from current schema".
- Ask whether to use a proxy metric or require additional data.

## Metric Quality Rules

Each metric should be:

- Business relevant
- Clearly defined
- SQL-verifiable
- Dimensionally decomposable
- Time-window aware
- Explicit about numerator and denominator
- Explicit about exclusions such as test users or immature cohorts

## Retention Example

For D7 retention:

```text
D7 retention = users active on day 7 after signup / new users on signup day
```

Rules:

- Exclude test users.
- Exclude cohorts that have not had seven full days to mature.
- Define whether day 7 means calendar date difference or 168-hour window.
- Segment by signup channel, device, region, and signup date when available.


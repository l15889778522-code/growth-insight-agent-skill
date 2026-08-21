# Metric Framework

## Metric Types

- `north-star`
- `core`
- `process`
- `guardrail`
- `diagnostic`

## Required Fields

Every metric contains:

```text
metric_id
name
type
business_definition
formula
grain
dimensions
time_window
field_dependencies
status
source
owner
data_risks
version
```

`status` is `exploratory | candidate | official`. `metric_id` is stable across revisions and `version` increases when the definition changes. `field_dependencies` must contain at least one concrete field or declared semantic-layer dependency, and `conflict_checks` must record either the checks performed or an explicit no-conflict result.

For a revised Metrics attempt, a new metric starts at version 1, an unchanged metric keeps its version, and any changed definition increments exactly by one. Deleted IDs disappear from the new artifact; downstream artifacts that referenced them become stale.

## User Edits

`新增指标`、`修改指标`、`删除指标` and regeneration always create a new Metrics attempt. Validate unique IDs, complete formulas, field dependencies, and conflicts before showing the revision. Do not start SQL until the user confirms the final Metrics artifact.

If a metric cannot be computed from available schema, keep it only with a visible dependency gap or propose a labeled proxy. Never invent support.

## Quality

Metrics must be business-relevant, SQL-verifiable, dimensionally decomposable, time-window aware, explicit about numerator and denominator, and explicit about exclusions. For rates and retention, define numerator, denominator population, calculation grain, eligible windows, zero-denominator behavior, minimum data coverage, and whether test/internal/automated users are identifiable and excluded.

For D7 retention, define cohort maturity and whether day seven means calendar-day difference or a 168-hour window.

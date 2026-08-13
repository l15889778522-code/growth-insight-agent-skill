# Routing Rules

## Preset Routes

| Task | Default route | Required input |
|---|---|---|
| Complete diagnosis | Business -> Metrics -> SQL -> Insight -> Visualization -> Review -> Report | Business question; schema or database before SQL |
| Metric design | Business -> Metrics -> Review | Business context and measurement goal |
| Existing metrics to SQL | Metrics -> SQL -> Review | Confirmed metric definitions, schema, and dialect |
| Analyze supplied results | Business -> Insight -> Visualization -> Review -> Report | Result file, field description, and metric definitions |
| Review only | Review | Report, SQL, metrics, or results to review |
| Final report | Review -> Report | Completed evidence-linked analysis artifacts |

## Construction Algorithm

1. Classify the task and choose the shortest preset that can answer it.
2. Record all required, provided, and missing inputs.
3. Add a prerequisite role when it can produce a missing input.
4. Otherwise request the input or define a fallback route with a visible capability limit.
5. Link each stage to explicit dependency stage IDs.
6. Require Review as an ancestor of Report.
7. Validate unique roles, contiguous sequence numbers, and acyclic dependencies.
8. Show the plan and wait for `确认路由`.

## Route Revision

A running workflow returns to `awaiting_route_confirmation` when new evidence, missing data, a skip request, or Review changes the route.

List reusable approved artifacts in `reused_approved_artifacts` with their artifact and input hashes. Approval carry-forward is legal only when the stage node, dependency hashes, and artifact hash remain unchanged.

`跳过当前阶段` is rejected when a downstream required input would be missing. Report cannot remain in a route that removes Review.
No stage may declare a skipped stage as a dependency. An `approved` stage in a revised route is legal only with a matching reusable artifact entry and an existing approval record.

## Route Plan Fields

Use `schemas/route-plan.schema.json`. Validate with:

```powershell
python scripts/validate_route_plan.py <route-plan.json>
```

Use `--require-executable` immediately before the first spawn. A structurally valid plan may still be non-executable when `missing_inputs` is non-empty.

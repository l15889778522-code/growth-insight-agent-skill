# JSON Handoff Contract

JSON is the system source of truth. Markdown is a deterministic human-readable rendering.

## Response Shape

Every subagent response must be exactly one JSON object with these common fields:

```text
schema_version
agent_contract_version
run_id
stage_id
role
attempt
stage_status
summary
confirmed_decisions
conflicts
facts
calculations
assumptions
hypotheses
evidence
metrics
data_artifacts
risks
open_questions
required_next_inputs
recommended_next_stage
lineage
role_payload
```

`stage_status` is `PASS | PASS_WITH_RISKS | BLOCKED | FAIL`.

## Rules

- Return no prose or Markdown fence outside the JSON object.
- Copy run metadata exactly from the orchestrator.
- Include every array field, using `[]` when empty.
- Use `BLOCKED` with non-empty `required_next_inputs` when essential input is missing.
- An unresolved conflict cannot return `PASS`.
- Facts, calculations, and evidence must cite resolvable files, fields, or approved artifacts.
- Metrics mirror their role payload and use unique stable IDs with deterministic version increments.
- SQL field mappings identify each metric's source fields, SQL expression, and result column.
- Insight observations and report recommendations carry explicit `metric_ids` and `evidence_refs`.
- Visualization binds `source_file` to `source_sha256`.
- Never claim a query ran without a query manifest and result hash.
- `recommended_next_stage` is advisory and never authorizes a spawn.
- Role-specific fields must satisfy `schemas/stages/<role>.schema.json`.

## Validation

The root saves `raw-response.txt`, then runs `scripts/validate_stage_output.py`. One correction is allowed. A second failure marks the stage failed and blocks all downstream roles.

After validation, `scripts/render_stage_report.py` creates Markdown. The JSON object, not the Markdown report, is passed to downstream Agents.

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
- In contract 1.2, every evidence reference contains `artifact_id`, exact artifact `sha256`, `selector_type`, and `selector_value`. Supported selectors are file, JSON Pointer, CSV row/cell, stage field, query-manifest field, and registered calculation ID.
- A calculation contains a stable `calculation_id`, expression, structured input evidence references, output value, and precision metadata.
- Metrics mirror their role payload and use unique stable IDs with deterministic version increments.
- SQL field mappings identify each metric's source fields, SQL expression, and result column.
- Insight observations and report recommendations carry explicit `metric_ids` and `evidence_refs`.
- Visualization binds `source_file` to `source_sha256`.
- Never claim a query ran without a query manifest and result hash.
- `recommended_next_stage` is advisory and never authorizes a spawn.
- Role-specific fields must satisfy `schemas/stages/<role>.schema.json`.

## Validation

The root saves `raw-response.txt`, then runs `scripts/validate_stage_output.py`. The validator defaults to the current 1.2 contract. A v1.2 run also binds both version fields to its saved runtime contract, so a structurally valid legacy 1.1 response is still rejected. Legacy outputs are accepted only by an explicit legacy validation or migration path.

Every failed attempt remains in its own attempt directory. Resuming creates a new attempt and all downstream roles remain blocked until a valid current attempt is recorded and approved; retry count is an orchestration policy, not an unstated validator shortcut.

Before `record-stage`, the root records `agent-execution-receipt.json`. The receipt binds the native Agent ID when observable, role configuration hash, attempt, raw response hash, parsed JSON hash, timestamps, model metadata, and explicit missing-metadata reasons. `record-stage` rejects a missing, reused, overwritten, or mismatched receipt.

After validation, `scripts/render_stage_report.py` creates Markdown. The JSON object, not the Markdown report, is passed to downstream Agents.

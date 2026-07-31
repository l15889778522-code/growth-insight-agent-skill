# Runtime State And Recovery

## Files

```text
multi-agent-data-analysis-runs/<run_id>/
  request.json
  route-plan.json
  run-state.json
  events.jsonl
  approvals.jsonl
  stages/<stage_id>/attempt-<n>/
  data/
  lineage/
  charts/
  final/
```

All paths in state are relative to the run directory. The root and deterministic scripts are the only writers.

## Global States

```text
initialized
awaiting_route_confirmation
running
validating
finalizing
awaiting_user_confirmation
awaiting_query_confirmation
revising
blocked
failed
stopped
completed
```

Stage states are `pending`, `running`, `validating`, `awaiting_user_confirmation`, `approved`, `revising`, `blocked`, `failed`, `stale`, `skipped`, and `completed`.

## Approval Objects

Approvals use `route | stage | query | rollback` and bind:

- run and route revision;
- subject ID and revision;
- subject SHA-256;
- original user text;
- an idempotency key.

Query approval additionally binds SQL SHA-256, data-source label, physical source fingerprint, dialect, timeout, and row limit.
It also binds the maximum result size in bytes. Canonical actions are `confirm_route`, `approve_stage`, `execute_query`, and `approve_rollback`.

A failed Review writes `rollback-plan.json`. Its rollback approval binds the Review artifact, target artifact, required fixes, route revision, and plan file hash.

## Commands

```powershell
python scripts/runctl.py init --runs-root <dir> --run-id <id> --request-file <request.json>
python scripts/runctl.py set-route --run-dir <run> --route-file <route.json>
python scripts/runctl.py start-stage --run-dir <run> --stage-id <id>
python scripts/runctl.py record-agent-runtime --run-dir <run> --stage-id <id> --thread-id <id> --model <model>
python scripts/runctl.py record-stage --run-dir <run> --stage-json <stage.json> --stage-markdown <stage.md> --validation-report <validation.json>
python scripts/runctl.py approve ...
python scripts/runctl.py revise --run-dir <run> --stage-id <id> --request <text>
python scripts/runctl.py metric-edit --run-dir <run> --edit-file <metric-edit.json>
python scripts/runctl.py stop --run-dir <run> --reason <text>
python scripts/runctl.py audit --run-dir <run>
python scripts/runctl.py recover --run-dir <run>
python scripts/runctl.py resume --run-dir <run>
python scripts/runctl.py summary --run-dir <run> --output final/run-summary.json
python scripts/runctl.py finalize --run-dir <run>
```

## Recovery

Every event contains the full post-event state snapshot and a previous/current state hash chain. Approval events also contain the immutable approval object and its hash. `audit` verifies the chain, state Schema, approval equivalence, attempt metadata, artifact paths, and hashes. `recover` discards an incomplete final event line, restores the last committed snapshot, and rebuilds `approvals.jsonl` from committed events; `resume` returns only to a legal stage boundary. Neither command reconstructs an interrupted model response or assumes an Agent completed.

The `.run.lock` file uses an operating-system byte-range lock. The OS releases ownership when a process exits; callers never remove another process's lock file.

A terminal role first enters `finalizing`. The `finalize` command verifies the final Report when routed, rebuilt metric lineage when Metrics was routed, and successful manifests for ready charts; it then writes `final/run-summary.json` and commits `completed` in one state transition.

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
- provenance containing the source surface, exact text hash, capture time, trust level, and capture method.

The portable CLI records user-message provenance as `self_asserted`. `host_observed` is reserved for native Codex tool results carrying an Agent ID, and `host_signed` is reserved for a future verifiable host receipt.

Query approval additionally binds SQL SHA-256, data-source label, physical source fingerprint, dialect, timeout, and row limit.
It also binds the maximum result size in bytes. Canonical actions are `confirm_route`, `approve_stage`, `execute_query`, and `approve_rollback`.

A failed Review writes `rollback-plan.json`. Its rollback approval binds the Review artifact, target artifact, required fixes, route revision, and plan file hash.

## Commands

```powershell
python scripts/runctl.py init --runs-root <dir> --run-id <id> --request-file <request.json>
python scripts/runctl.py set-route --run-dir <run> --route-file <route.json>
python scripts/runctl.py start-stage --run-dir <run> --stage-id <id>
python scripts/runctl.py record-agent-runtime --run-dir <run> --stage-id <id> --thread-id <id> --model <model>
python scripts/runctl.py record-agent-receipt --run-dir <run> --stage-id <id> --raw-response <raw-response.txt> --stage-json <stage.json> --agent-id <id> --capture-method codex_tool_result
python scripts/runctl.py record-stage --run-dir <run> --stage-json <stage.json> --stage-markdown <stage.md> --validation-report <validation.json>
python scripts/runctl.py approve ...
python scripts/runctl.py revise --run-dir <run> --stage-id <id> --request <text>
python scripts/runctl.py metric-edit --run-dir <run> --edit-file <metric-edit.json>
python scripts/runctl.py stop --run-dir <run> --reason <text>
python scripts/runctl.py audit --run-dir <run>
python scripts/runctl.py recover --run-dir <run>
python scripts/runctl.py recover-executions --run-dir <run>
python scripts/runctl.py migrate --run-dir <legacy-run>
python scripts/runctl.py resume --run-dir <run>
python scripts/runctl.py summary --run-dir <run>
python scripts/runctl.py publish-final-report --run-dir <run>
python scripts/runctl.py finalize --run-dir <run>
```

## Recovery

Every v1.2 event hashes the complete normalized event content and previous event in addition to the state hash chain. Event 1 and every 25th event contain a complete `state_after` checkpoint; intervening events contain deterministic `state_delta` operations. `audit` replays either encoding, continues to accept historical full-snapshot v1.1/v1.2 logs, and verifies the resulting state hash. Approval events also contain the immutable approval object and its hash. `recover` restores the replayed committed state and approvals and appends a `state_recovered` audit event; `recover-executions` either completes an atomically moved publication from its committed descriptors or aborts the interrupted lease. Neither command reconstructs an interrupted model response or assumes an Agent completed.

Queries, charts, lineage, and final-report rendering use execution leases with `prepared -> executing -> publishing -> completed/aborted`. A lease binds route revision and exact input hashes. Stop, route revision, upstream revision, timeout, or changed input prevents publication. Query requests and results live under immutable `data/queries/<query_id>/revision-<n>/` paths.

The `.run.lock` file uses an operating-system byte-range lock. The OS releases ownership when a process exits; callers never remove another process's lock file.

A user-approved Report enters `finalizing`. `publish-final-report` creates a versioned final report plus manifest bound to the approved Report, passing Review, latest lineage, and chart manifest. `finalize` refuses missing, empty, changed, or unregistered final output, rechecks consistency after summary generation, writes a versioned run summary containing the final report artifact ID, and commits `completed`.

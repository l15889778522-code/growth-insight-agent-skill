# Stateful Native-Agent Workflow

The root task owns orchestration and artifacts. Specialist work runs in visible Codex native subagent threads, one at a time.

## New Run

```text
request
  -> initialize run
  -> propose route
  -> awaiting_route_confirmation
  -> user confirms exact route revision
  -> run one role
  -> validate and render
  -> awaiting_user_confirmation
```

The initial request creates and displays a route only. It does not start Business automatically.

## Role Lifecycle

For each stage:

1. Verify every declared dependency is approved; skipped stages cannot remain as dependencies.
2. Start one named custom Agent and create a new attempt directory.
3. Pass exact approved JSON paths and hashes, never conversational memory alone.
4. Require one JSON object matching `schemas/handoff.schema.json` and the role Schema.
5. Save the raw response before parsing it.
6. Allow one correction for validation errors.
7. Record the native thread ID, resolved model, effort, elapsed time, and Token usage when available.
8. Render Markdown from validated JSON.
9. Register JSON, Markdown, validation-report hashes, and move passing work to `awaiting_user_confirmation`.
10. Show the result and end the current response.
11. Start at most one next role only after a later explicit user confirmation.

`BLOCKED` and `FAIL` are not approvable stage results. They move the run to a blocked or failed boundary for input, revision, or rollback.

Rendered stage reports are answer-first and contain only the current role's work. The first sections explain what happened, its business meaning, the key risks, and what the user is being asked to confirm. The internal `role_payload` remains unchanged in JSON, while the report uses Chinese headings and prose, never JSON dumps, raw SQL, code blocks, or inline-code formatting. Exact SQL remains in run artifacts; hashes and paths are plain-text references in the technical appendix. Formatting must not hide caveats or make a failed/blocked stage appear approvable.

Report is terminal. After its validated JSON and final Markdown are written, enter `finalizing`, rebuild required lineage, verify chart manifests, publish the immutable report, and call `runctl.py finalize`. A route ending at Review is also terminal after Review approval; call `runctl.py finalize` directly to create an immutable review-terminal summary without a final report.

## User Commands

Route gate:

- `确认路由`
- `修改路由：...`
- `终止分析`

Role gate:

- `确认，进入下一步` or `继续`
- `修改：...` or `补充：...`
- `重新生成当前阶段` or `重新生成这一阶段`
- `跳过当前阶段`
- `回退到：<角色>`
- `终止分析`

Metrics also accepts `新增指标`、`修改指标`、`删除指标` and `确认最终指标体系`.

## Query Branch

```text
SQL stage approved
  -> prepare final read-only SQL
  -> awaiting_query_confirmation
  -> user confirms exact SQL SHA-256
  -> deterministic query runner
  -> manifest + CSV + profile, bound to the same query revision and metric IDs
  -> start Insight under the stored SQL-stage approval
```

Query confirmation is separate from SQL-stage confirmation. A changed SQL request requires a new query confirmation.
The fingerprint also binds dialect, data-source label, non-secret physical source fingerprint, timeout, row limit, and maximum result bytes.
The query manifest must match the approved canonical SQL fingerprint, source SQL fingerprint, query revision, and metric IDs. Every current query request must have one matching manifest, result CSV, and result profile before Review can start.

## Revision And Review

- Revising a stage creates a new attempt and never overwrites history.
- All dependent approved artifacts become `stale`.
- A route revision can carry forward an approval only when stage identity, input hashes, and artifact hash are unchanged and listed in `reused_approved_artifacts`.
- Review `FAIL` creates a hash-bound rollback plan. Only a later `approve_rollback` action routes back to the earliest responsible stage and invalidates completed descendants.
- Retrying or revising Review does not invalidate metric lineage derived only from approved upstream stages. Revising Metrics, SQL, Insight, Visualization, or Report still invalidates affected lineage.
- Report requires Review `PASS` or `PASS_WITH_RISKS`.
- Review cannot pass while the current metric lineage contains an unresolved break. Missing query results, result columns, or chart mappings must be repaired or explicitly routed as a non-data terminal workflow.
- A Review-terminal route requires Review `PASS` or `PASS_WITH_RISKS` and does not create a final report.

## Concurrency

Only one role may be `running`. Read-heavy Agent work can be parallelized in other projects, but this Skill intentionally remains serial because every handoff requires user approval and downstream artifacts depend on exact versions.

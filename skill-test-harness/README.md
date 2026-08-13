# Skill Test Harness

This subproject exercises the v1.1 Codex-native multi-agent workflow before release. It validates the orchestration contract, not a production database or user interface.

## Test Goals

- Verify that the root task proposes and waits for approval of a dynamic route before spawning a role.
- Verify that each selected role runs as a visible native child Agent and returns schema-valid JSON.
- Verify that every Business-through-Review artifact is hash-bound and approved in a later user turn.
- Verify that Metrics supports user-added, edited, and deleted metrics through new revisions.
- Verify that SQL generation uses only the approved metric revision.
- Verify that database execution has a separate, hash-bound query approval.
- Verify that results, charts, lineage, review, and the final report remain traceable.
- Verify that audit and recovery stop at the last legal stage boundary.

## Full Diagnostic Flow

Use `test-cases/new-user-retention-drop.md` with `mock-schema/social-platform.md`. The expected route is the full diagnostic preset:

1. The root task creates and displays a route plan, then pauses.
2. The user approves the exact route revision.
3. Business runs, its artifact is displayed, and the workflow pauses.
4. Metrics runs only after Business approval.
5. The user adds the first-day follow-rate metric; Metrics creates a new revision and pauses again.
6. SQL runs only after the revised Metrics artifact is approved.
7. The SQL artifact is approved independently from any live query.
8. If live data is used, the root task displays the complete query fingerprint and waits for explicit query approval.
9. Insight, Visualization, and Review each run only after the prior artifact is approved.
10. Report runs only after an approved Review result of `PASS` or `PASS_WITH_RISKS`.
11. The completed run passes `runctl.py audit`, and its summary preserves artifact and runtime metadata.

## Pass Criteria

- No role starts before route approval or its direct predecessor's approval.
- No two roles are running at the same time.
- The user-added metric appears in the approved metric revision and downstream SQL where applicable.
- SQL is read-only, and a live query cannot run without an exact query approval.
- Claims and charts use registered evidence; missing data remains explicitly unresolved.
- Review checks metric consistency, SQL risks, missing dimensions, evidence quality, and data quality.
- A Review failure produces an approval-gated rollback plan rather than a Report.
- Repeated approvals are idempotent, and recovery does not duplicate a role attempt.

Use `checklists/workflow-checklist.md` to record the manual smoke result after restarting Codex with the v1.1 Agent definitions installed.

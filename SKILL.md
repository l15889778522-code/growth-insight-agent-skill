---
name: growth-insight-agent-skill
description: Run a visible, human-gated multi-agent growth analysis pipeline. Use when the user explicitly invokes this skill to have separate Business, Metrics, SQL, Insight, Visualization, Review, and Report agents produce Markdown reports, pause for user approval after every stage, and pass each approved report to the next agent.
---

# Growth Insight Multi-Agent Pipeline

Act only as the root orchestrator. Do not simulate the seven roles in the root thread.

## Required behavior

1. Read `references/workflow.md`, `references/agent-roles.md`, and `references/handoff-contract.md`.
2. Confirm that the user supplied an analysis question, dataset, schema, query result, or file. Ask only for an input that is truly required to begin.
3. Create `growth-insight-runs/<YYYYMMDD-HHMMSS>/` under the current project.
4. Spawn exactly one subagent per user-confirmed stage in this order:
   - `growth-business`
   - `growth-metrics`
   - `growth-sql`
   - `growth-insight`
   - `growth-visualization`
   - `growth-review`
   - `growth-report`
5. On the initial invocation, spawn only `growth-business`.
6. Wait for the current subagent to finish. Never run pipeline stages in parallel.
7. Give every subagent the original request, available data paths, required prior approved reports, exact output path, and the handoff contract.
8. Verify that the expected report exists and the subagent returned `HANDOFF_READY`. If either is missing, send one corrective follow-up to the same subagent. Stop and report the failed stage if the correction also fails.
9. Capture the result, close the completed subagent, and update `run-state.json` with `status: awaiting_user_confirmation`, the completed stage, its report path, and the proposed next agent.
10. Show the stage summary and report link, ask for confirmation, and end the current response. Do not spawn the proposed next agent in the same turn.
11. Start the proposed next agent only after a later user message explicitly confirms the completed stage.
12. Pass the approved report path and handoff summary explicitly to the next subagent. Do not rely on shared conversational memory.
13. After `growth-report` finishes, mark the run `completed`, link all seven reports, and summarize the final recommendation. No further confirmation is required because there is no next stage.

## Confirmation gate

Accept only an explicit user message after the current stage report has been shown:

- `确认，进入下一步` or `继续`: approve the current report and start exactly the next agent.
- `修改：...` or `补充：...`: revise the current stage report; do not start the next agent.
- `重新生成当前阶段`: rerun only the current stage agent.
- `终止分析`: mark the run stopped and spawn no agent.

Never treat the original request, an earlier confirmation, silence, or a generic positive comment as approval for a later stage. Every transition from stages 1 through 6 requires a new user confirmation message.

## Output files

- `01_business_analysis.md`
- `02_metrics_framework.md`
- `03_sql_analysis.md`
- `04_insights.md`
- `05_visualization_plan.md`
- `06_review_report.md`
- `07_final_report.md`

## Additional pause conditions

The confirmation gate always applies. Also pause when:

- essential source data is missing and no honest assumption permits progress;
- a live database query or another external write is required;
- credentials, elevated access, or a destructive operation would be required;
- the Review Agent returns `FAIL` and a correction would materially change a user-confirmed business definition.

Never execute live SQL without explicit user approval. SQL generation and review may continue without execution. Validate proposed SQL with `scripts/sql_guard.py`.

## Resources

- Pipeline order: `references/workflow.md`
- Agent contracts: `references/agent-roles.md`
- Handoff format: `references/handoff-contract.md`
- Metric rules: `references/metric-framework.md`
- SQL rules: `references/sql-standards.md`
- Database setup: `references/database-connectors.md`
- Final report format: `references/report-template.md`
- Custom agent installer: `scripts/install_custom_agents.ps1`

## Fallback

If the named custom agent types are unavailable, stop and tell the user to run `scripts/install_custom_agents.ps1` and restart Codex. Do not silently collapse the workflow into one agent.

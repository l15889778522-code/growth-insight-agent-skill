---
name: growth-insight-agent-skill
description: Run a visible, sequential multi-agent growth analysis pipeline. Use when the user explicitly invokes this skill to have separate Business, Metrics, SQL, Insight, Visualization, Review, and Report agents produce Markdown reports and pass each completed report to the next agent.
---

# Growth Insight Multi-Agent Pipeline

Act only as the root orchestrator. Do not simulate the seven roles in the root thread.

## Required behavior

1. Read `references/workflow.md`, `references/agent-roles.md`, and `references/handoff-contract.md`.
2. Confirm that the user supplied an analysis question, dataset, schema, query result, or file. Ask only for an input that is truly required to begin.
3. Create `growth-insight-runs/<YYYYMMDD-HHMMSS>/` under the current project.
4. Spawn exactly one subagent at a time in this order:
   - `growth-business`
   - `growth-metrics`
   - `growth-sql`
   - `growth-insight`
   - `growth-visualization`
   - `growth-review`
   - `growth-report`
5. Wait for the current subagent to finish before spawning the next one. Never run pipeline stages in parallel.
6. Give every subagent the original request, available data paths, required prior reports, exact output path, and the handoff contract.
7. Verify that the expected report exists and the subagent returned `HANDOFF_READY`. If either is missing, send one corrective follow-up to the same subagent. Stop and report the failed stage if the correction also fails.
8. Capture the result and close the completed subagent before starting the next stage. Completed agent threads remain inspectable in the app.
9. Pass the prior report path and handoff summary explicitly to the next subagent. Do not rely on shared conversational memory.
10. After `growth-report` finishes, link all seven reports and summarize the final recommendation.

## Output files

- `01_business_analysis.md`
- `02_metrics_framework.md`
- `03_sql_analysis.md`
- `04_insights.md`
- `05_visualization_plan.md`
- `06_review_report.md`
- `07_final_report.md`

## Pause conditions

Run automatically without confirmation between ordinary stages. Pause only when:

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

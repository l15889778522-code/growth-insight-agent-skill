# Sequential Workflow

The root task is an orchestrator, not an analysis agent. It creates seven visible subagent threads sequentially, with a mandatory user confirmation gate between stages.

| Stage | Custom agent type | Required input | Output file |
|---|---|---|---|
| 1 | `growth-business` | Original request and source context | `01_business_analysis.md` |
| 2 | `growth-metrics` | Stage 1 report and schema context | `02_metrics_framework.md` |
| 3 | `growth-sql` | Stages 1-2 reports and data source context | `03_sql_analysis.md` |
| 4 | `growth-insight` | Stages 1-3 reports and query results, if available | `04_insights.md` |
| 5 | `growth-visualization` | Stages 1-4 reports | `05_visualization_plan.md` |
| 6 | `growth-review` | Stages 1-5 reports | `06_review_report.md` |
| 7 | `growth-report` | Stages 1-6 reports | `07_final_report.md` |

For each stage:

1. Spawn the named custom agent.
2. Wait for completion.
3. Check the expected output file and `HANDOFF_READY` marker.
4. Send one correction message if the contract was not met.
5. Record the handoff summary.
6. Close the agent.
7. Write `run-state.json` with `status: awaiting_user_confirmation`.
8. Show the report to the user and end the response.
9. Wait for a new user message that explicitly confirms the stage.
10. Mark the report approved and include its path and summary in the next agent's prompt.

The initial request starts Stage 1 only. A confirmation starts exactly one next stage. Never use one confirmation to authorize multiple transitions.

Accepted gate commands:

- `确认，进入下一步` or `继续`
- `修改：...`
- `补充：...`
- `重新生成当前阶段`
- `终止分析`

Do not spawn later stages early. The purpose is traceable, human-approved report-to-report handoff, not parallel or unattended analysis.

The app exposes each subagent thread in the task's agent activity. The exact placement may vary by Codex app version; these are subagent threads rather than seven unrelated top-level projects.

# Growth Insight Multi-Agent Skill

This Codex skill runs a real human-gated subagent workflow for growth and business data analysis.

It does not ask one model to imitate several roles. The root task starts one custom agent at a time. Every agent writes a Markdown report, then the workflow pauses until the user approves that report before it becomes input to the next agent:

1. Business understanding
2. Metric framework
3. SQL and data-source assessment
4. Insight and attribution hypotheses
5. Visualization plan
6. Quality review
7. Final report

## Install

Install or copy this repository as a Codex skill, then install its custom agent definitions:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_custom_agents.ps1
```

Restart Codex after installation. Custom agents are loaded from `~/.codex/agents/*.toml` when Codex starts.

## Invoke

Attach a dataset, schema, query result, or business question and invoke:

```text
$growth-insight-agent-skill 分析这份数据。每个 Agent 完成后先展示报告并暂停，只有我回复“确认，进入下一步”才启动下一个 Agent。
```

The agent threads appear in Codex's subagent activity for the task. Their exact UI placement depends on the Codex app version; they are inspectable subagent threads, not seven unrelated projects.

At every gate, use one of these commands:

- `确认，进入下一步` or `继续`
- `修改：...` or `补充：...`
- `重新生成当前阶段`
- `终止分析`

## Outputs

Each run creates `growth-insight-runs/<timestamp>/` with:

```text
01_business_analysis.md
02_metrics_framework.md
03_sql_analysis.md
04_insights.md
05_visualization_plan.md
06_review_report.md
07_final_report.md
```

## Safety

- Live database access is read-only.
- SQL is checked with `scripts/sql_guard.py`.
- Live queries require explicit user confirmation.
- Reports separate evidence, assumptions, and hypotheses.
- The Review Agent returns `PASS`, `PASS_WITH_RISKS`, or `FAIL` before final synthesis.

## Repository structure

```text
SKILL.md
agents/openai.yaml
assets/custom-agents/*.toml
references/
scripts/install_custom_agents.ps1
scripts/sql_guard.py
scripts/inspect_schema.py
scripts/run_readonly_query.py
```

# Mode 2 Evaluation Prompt

- Mode: `codex-native-free`
- Case: `metric-design-custom-edit`
- Repeat: `01`

The evaluation root was instructed to use Codex native free orchestration and independently decide whether delegation was useful, how many native default subagents to spawn, how to divide work, and how to synthesize the result.

Isolation rules:

- Do not invoke or follow `multi-agent-data-analysis-skill`.
- Do not use any `growth-*` custom agent type.
- Do not use Skill routing, `runctl.py`, approval gates, stage contracts, or saved controlled-workflow artifacts.
- If delegating, use only native default subagents.
- Do not edit repository files, access a database, write SQL, or create charts.
- Do not ask follow-up questions; state necessary assumptions.
- Complete only this case, then stop.

Frozen input:

```json
{
  "business_question": "Which activation KPIs should the onboarding team use to improve durable product adoption?",
  "product_definition": {
    "product": "A web-based team project collaboration product.",
    "new_user": "A non-internal account on its first successful registration.",
    "candidate_activation_actions": [
      "create_project",
      "create_task",
      "invite_teammate",
      "assign_task"
    ],
    "available_event_fields": [
      "user_id",
      "event_name",
      "event_time",
      "project_id",
      "is_internal"
    ],
    "reporting_timezone": "Asia/Shanghai"
  },
  "user_metric_edit": {
    "operation": "add",
    "name": "D7 retained activation rate",
    "definition": "Among users who satisfy the final activation definition within 24 hours after registration, the share who perform at least one agreed core collaboration action on calendar day D0+7 in Asia/Shanghai.",
    "required_notes": [
      "Keep the denominator limited to activated new users.",
      "State how late-arriving D7 cohorts are handled.",
      "Do not invent fields beyond the supplied product definition."
    ]
  },
  "constraints": {
    "database_access": false,
    "sql_required": false,
    "chart_required": false,
    "expected_deliverable": "A concise, decision-ready KPI contract that incorporates the user metric edit."
  }
}
```

Requested output:

- Recommend a compact onboarding KPI hierarchy.
- Define business meaning, formula, window, grain, segmentation limits, and caveats.
- Incorporate the user-requested D7 retained activation rate.
- Check consistency and flag measurement gaps without inventing fields.
- Append execution metadata after completing the analytical work.

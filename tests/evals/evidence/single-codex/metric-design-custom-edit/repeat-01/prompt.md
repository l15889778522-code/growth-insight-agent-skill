# Mode 1 Evaluation Prompt

- Mode: `single-codex`
- Case: `metric-design-custom-edit`
- Repeat: `01`
- Model: `gpt-5.6-sol`
- Reasoning effort: `max`

The evaluation root was instructed to complete the analytical work directly as the only analytical Agent.

Isolation rules:

- Do not spawn or delegate to any subagent.
- Do not invoke, read, or follow `multi-agent-data-analysis-skill`.
- Do not use any `growth-*` custom Agent or role runtime.
- Do not read repository files or use web, filesystem, shell, database, SQL, chart, approval, routing, or other tools.
- Do not ask follow-up questions; state necessary assumptions.
- Do not write or edit any file.
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

- Recommend a compact onboarding KPI hierarchy selected for this business decision, not a fixed universal metric set.
- Define business meaning, formula, window, grain, segmentation limits, applicability conditions, and caveats.
- Incorporate the user-requested D7 retained activation rate without automatically declaring it a universal north-star metric.
- Check consistency and flag measurement gaps without inventing fields.
- Append execution metadata reporting whether subagents or tools were used; do not claim unobservable model or Token values.

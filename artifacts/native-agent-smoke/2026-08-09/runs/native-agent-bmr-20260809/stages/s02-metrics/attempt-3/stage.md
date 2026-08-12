# 指标体系报告

| 字段 | 值 |
|---|---|
| run_id | `native-agent-bmr-20260809` |
| stage_id | `s02-metrics` |
| role | `growth-metrics` |
| attempt | `3` |
| stage_status | `PASS` |
| agent_contract_version | `1.2` |

## 摘要

Defined candidate contracts for first activation rate and seven-day retained activation rate using only the approved business inputs and available schema context. Unknown event mappings, observation-window rules, and plan timing remain explicit dependency gaps and risks. No data access, SQL generation, or calculation was performed.

## 已确认决策

- The analysis unit is the account.
- The population is non-test accounts created from 2026-07-01 through 2026-07-31 in Asia/Shanghai.
- Both metrics are segmented by signup_cohort_day and plan.
- The user-specified metric_id seven_day_retained_activation_rate and its activated-account denominator are preserved.
- Both metric definitions remain candidate contracts until event mappings, window boundaries, and plan timing are approved.

## 冲突

无。

## 事实

无。

## 计算结果

无。

## 假设

- signup_cohort_day is derived from accounts.created_at using Asia/Shanghai.
- Eligible accounts are identified by accounts.is_test being false.
- Cohort maturity for seven-day retained activation will be enforced once the exact day-seven observation-window rule is approved.

## 待验证推断

无。

## 证据

无。

## 指标

```json
{
  "business_definition": "The percentage of eligible non-test accounts in the signup cohort that achieve a qualifying first activation within the approved activation qualification window.",
  "data_risks": [
    "The event_name value or event sequence defining first activation is unknown.",
    "The allowed interval between signup and qualifying first activation is unknown.",
    "The business timestamp at which plan should be assigned is unknown."
  ],
  "dimensions": [
    "signup_cohort_day",
    "plan"
  ],
  "field_dependencies": [
    "accounts.account_id",
    "accounts.created_at",
    "accounts.plan",
    "accounts.is_test",
    "events.account_id",
    "events.event_name",
    "events.event_time"
  ],
  "formula": "Distinct eligible accounts with a qualifying first-activation timestamp within the approved activation qualification window divided by distinct eligible accounts in the signup cohort.",
  "grain": "signup_cohort_day by plan",
  "metric_id": "first_activation_rate",
  "name": "First Activation Rate",
  "owner": "Growth/Product Analytics",
  "source": "Approved request.json and approved s01-business attempt-2 stage.json, using available schema context only.",
  "status": "candidate",
  "time_window": "Signup cohorts from 2026-07-01 through 2026-07-31 in Asia/Shanghai; the activation qualification window is not yet mapped or approved.",
  "type": "process",
  "version": 1
}
```

```json
{
  "business_definition": "Among accounts with a qualifying first activation and a complete approved day-seven observation window, the percentage that perform at least one qualifying core_action during that day-seven window.",
  "data_risks": [
    "The event_name value or event sequence defining first activation is unknown.",
    "The event_name values mapped to core_action are unknown.",
    "The exact day-seven window, including calendar-day versus elapsed-hour interpretation and boundary inclusivity, is unknown.",
    "The cohort-maturity cutoff depends on the unresolved day-seven window.",
    "The business timestamp at which plan should be assigned is unknown."
  ],
  "dimensions": [
    "signup_cohort_day",
    "plan"
  ],
  "field_dependencies": [
    "accounts.account_id",
    "accounts.created_at",
    "accounts.plan",
    "accounts.is_test",
    "events.account_id",
    "events.event_name",
    "events.event_time"
  ],
  "formula": "Distinct first-activated accounts with at least one qualifying core_action in the approved day-seven window divided by distinct first-activated accounts eligible for complete observation through that window.",
  "grain": "signup_cohort_day by plan",
  "metric_id": "seven_day_retained_activation_rate",
  "name": "Seven-Day Retained Activation Rate",
  "owner": "Growth/Product Analytics",
  "source": "Approved request.json and approved s01-business attempt-2 stage.json, using available schema context only.",
  "status": "candidate",
  "time_window": "Signup cohorts from 2026-07-01 through 2026-07-31 in Asia/Shanghai; retention is evaluated after first activation, but the exact day-seven boundary and cohort-maturity rule are not yet approved.",
  "type": "core",
  "version": 1
}
```


## 数据产物

无。

## 风险

- Available schema fields do not establish that qualifying activation or core-action mappings exist.
- Unknown first-activation and core_action event mappings prevent operational computation.
- Unknown activation and day-seven window rules can materially change numerator, denominator, and cohort maturity.
- Unknown plan timing can cause segment reassignment or temporal leakage.

## 开放问题

- Which event_name value or event sequence defines first activation?
- Which event_name values qualify as core_action?
- What qualification window applies to first activation after signup?
- Does day seven mean a calendar day, a 168-hour boundary, or another tolerance window, and which boundaries are inclusive?
- Should plan be assigned at account creation, first activation, or another observation time?

## 下一阶段必需输入

- Approved first-activation event or event-sequence mapping.
- Approved core_action event_name mapping.
- Approved first-activation qualification window.
- Approved day-seven observation-window and cohort-maturity rules.
- Approved plan assignment timing.

## 血缘

无。

## 角色专属结果

```json
{
  "conflict_checks": [
    "Confirmed metric IDs are unique and stable; both new metrics use version 1.",
    "Confirmed seven_day_retained_activation_rate is included without renaming or deletion.",
    "Confirmed both metrics contain the requested dimensions signup_cohort_day and plan.",
    "Confirmed the retained-activation numerator requires at least one core_action and its denominator remains first-activated accounts.",
    "Confirmed the population, account grain, and Asia/Shanghai timezone align with the approved Business handoff.",
    "Confirmed unknown event mappings, window boundaries, cohort maturity, and plan timing were not silently inferred and are recorded as dependency gaps and risks.",
    "No unresolved definition conflicts were found among the approved inputs."
  ],
  "dependency_gaps": [
    "First activation lacks an approved event_name or event-sequence mapping.",
    "core_action lacks an approved event_name mapping.",
    "The first-activation qualification window is undefined.",
    "The day-seven observation-window boundaries and cohort-maturity rule are undefined.",
    "The timestamp used to assign plan is undefined."
  ],
  "metrics": [
    {
      "business_definition": "The percentage of eligible non-test accounts in the signup cohort that achieve a qualifying first activation within the approved activation qualification window.",
      "data_risks": [
        "The event_name value or event sequence defining first activation is unknown.",
        "The allowed interval between signup and qualifying first activation is unknown.",
        "The business timestamp at which plan should be assigned is unknown."
      ],
      "dimensions": [
        "signup_cohort_day",
        "plan"
      ],
      "field_dependencies": [
        "accounts.account_id",
        "accounts.created_at",
        "accounts.plan",
        "accounts.is_test",
        "events.account_id",
        "events.event_name",
        "events.event_time"
      ],
      "formula": "Distinct eligible accounts with a qualifying first-activation timestamp within the approved activation qualification window divided by distinct eligible accounts in the signup cohort.",
      "grain": "signup_cohort_day by plan",
      "metric_id": "first_activation_rate",
      "name": "First Activation Rate",
      "owner": "Growth/Product Analytics",
      "source": "Approved request.json and approved s01-business attempt-2 stage.json, using available schema context only.",
      "status": "candidate",
      "time_window": "Signup cohorts from 2026-07-01 through 2026-07-31 in Asia/Shanghai; the activation qualification window is not yet mapped or approved.",
      "type": "process",
      "version": 1
    },
    {
      "business_definition": "Among accounts with a qualifying first activation and a complete approved day-seven observation window, the percentage that perform at least one qualifying core_action during that day-seven window.",
      "data_risks": [
        "The event_name value or event sequence defining first activation is unknown.",
        "The event_name values mapped to core_action are unknown.",
        "The exact day-seven window, including calendar-day versus elapsed-hour interpretation and boundary inclusivity, is unknown.",
        "The cohort-maturity cutoff depends on the unresolved day-seven window.",
        "The business timestamp at which plan should be assigned is unknown."
      ],
      "dimensions": [
        "signup_cohort_day",
        "plan"
      ],
      "field_dependencies": [
        "accounts.account_id",
        "accounts.created_at",
        "accounts.plan",
        "accounts.is_test",
        "events.account_id",
        "events.event_name",
        "events.event_time"
      ],
      "formula": "Distinct first-activated accounts with at least one qualifying core_action in the approved day-seven window divided by distinct first-activated accounts eligible for complete observation through that window.",
      "grain": "signup_cohort_day by plan",
      "metric_id": "seven_day_retained_activation_rate",
      "name": "Seven-Day Retained Activation Rate",
      "owner": "Growth/Product Analytics",
      "source": "Approved request.json and approved s01-business attempt-2 stage.json, using available schema context only.",
      "status": "candidate",
      "time_window": "Signup cohorts from 2026-07-01 through 2026-07-31 in Asia/Shanghai; retention is evaluated after first activation, but the exact day-seven boundary and cohort-maturity rule are not yet approved.",
      "type": "core",
      "version": 1
    }
  ]
}
```

## Handoff

- stage_status: `PASS`
- approval_status: `awaiting_user_confirmation`
- recommended_next_stage: `growth-review`
- required_next_inputs: `5` item(s)

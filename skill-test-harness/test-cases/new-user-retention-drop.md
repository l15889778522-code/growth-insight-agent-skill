# Test Case: New User D7 Retention Drop

## User Request

```text
最近社交平台新用户 7 日留存下降，请分析可能原因，并设计分析方案。
```

## Required Manual Interaction

After the Metrics Agent provides the initial metric framework, simulate this user revision:

```text
新增指标：首日关注率
业务含义：衡量新用户注册首日是否建立社交关系。
计算口径：注册当日完成 follow 事件的新用户数 / 注册当日新用户数。
分析维度：signup_channel、device_type、region、signup_date。
```

## Expected Agent Behavior

Business Agent should identify:

- D7 retention drop as the core business problem.
- New user cohorts as the analysis population.
- Channel, device, region, signup date, activation behavior, content consumption, and social interaction as key dimensions.

Metrics Agent should include:

- D1 retention rate
- D7 retention rate
- New user activation rate
- Content consumption depth
- First-day follow rate after the user adds it
- Guardrail metrics such as sample size and data completeness

SQL Agent should generate read-only SQL for:

- Overall D7 retention trend
- D7 retention by acquisition channel
- First-day activation behavior
- First-day follow rate
- Content consumption depth

Review Agent should check:

- Whether recent cohorts are incorrectly included in D7 retention.
- Whether test users are excluded.
- Whether channel null values are handled.
- Whether sample sizes are sufficient.
- Whether the custom first-day follow rate is reflected in SQL and insights.


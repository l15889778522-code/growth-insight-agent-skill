**Recommendation**

Use a decision-specific KPI tree:

1. **Primary outcome:** D7 durable-adoption yield.
2. **Drivers:** 24h activation rate and D7 retained activation rate.
3. **Diagnostics:** 24h action reach and activation latency.

The requested D7 retained activation rate is a quality indicator for activated users, not a universal north-star metric. It can rise merely because fewer, higher-intent users qualify as activated.

**Metric Definitions**

Let:

- `t0(u)` be user `u`’s first successful registration time.
- `D0(u)` be the calendar date containing `t0(u)` in `Asia/Shanghai`.
- `N_c` be distinct non-internal new users registered in cohort `c`.
- Work actions `W = {create_project, create_task}`.
- Coordination actions `K = {invite_teammate, assign_task}`.
- D7 core actions `C = {create_task, assign_task}`.

**Final activation definition v1:** A user activates when, within `[t0, t0 + 24 hours)`, they perform at least one action from `W` and one from `K` with the same non-null `project_id`. Event order is unrestricted.

This paired milestone accommodates both creating a project and contributing to an existing project. It excludes isolated setup activity.

| KPI | Business meaning | Formula and window | Grain and use |
|---|---|---|---|
| **D7 durable-adoption yield** | Share of all new users who activate promptly and return for meaningful work | `|R_c| / |N_c|`, where `R_c` contains activated users performing a `C` action on local calendar day `D0+7` | User binary, aggregated by registration cohort. Primary onboarding decision KPI |
| **24h activation rate** | Reach of the activation experience | `|A_c| / |N_c|` using activation definition v1 | Leading driver; reveals whether onboarding gets enough users to the milestone |
| **D7 retained activation rate** | Quality and durability of users reached by activation | `|R_c| / |A_c|` | Denominator is strictly activated new users, as requested. Undefined when `|A_c|=0` |
| **24h action reach** | Location of onboarding friction | For each candidate action `a`: distinct users performing `a` within 24 hours divided by `|N_c|` | Diagnostic only; rates must not be summed |
| **Activation latency** | Speed at which successful users reach activation | Median elapsed time from `t0` to completion of the paired activation condition, among `A_c` | Diagnostic only because it excludes non-activators |

For D7, the observation window is:

`[00:00 on D0+7, 00:00 on D0+8)` in `Asia/Shanghai`.

Any qualifying project may be used on D7; it need not be the project involved in activation.

**Decision Rule**

Optimize D7 durable-adoption yield. Use its exact decomposition:

`D7 durable-adoption yield = 24h activation rate × D7 retained activation rate`

This identity must hold for the same mature cohort. An onboarding change should not be judged successful from retained activation alone if total durable-adoption yield declines.

**Reporting And Segmentation**

- Count each `user_id` once per cohort and show numerator, denominator, and rate.
- Aggregate weekly results by summing user counts, not averaging daily percentages.
- Permitted breakdowns are registration cohort and qualifying event/action path.
- Use `project_id` for same-project matching and data-quality checks, not as a general business segment.
- Acquisition source, device, geography, plan, role, team, experiment variant, invite acceptance, and assignee type are unavailable and must not be inferred.

**Late Cohorts**

A cohort is reportable only after its complete local D7 window has closed and the documented event-latency buffer has elapsed. Immature cohorts remain **pending**, rather than being counted as zero.

No latency buffer is supplied. Until one is established from the event pipeline SLA, results must be labeled preliminary. Late registration, activation, or D7 events should trigger full cohort backfills and a revised status; they must not be assigned to the current reporting cohort.

**Assumptions And Gaps**

- `event_name` can identify successful registration, although its exact taxonomy value was not supplied.
- `user_id` is a stable identifier for the registered account and identifies the actor of subsequent events.
- The source timezone and semantics of `event_time` are known before conversion to `Asia/Shanghai`.
- Candidate actions reliably populate `project_id`; otherwise activation v1 cannot be evaluated as defined.
- `assign_task` does not reveal whether a task was assigned to oneself or a teammate, and `invite_teammate` does not show acceptance.
- The supplied actions emphasize creation and administration. Collaborator-only adoption, such as viewing, commenting, completing, or responding, is not measurable.
- One D7 action is point retention, not proof of long-term or team-level adoption. Activation v1 should be versioned and later validated against longer-term outcomes.
- Descriptive cohort movement cannot establish causal onboarding impact because no experiment assignment field is available.

**Execution Metadata**

- Mode: `single-codex`
- Subagents used: No
- Tools, files, web, database, SQL, or charts used: No
- Model and token values: Not reported because they are not observable here.

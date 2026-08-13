# Native-Free Raw Response

**Recommendation**

Adopt a strict initial activation milestone that represents a complete collaboration setup:

> A new user is activated when, within 24 elapsed hours of first successful registration, they perform `create_project`, `create_task`, `invite_teammate`, and `assign_task` for the same non-null `project_id`.

Use `create_task` or `assign_task` as the agreed D7 core collaboration action. These represent ongoing project execution; project creation and invitation are primarily setup actions.

**KPI Contract**

| Hierarchy | KPI | Business definition and formula | Window | Grain and segments | Caveats |
|---|---|---|---|---|---|
| Outcome | **D7 retained activation rate** | Activated new users performing `create_task` or `assign_task` on D7 ÷ activated new users eligible for D7 measurement. The denominator remains strictly limited to activated new users. | Activation: `[registration, registration + 24h)`. D7: the full Asia/Shanghai calendar date seven dates after registration date. | User-level flag; report by registration cohort week and overall. | Conditional on activation, so it can improve while total activation falls. Does not prove another teammate participated. |
| Primary driver | **24-hour collaborative activation rate** | New users satisfying the complete activation definition ÷ eligible new users whose 24-hour window has closed. | First 24 elapsed hours after registration. | User-level flag; daily cohorts, aggregated weekly. | The four-action threshold is an initial hypothesis and should eventually be validated against longer-term adoption. |
| Speed diagnostic | **Activation completion time, P50/P75** | P50 and P75 of `activation_completion_time − registration_time`, where completion time is when the last required action first makes the user activated. | Censored at 24 hours. | Activated user; weekly registration cohort. | Excludes non-activated users and therefore must never replace activation rate. |
| Funnel diagnostics | **24-hour action reach rates** | For each candidate action: unique eligible new users performing that action within 24 hours ÷ eligible new users whose window has closed. | First 24 elapsed hours. | User-action flag; weekly registration cohort. | These are parallel reach rates, not an ordered funnel. Use them to locate the action associated with activation loss. |

Segmentation should be limited to overall results and registration cohort day or week. The supplied fields do not support defensible cuts by acquisition channel, device, geography, role, organization, or team size. `project_id` should not be used as a user segment because users may have multiple projects.

**Decision Rule**

Judge onboarding changes on the pair of primary metrics:

- Prefer changes that increase D7 retained activation without reducing 24-hour activation.
- Use the all-new-user consistency check
  `durable adoption yield = activation rate × D7 retained activation rate = D7 retained activated users ÷ eligible new users`.
- Use action reach and activation time only to diagnose why the primary metrics moved.

**Cohort And Lateness Policy**

- A 24-hour cohort is excluded until every included user’s 24-hour window has closed.
- A D7 cohort is excluded until its complete D7 calendar day has ended in Asia/Shanghai; partial D7 days are never reported.
- Mark D7 results provisional for 48 hours after D7 closes and recompute them from the full event history to absorb late events.
- Finalize after that buffer. Later backfills should restate affected cohorts.
- Because no ingestion timestamp is supplied, event lateness cannot be identified or audited directly; the 48-hour buffer is an operating assumption that requires later validation.

**Measurement Gaps**

- No canonical successful-registration event name or success indicator is specified.
- There is no invitee or assignee identifier, invitation acceptance status, or workspace identifier. Consequently, activation and D7 behavior are collaboration proxies, not proof of multi-user adoption.
- The contract depends on `project_id` being populated consistently for all four activation actions.
- There is no event identifier or ingestion timestamp for deduplication and lateness monitoring. User-level existence metrics reduce duplicate sensitivity but do not solve it.
- The meaning of `user_id` on `assign_task` must be the actor, not the assignee.
- `is_internal` is treated as eligibility at registration; mutable or event-specific semantics would require clarification.

**Assumptions**

1. A canonical successful-registration event can be identified, with its earliest occurrence defining registration.
2. `user_id` identifies the acting user on candidate action events.
3. Candidate action events represent successful completed actions.
4. `project_id` is stable and non-null for those actions.
5. `event_time` is the occurrence timestamp and can be converted to Asia/Shanghai.
6. A 48-hour D7 finalization buffer is initially adequate.

EVALUATION_METADATA

```json
{
  "mode": "codex-native-free",
  "case_id": "metric-design-custom-edit",
  "completed": true,
  "logical_roles_performed": [
    "growth-business",
    "growth-metrics",
    "growth-review"
  ],
  "native_agents_spawned": [],
  "orchestration_summary": "Completed directly without delegation: framed the onboarding decision, defined the KPI hierarchy and custom D7 metric, then reviewed denominator consistency, cohort maturity, segmentation limits, and field-level measurement gaps.",
  "assumptions_count": 6
}
```

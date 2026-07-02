# Workflow Checklist

Use this checklist to manually validate a GrowthInsight Agent Skill run.

## Stage Gates

- [ ] Business Agent output was shown to the user.
- [ ] Workflow paused before Metrics Agent.
- [ ] Metrics Agent output was shown to the user.
- [ ] User was able to add, edit, or delete metrics.
- [ ] Workflow paused before SQL Agent.
- [ ] SQL Agent used the final confirmed metrics.
- [ ] Workflow paused before Insight Agent.
- [ ] Insight Agent output was shown to the user.
- [ ] Workflow paused before Visualization Agent.
- [ ] Visualization Agent output was shown to the user.
- [ ] Workflow paused before Review Agent.
- [ ] Review Agent output was shown to the user.
- [ ] Final report was generated only after review confirmation.

## SQL Safety

- [ ] SQL is read-only.
- [ ] SQL does not include INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, REPLACE, MERGE, GRANT, or REVOKE.
- [ ] Queries include date filters where appropriate.
- [ ] Large exploratory queries include LIMIT where appropriate.
- [ ] D7 retention excludes cohorts that have not had seven full days to mature.

## Report Completeness

- [ ] Business background
- [ ] Analysis objective
- [ ] Analysis scope and non-goals
- [ ] Confirmed metric framework
- [ ] Data source and schema assumptions
- [ ] SQL query plan
- [ ] Key insights
- [ ] Root-cause hypotheses
- [ ] Visualization plan
- [ ] Data quality risks
- [ ] Review comments
- [ ] Next-step recommendations


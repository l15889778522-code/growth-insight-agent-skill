# Skill Test Harness

This subproject is used to test GrowthInsight Agent Skill before it is packaged as a formal Codex skill.

It focuses on workflow correctness rather than UI or production database integration.

## Test Goals

- Verify that each agent stage produces a clear intermediate artifact.
- Verify that the workflow pauses after each agent for user confirmation.
- Verify that the metric stage supports user-added, edited, and deleted metrics.
- Verify that SQL generation uses the final confirmed metric framework.
- Verify that database-related SQL follows read-only safety rules.
- Verify that the final report reflects all confirmed stage outputs.

## Test Flow

Use the sample business request in `test-cases/new-user-retention-drop.md`.

Expected workflow:

1. Business Agent creates a business problem decomposition.
2. User confirms or revises the business decomposition.
3. Metrics Agent creates an initial metric framework.
4. User adds at least one custom metric.
5. Metrics Agent merges the custom metric into the final metric framework.
6. SQL Agent generates read-only SQL based on the final metric framework and `mock-schema/social-platform.md`.
7. User confirms or revises the SQL direction.
8. Insight Agent generates hypotheses and drill-down logic.
9. Visualization Agent proposes charts and report structure.
10. Review Agent identifies risks and gives a pass decision.
11. Main Agent writes the final report.

## Pass Criteria

The test passes if:

- No stage proceeds before user confirmation.
- The user-added metric appears in the final metric framework.
- The SQL stage references the user-added metric where applicable.
- SQL statements are read-only.
- The review stage comments on metric consistency, SQL risks, missing dimensions, and data quality risks.
- The final report includes business background, objective, metrics, SQL plan, insights, visualization plan, risks, review comments, and next steps.


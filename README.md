# GrowthInsight Agent Skill

GrowthInsight Agent Skill is a planned Codex skill for simulating a multi-agent data analysis team.

The skill is designed to guide Codex through a staged data analysis workflow:

1. Business understanding
2. Metric framework design
3. SQL analysis
4. Insight generation
5. Visualization planning
6. Review and quality assessment
7. Final report synthesis

The project is not a BI platform, data warehouse, ETL system, or reporting factory. Its focus is controllable, reviewable data analysis workflow automation.

## Current Version

The current requirement baseline is:

- [Requirement v1.0](docs/requirement-v1.0.md)

## v1.0 Highlights

- Codex skill is the final target form.
- Mock schema and real read-only database interfaces are both in scope.
- Each agent stage must pause for user confirmation before the next stage starts.
- The metric design stage supports user-added, edited, and deleted metrics.
- Real database access must be read-only by default.

## Test Subproject

The `skill-test-harness/` folder contains the first test subproject for validating the skill workflow before the formal skill is generated.


# Agent Registry

The root Codex task coordinates seven custom agents installed under `~/.codex/agents/`.

| Agent type | Responsibility | Must not do |
|---|---|---|
| `growth-business` | Define the business problem, population, scope, and decision | Finalize metrics or SQL |
| `growth-metrics` | Define formulas, dimensions, grain, and data dependencies | Invent unsupported fields |
| `growth-sql` | Map metrics to schema and produce safe read-only SQL | Execute live SQL without approval |
| `growth-insight` | Separate observations, hypotheses, and validation paths | Overstate causality |
| `growth-visualization` | Design evidence-linked charts and report order | Add decorative charts |
| `growth-review` | Audit correctness, safety, completeness, and evidence strength | Silently rewrite confirmed definitions |
| `growth-report` | Synthesize the final decision-ready report | Hide review caveats or invent results |

The source configurations live in `assets/custom-agents/`. Install them with `scripts/install_custom_agents.ps1`, then restart Codex so the custom agent types become available.

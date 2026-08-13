# Three-Mode Comparison: metric-design-custom-edit

Generated: 2026-08-13

## Executive result

This is a one-case baseline, not a general model ranking.

| Mode | Run status | Blind quality | Wall time | Main result |
|---|---|---:|---:|---|
| Single Codex Agent | Completed | 0.961 | 256.399 s | Best concise decision contract |
| Codex native free orchestration | Completed | 0.949 | 262.402 s | Close second; no descendant Agent was used |
| Controlled Skill | Incomplete | 0.799 descriptive only | 3120.054 s | Best auditability, but finalization failed |

The repository aggregator formally includes quality only for completed runs. Therefore the Controlled Skill score is retained as a blind-review observation but excluded from completed-run aggregate quality and coverage.

## Blind quality scores

Two independent reviewers scored neutral candidates in opposite presentation order. Final values are the arithmetic mean after correcting the blind bundle with deterministic evidence-verification facts.

| Mode | Business coverage | Metric consistency | Route quality | SQL handling | Evidence traceability | Overall |
|---|---:|---:|---:|---:|---:|---:|
| Single Codex Agent | 0.970 | 0.955 | 0.970 | 1.000 | 0.910 | **0.961** |
| Codex native free | 0.945 | 0.955 | 0.950 | 1.000 | 0.895 | **0.949** |
| Controlled Skill | 0.740 | 0.880 | 0.405 | 1.000 | **0.970** | **0.799** |

Both reviewers independently ranked the candidates in the same order.

## Interpretation

### Single Codex Agent

The single Agent produced the strongest decision hierarchy and the shortest usable KPI contract. It explicitly optimized all-new-user durable-adoption yield, preserved the requested activated-user denominator, and connected activation rate with conditional D7 retention.

Its main weakness is that the paired activation rule and D7 action set are analytical proposals, not approved facts. Evidence is hash-bound at file level but lacks claim-level structured references.

### Codex native free orchestration

The native-free response was close to the single Agent in quality and speed. It proposed a stricter four-action activation threshold and handled cohort maturity and late arrivals clearly.

No descendant Agent was actually used, so this case provides no evidence that free multi-Agent orchestration improved the result. The unverified Mode 2 model identity also prevents claiming exact model equivalence.

### Controlled Skill

The controlled workflow was strongest in evidence traceability: 75 structured references resolved, and schemas, receipts, approvals, lineage, and the hash-chain audit all passed.

It lost substantial value in three areas:

1. The terminal Business -> Metrics -> Review route could not complete because finalization required a Report stage that the approved route intentionally omitted.
2. The KPI contract remained long and left the final activation rule and D7 action set unresolved.
3. Review required correction after a Windows path-rendering misdiagnosis, followed by a second attempt to restore omitted approved decisions.

The 52-minute wall time includes human confirmation and repair delays, so it must not be interpreted as pure model latency. It still shows that the current workflow is too heavy for a simple metric-design task.

## Product conclusion

For this simple case, the current Skill does not beat a single Codex Agent on answer quality or completion efficiency. Its demonstrated advantage is auditability and controlled approval, not analytical quality.

The next implementation priorities are:

1. Allow Review-terminal routes to finalize without a Report while still producing an immutable run summary.
2. Add the human-editable `metrics-workbench.toml` export/import and confirmed diff workflow.
3. Supply Review with normalized structured inputs and a deterministic approved-decision bundle.
4. Produce a compact user-facing Metrics summary separate from the full audit artifact.
5. Rerun the Controlled Skill as a post-fix repeat before making broader claims.

## Limits

- Only one of eight cases was attempted in each mode.
- The Controlled Skill run is incomplete and excluded from formal completed-run aggregation.
- Each mode has one repeat, so no variance estimate is available.
- Token usage is unavailable for all modes.
- Mode 2 did not expose a verifiable model identity.
- No empirical data was available to validate any proposed activation definition.

# v1.1 Workflow Checklist

Use this checklist for a manual Codex-native smoke run.

## Installation And Route

- [ ] Skill and seven Agent manifests match their installed SHA-256 values.
- [ ] Codex was restarted after installation or update.
- [ ] Runtime preflight passes and a child response reports `agent_contract_version: 1.1`.
- [ ] The proposed route lists required, provided, and missing inputs.
- [ ] No role starts before the exact route revision is confirmed.
- [ ] Each selected role appears as a separate visible child Agent task.

## Stage Gates

- [ ] Every Business-through-Review result is displayed before its approval.
- [ ] Each approval binds the current run, route revision, stage, artifact revision, and SHA-256.
- [ ] A downstream role reads only approved upstream artifact versions.
- [ ] Only one role is running at a time.
- [ ] A repeated approval does not create another attempt.
- [ ] Report starts only after an approved Review result of `PASS` or `PASS_WITH_RISKS`.

## Metrics And SQL

- [ ] The user can add, edit, or delete a metric and receives a new Metrics revision.
- [ ] Stable metrics retain `metric_id`; changed metrics increment `version`.
- [ ] Downstream artifacts become stale after an approved upstream metric changes.
- [ ] SQL uses the final approved metrics and is accepted by the AST guard.
- [ ] Live execution requires a separate approval of SQL, source, dialect, timeout, row limit, and byte limit.
- [ ] Changing any query-bound value invalidates the prior query approval.
- [ ] D7 retention excludes cohorts without seven full days of maturity.

## Evidence And Recovery

- [ ] Uploaded evidence is ingested and hash-registered before a child Agent reads it.
- [ ] Query results have a manifest, profile, row count, field list, and SHA-256.
- [ ] PNG or HTML charts are generated from registered results and record their source hash.
- [ ] Metric lineage connects metrics, SQL, results, insights, charts, and recommendations.
- [ ] Missing results produce hypotheses or blocked chart specs, not fabricated findings.
- [ ] Review `FAIL` creates a hash-bound rollback plan and never starts Report.
- [ ] `runctl.py audit` validates the event hash chain and current state.
- [ ] `runctl.py recover` restores the last legal boundary without duplicating work.

## Final Report

- [ ] Business context, objective, scope, and confirmed metrics are present.
- [ ] Evidence-backed findings are separated from hypotheses and caveats.
- [ ] SQL and data-source assumptions are traceable.
- [ ] Review risks are preserved without dilution.
- [ ] Recommendations include evidence references and next validation steps.
- [ ] `final/run-summary.json` records artifacts and available runtime metadata without invented Token counts.

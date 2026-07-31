# Evaluation Suite

`cases.json` is the fixed v1.1 comparison set. Run each case in three modes:

1. `single-codex`: one Codex task without this Skill;
2. `v1.0`: the fixed seven-role v1.0 workflow;
3. `v1.1`: the native, dynamically routed workflow.

Store one record per completed case:

```text
tests/evals/results/<mode>/<case_id>.json
```

Each record must satisfy `schemas/evaluation-record.schema.json`. Rule checks should come from saved artifacts and logs. Quality scores require a reviewer using the same rubric across all three modes. Record actual elapsed time and Token counts only when the runtime exposes them; otherwise use `null`.

The aggregator deliberately leaves absent records as `not_run`:

```powershell
python scripts/run_evals.py `
  --results-root tests/evals/results `
  --output-json tests/evals/evaluation-results.json `
  --output-markdown tests/evals/evaluation-results.md
```

Compare quality or cost only at equal case coverage. Do not treat a missing run as either a pass or a failure.

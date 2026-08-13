# Evaluation Suite

`cases.json` is the fixed v1.2 comparison set. Execute every case with the same input snapshot, timeout, repeat count, and approved model profile in three modes:

1. `single-codex`: one Codex task without this Skill;
2. `v1.0`: the fixed seven-role v1.0 workflow;
3. `v1.2`: the current native, dynamically routed workflow.

For v1.2, derive rule checks from an actual run rather than filling them by hand:

```powershell
python scripts/capture_eval_record.py `
  --run-dir runs/<run-id> `
  --case-id full-diagnosis-sqlite `
  --output tests/evals/results/v1.2/full-diagnosis-sqlite/repeat-01.json
```

The capture command recalculates Schema validity, SQL safety, evidence resolution, approval and Agent receipt gates, and recovery evidence. It records source paths and SHA-256 values in `rule_evidence` and the event/run hashes in `provenance`. A missing blind quality review or runtime Token field remains `null`; it is never treated as zero or as a pass.

Use `quality-rubric.json` for mode-blind review. A quality-review JSON supplied through `--quality-review` must name its rubric version, set `blind_to_mode` to `true`, identify reviewers, provide all five scores and rationales, and retain disagreements.

Store repeats under `tests/evals/results/<mode>/<case_id>/*.json`. A legacy single record at `tests/evals/results/<mode>/<case_id>.json` is also accepted. Aggregate all available records with:

```powershell
python scripts/run_evals.py `
  --results-root tests/evals/results `
  --output-json tests/evals/evaluation-results.json `
  --output-markdown tests/evals/evaluation-results.md
```

The aggregator reports coverage, success rate, measured quality and Token counts, and standard deviation when at least two repeats exist. Compare modes only at equal case and measurement coverage. External adapters for `single-codex` and `v1.0` must still emit records satisfying `schemas/evaluation-record.schema.json`; they may not claim `run_artifacts` provenance unless the referenced artifacts exist.

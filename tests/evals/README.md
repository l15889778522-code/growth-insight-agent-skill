# Evaluation Suite

`cases.json` is the fixed comparison set. Execute a case with the same input snapshot, timeout, repeat count, and approved model profile in three formal modes:

1. `single-codex`: one Codex task without this Skill;
2. `codex-native-free`: Codex decides whether and how to use native subagents, without this Skill's roles, approvals, contracts, or runtime;
3. `controlled-skill`: the current native, dynamically routed, human-gated Skill workflow.

Historical `v1.0`, `v1.1`, and `v1.2` record values remain Schema-readable for archival compatibility, but the aggregator excludes them from the formal comparison. Run only one mode at a time after explicit user authorization. Do not compare modes until they have equal case and measurement coverage.

For `controlled-skill`, derive rule checks from an actual run rather than filling them by hand:

```powershell
python scripts/capture_eval_record.py `
  --run-dir runs/<run-id> `
  --case-id full-diagnosis-sqlite `
  --output tests/evals/results/controlled-skill/full-diagnosis-sqlite/repeat-01.json
```

The capture command recalculates Schema validity, SQL safety, evidence resolution, approval and Agent receipt gates, and recovery evidence. It records source paths and SHA-256 values in `rule_evidence` and the event/run hashes in `provenance`. For `single-codex` and `codex-native-free`, use `capture_external_eval_record.py` with a saved execution manifest and raw evidence directory. A missing blind quality review or runtime Token field remains `null`; it is never treated as zero or as a pass.

Use `quality-rubric.json` for mode-blind review. A quality-review JSON supplied through `--quality-review` must name its rubric version, set `blind_to_mode` to `true`, identify reviewers, provide all five scores and rationales, and retain disagreements.

Store repeats under `tests/evals/results/<mode>/<case_id>/*.json`. A legacy single record at `tests/evals/results/<mode>/<case_id>.json` is also accepted. Aggregate all available records with:

```powershell
python scripts/run_evals.py `
  --results-root tests/evals/results `
  --output-json tests/evals/evaluation-results.json `
  --output-markdown tests/evals/evaluation-results.md
```

The aggregator reports coverage, success rate, measured quality and Token counts, and standard deviation when at least two repeats exist. Compare modes only at equal case and measurement coverage. External adapters must emit records satisfying `schemas/evaluation-record.schema.json`; they may not claim `run_artifacts` provenance unless the referenced artifacts exist.

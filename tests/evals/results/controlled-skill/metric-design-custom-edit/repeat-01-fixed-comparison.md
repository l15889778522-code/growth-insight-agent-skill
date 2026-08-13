# Controlled Skill Mode 3: Before and After

## Scope

- Case: `metric-design-custom-edit`
- Frozen request SHA-256: `bf8fb064db57b37aafdbbaf8788dc87d976171c8af3b1fb8a117dc08acaae572`
- Frozen route SHA-256: `4f747cc20eae26666b83f4dc82082d308676263b8c7acdfb6c224e690d461ea0`
- Required route: `growth-business -> growth-metrics -> growth-review`
- No SQL, database, chart, or Report stage is part of this case.

## Outcome

| Dimension | Before fix | After fix |
|---|---|---|
| Run | `eval-controlled-skill-metric-design-custom-edit-r01` | `eval-controlled-skill-metric-design-custom-edit-r02` |
| Final status | `finalizing` | `completed` |
| Completion kind | `null` | `review_terminal` |
| Report required | Yes, incorrectly | No |
| Report artifact | Missing, which blocked completion | Not generated, as intended |
| Audit errors | None | None |
| Route and stage gates | Passed through Review | Passed through Review and finalized |
| Approved stages | Business, Metrics, Review | Business, Metrics, Review |
| Quality score | `0.799` blind review of the failed run | Not measured; remains `null` |

The old run reached the intended Review endpoint but the finalizer rejected it because it required a Report stage. The fixed run uses the same frozen request and route, records `completion_kind=review_terminal`, writes an immutable run summary, and passes audit without inventing a Report artifact.

## Fixes exercised

1. `Business -> Metrics -> Review` can end after an approved Review result.
2. Report-terminal routes still require an approved Report result.
3. The final summary records the completion kind and keeps the approved artifact and approval trail immutable.
4. Review reads a registered input bundle containing approved upstream artifacts and decisions.
5. Review validation rejects stale, duplicated, or mismatched input bundles and decision inheritance.
6. The metrics workbench can export TOML, accept human edits, validate the edited contract, import an exact revision, and retain the edit history.
7. The finalizer accepts a relative run directory, matching the CLI's normal path usage.

## Recovery evidence

The fixed run retains failed and interrupted attempts instead of deleting them:

- Metrics had an initial failed handoff and a corrected approved attempt.
- Review had an initial contextual validation failure, two interrupted attempts, and a final approved attempt.
- The final Review attempt is `attempt-4`; its raw response, receipt, validation report, input bundle, and SHA-256 references remain in the run directory.
- The final run has `revision=39`, `status=completed`, `completion_kind=review_terminal`, and `audit.errors=[]`.

## Evaluation boundary

This rerun proves the controlled workflow's terminal behavior and evidence integrity for a metric-design route. It does not establish blind quality, model superiority, cost, database integration, or three-mode comparability. Those fields remain unmeasured until the same frozen case and review protocol are run across the required modes.

## Recommended next case

Do not start a broad batch immediately. The next single case should be `review-fail-rollback`, because it directly tests the next highest-risk behavior: Review failure, earliest responsible stage selection, downstream invalidation, and recovery without publishing stale artifacts. After that, run `route-skip-unused-stages` to verify that dynamic routing remains minimal across a second successful terminal route.


# 数据分析报告

> Run `instagram-full-smoke-fix-20260820` | Review-approved synthesis | Contract `1.2`

## 执行摘要

当前不能正式交付，不能证明活跃度下降。本输出仅用于独立测试；Review=FAIL 使正式 Report 被阻断。

## 证据摘要

- 已提供的 Business、Metrics、SQL、Insight 与查询证据可作为输入 artifact 引用。
- 现有 Review 结果为 FAIL，因此输入尚未达到正式 Report 的交付前置条件。
- 数据仅覆盖单个自然日，且 SQL 分母粒度问题未解决，不能支持活跃度下降结论。

## 建议

| ID | 建议 | 指标 | 证据 |
|---|---|---|---|
| `rec-repair-sql-and-rerun-review` | 先修复 s03-sql 的分母粒度问题，重新生成并验证查询证据，再重新执行 Review；在 Review 通过前不得正式交付 Report。 | - | sql-stage-s03-attempt-7@f7e6a4358ff8, activity-metrics-query-manifest-revision-2@d637f13a4027, activity-metrics-result-profile-revision-2@af6d1e060277 |

## 限制与风险

- Review=FAIL：本输出不是正式可交付报告。
- SQL 分母粒度问题阻断活跃度下降结论。
- 数据仅覆盖单个自然日。
- 无图表；Visualization 已跳过。
- 指标血缘尚未完成。
- 未提供 PASS_WITH_RISKS finding_id，因此没有可按 [finding_id] 复制的 PASS_WITH_RISKS finding。

## 后续行动

- 先修复 s03-sql 及其分母粒度问题。
- 重新生成或验证查询证据后重新执行 Review。
- 仅在 Review=PASS 或 PASS_WITH_RISKS 且风险得到明确保留后，才能正式生成 Report。

## 可审计附录

## 事实

- 已提供 Business、Metrics、SQL、Insight、查询 manifest、结果 profile、结果 CSV、Review FAIL 和当前指标血缘 artifact。
- Review artifact 的状态为 FAIL。
- 数据仅覆盖单个自然日。

## 计算结果

无。

## 假设

- 未假设输入 artifact 中未明确提供的指标值、Review finding_id 或 SQL 执行结果。

## 待验证推断

无。

## 证据

- business-stage-s01-attempt-2@9402cfaefec6
- metrics-stage-s02-attempt-4@e00059c0af5b
- sql-stage-s03-attempt-7@f7e6a4358ff8
- insight-stage-s04-attempt-3@3f37eaf8d0ad
- activity-metrics-query-manifest-revision-2@d637f13a4027
- activity-metrics-result-profile-revision-2@af6d1e060277
- activity-metrics-result-csv-revision-2@ab1823aa2baa
- review-stage-s06-attempt-3-fail@c1f93a1943b5
- metric-lineage-revision-000105@e8368b144149

## 数据产物

```json
{
  "artifact_id": "business-stage-s01-attempt-2",
  "kind": "stage",
  "path": "stages/s01-business/attempt-2/parsed-stage.json",
  "sha256": "9402cfaefec6bf328b531d55e16620d0fa182697f23f994b41a9e130c1699163"
}
```
```json
{
  "artifact_id": "metrics-stage-s02-attempt-4",
  "kind": "stage",
  "path": "stages/s02-metrics/attempt-4/parsed-stage.json",
  "sha256": "e00059c0af5b8fa66d26c65f8cf34c9e86c336048a638289715a9224ab0d3336"
}
```

```json
{
  "artifact_id": "sql-stage-s03-attempt-7",
  "kind": "stage",
  "path": "stages/s03-sql/attempt-7/parsed-stage.json",
  "sha256": "f7e6a4358ff88abda0752a8346d83b3cbecb15ebd213ce98586ca65cc247751e"
}
```

```json
{
  "artifact_id": "insight-stage-s04-attempt-3",
  "kind": "stage",
  "path": "stages/s04-insight/attempt-3/parsed-stage.json",
  "sha256": "3f37eaf8d0ad1da69d0ac46de20381ac9b693a20ffe0f8b3ad78892164fb0c64"
}
```

```json
{
  "artifact_id": "activity-metrics-query-manifest-revision-2",
  "kind": "query_manifest",
  "path": "data/queries/activity_metrics_by_period/revision-2/result/query-manifest.json",
  "sha256": "d637f13a40271a37cb4bb918c57cb1c87c7105d29509d3be36ee864697ec7082"
}
```

```json
{
  "artifact_id": "activity-metrics-result-profile-revision-2",
  "kind": "result_profile",
  "path": "data/queries/activity_metrics_by_period/revision-2/result/result-profile.json",
  "sha256": "af6d1e0602778c43dbd02ef13792b98659124d829f851e68f621b6639904d202"
}
```

```json
{
  "artifact_id": "activity-metrics-result-csv-revision-2",
  "kind": "result_csv",
  "path": "data/queries/activity_metrics_by_period/revision-2/result/result.csv",
  "sha256": "ab1823aa2baa905d9d7439f12a205b64d07473cce552ed212628b633c269bbbf"
}
```

```json
{
  "artifact_id": "review-stage-s06-attempt-3-fail",
  "kind": "review_stage",
  "path": "stages/s06-review/attempt-3/parsed-stage.json",
  "sha256": "c1f93a1943b5c9fb26f3619a7489f9ad02d52bc3bcca0e9c43eab000d71a089a"
}
```

```json
{
  "artifact_id": "metric-lineage-revision-000105",
  "kind": "lineage",
  "path": "lineage/revisions/revision-000105-e8368b1441492d63/metric-lineage.json",
  "sha256": "e8368b1441492d630a1a1b88d05b9fb06aae20eb3b780aa503cf8c7131c2158d"
}
```


## 血缘

```json
{
  "artifact_ids": [
    "business-stage-s01-attempt-2",
    "metrics-stage-s02-attempt-4",
    "sql-stage-s03-attempt-7",
    "insight-stage-s04-attempt-3",
    "activity-metrics-query-manifest-revision-2",
    "activity-metrics-result-profile-revision-2",
    "activity-metrics-result-csv-revision-2",
    "review-stage-s06-attempt-3-fail",
    "metric-lineage-revision-000105"
  ],
  "lineage_id": "s07-report-input-lineage",
  "relationships": [
    {
      "from": "business-stage-s01-attempt-2",
      "to": "s07-report",
      "type": "input"
    },
    {
      "from": "metrics-stage-s02-attempt-4",
      "to": "s07-report",
      "type": "input"
    },
    {
      "from": "sql-stage-s03-attempt-7",
      "to": "s07-report",
      "type": "input"
    },
    {
      "from": "insight-stage-s04-attempt-3",
      "to": "s07-report",
      "type": "input"
    },
    {
      "from": "activity-metrics-query-manifest-revision-2",
      "to": "s07-report",
      "type": "query_evidence"
    },
    {
      "from": "activity-metrics-result-profile-revision-2",
      "to": "s07-report",
      "type": "query_evidence"
    },
    {
      "from": "activity-metrics-result-csv-revision-2",
      "to": "s07-report",
      "type": "query_evidence"
    },
    {
      "from": "review-stage-s06-attempt-3-fail",
      "to": "s07-report",
      "type": "blocking_review"
    },
    {
      "from": "metric-lineage-revision-000105",
      "to": "s07-report",
      "type": "lineage_context"
    }
  ],
  "target_stage_id": "s07-report"
}
```

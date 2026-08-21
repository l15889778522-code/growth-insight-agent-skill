# SQL 日期边界与数据覆盖专项待办

状态：PARTIAL

## 背景

- 运行：`instagram-full-smoke-fix-20260820`
- 数据源：本机 MySQL `Instagram`，只记录数据源标识，不记录凭证
- 原查询：`activity_metrics_by_period`，revision 1
- 原 SQL SHA-256：`61dac2c45ad08ebf4e18db3b90afec79da08d91c7b8d64146b7592c045b1219d`
- 原因确认时间：2026-08-21

## 已确认问题

活动表中的时间范围只有一个自然日：`2023-07-19 19:44:59.610989`。原 SQL 使用：

```sql
latest_date = DATE(MAX(event_ts))
event_ts < latest_date
```

这会把最新自然日从 `recent` 窗口排除，导致 baseline、comparison、recent 的结果都为零。数据库实际存在活动记录，问题不是空库、权限失败或 `users` 连接丢失。

已完成的只读复核结果：

- `likes`：8,782 行
- `comments`：7,488 行
- `follows`：7,623 行
- `photos`：257 行
- 包含最新自然日后，四类事件均能连接到有效 `users`
- 包含最新自然日后的活动总数：24,150
- 包含最新自然日后的有效活跃用户数：87

## 本次运行已完成的修复

SQL 阶段已回退并生成 attempt 6。保持原 30 天窗口和 period_label × event_type 粒度，仅将 recent 的排他上界修正为：

```sql
DATE_ADD(latest_date, INTERVAL 1 DAY)
```

当前修订阶段产物：

- 阶段：`s03-sql`
- revision：`6`
- 阶段 artifact SHA-256：`32780c1e1ae012dc868f4d81568ce8533aa5ac061ab012d5151a2130dd2094dd`
- 阶段校验：通过
- 运行审计：通过
- 状态：等待用户审批

旧 query revision 1、旧全零结果和 Insight 结果保留为历史证据，并已由返工流程标记为失效；未删除。

## 后续专项修改

1. 将动态窗口逻辑改为显式区分 `latest_event_date`、完整自然日和排他上界，增加边界回归测试。
2. 在查询执行前检查可用自然日数量；不足三个完整分析窗口时，阻止“下降趋势”结论或将结果标记为不可归因。
3. 将“零值”与“没有可用时间覆盖”分开表达，避免用 `COALESCE(..., 0)` 掩盖不可计算状态。
4. 增加测试数据覆盖多个自然日，验证 baseline、comparison、recent 的边界和相邻日期不重叠。
5. 增加真实 MySQL 测试：最新日只含时间戳、跨日数据、空表、空时间戳、无效用户连接和多窗口数据。
6. 在 Insight 和 Review 阶段增加硬性规则：只有数据覆盖满足条件时，才允许输出活跃下降、下降时间和驱动归因。
7. 在后续修复中保持原始 query revision、审批哈希、结果血缘和回退记录不可变。

## 验收标准

- 最新自然日的数据不会被动态窗口意外排除。
- 旧 SQL 的 SHA-256 与新 SQL 的 SHA-256 在运行记录中可区分。
- 单日数据不会被报告为活跃下降。
- 不足三个完整窗口时，Insight 明确输出数据覆盖不足，并阻止下游生成误导性趋势结论。
- 所有边界场景均有自动测试和真实 MySQL 验证记录。

## 2026-08-21 Skill 加固进展

已完成：

1. SQL 阶段现在必须覆盖每个已批准指标：每个 `metric_id` 必须有字段映射，或明确进入 `unsupported_metrics`；受支持指标必须进入至少一个可执行查询。
2. SQL Agent 规则明确要求比率、份额和留存指标分别声明计算粒度、分母范围与排除规则。拆分维度不得在指标定义未授权时缩小共享分母；类似 `period_label × event_type` 的分析必须先按 `period_label` 计算总体分母，再连接事件类型分子。
3. Metrics、SQL、Insight 和 Review 规则已经明确区分“数值为零”“期间不存在”和“数据覆盖不足”。未覆盖批准比较窗口时，不得宣称下降趋势或归因。
4. 测试用户、内部用户、机器人等排除规则必须显式应用；Schema 不支持识别时必须记录缺口和风险，不能声称已经排除。
5. Review 在包含 Metrics 的路线中启动前必须存在有效指标血缘。Review 校验失败后的重试不再错误失效仅由上游批准产物生成的血缘。
6. Review 输入包现在绑定批准决策、指标血缘、查询 manifest、查询结果、结果 profile 和图表 manifest；Agent 不再通过重复抄写全部历史决策来证明继承。
7. 相关针对性回归为 `44 passed`；完整离线回归为 `152 passed, 2 skipped`。

仍未完成：

1. 已执行的历史 SQL 与结果是不可变证据，本次没有改写或伪造新的查询修订；活动用户分母修正仍需创建新的 SQL attempt、重新审批并执行。
2. 当前 Instagram 数据只覆盖一个自然日，不能完成三窗口趋势验收。需要补充真实多日数据或使用受控多日 fixture 后重新运行 SQL、Insight 和 Review。
3. 当前分母规则主要由结构化字段、完整指标覆盖校验和 Review 约束保护，尚未实现可对任意 SQL 自动证明分母粒度正确的通用 AST 语义证明器。
4. 本轮没有重新启动真实 Agent、真实 MySQL 查询或正式 Report，因此不能把本节标记为完整真实闭环 `DONE`。

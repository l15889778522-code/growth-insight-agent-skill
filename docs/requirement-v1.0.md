# GrowthInsight Agent Skill 需求文档 v1.0

## 1. 项目定位

GrowthInsight Agent Skill 是一个面向 Codex 的多 Agent 数据分析协作 skill。

它的目标不是建设 BI 平台、报表工厂或数据仓库，而是模拟真实数据分析团队的工作流：用户输入一个业务分析需求后，由多个数据分析角色分阶段协作，完成业务理解、指标设计、SQL 分析、洞察归因、可视化方案、质量评审和最终报告。

核心特点：

- 以 Codex skill 为最终交付形态
- 支持 mock schema 和真实数据库接口
- 支持每个 Agent 阶段完成后由用户确认
- 支持用户在指标体系阶段自定义新增、修改、删除指标
- 输出可用于面试讲解、业务汇报和分析复盘的数据分析报告

## 2. 非目标

本项目明确不做以下内容：

- 不做 ODS / DWD / DWS / ADS 数仓分层
- 不做 ETL 管道和任务调度平台
- 不做数据建模平台
- 不做企业级 BI 报表系统
- 不做权限系统、登录系统和复杂组织管理
- 不做拖拽式报表搭建器
- 不默认写入、修改或删除真实数据库数据

项目重点是数据分析流程自动化，而不是数据平台工程化。

## 3. 使用场景

典型输入：

```text
最近社交平台新用户 7 日留存下降，请分析可能原因，并设计分析方案。
```

Skill 应完成：

1. 理解业务背景和问题
2. 拆解分析目标和范围
3. 设计指标体系
4. 等待用户确认或自定义指标
5. 基于最终指标生成 SQL
6. 可选执行只读数据库查询
7. 生成洞察和归因假设
8. 设计图表和报告结构
9. 评审分析质量和数据风险
10. 汇总最终分析报告

## 4. Agent 角色设计

### 4.1 Main Agent：数据分析负责人

职责：

- 接收用户业务分析需求
- 调度各个专业 Agent
- 管理阶段确认流程
- 汇总各阶段产出
- 生成最终报告

Main Agent 不直接跳过用户确认节点。每个阶段完成后，必须等待用户确认、修改或补充。

### 4.2 Business Agent：业务理解

职责：

- 理解业务背景
- 明确分析目标
- 定义分析范围和非目标
- 补充业务假设
- 输出待确认的业务问题拆解

产出文件建议：

```text
docs/01_business_analysis.md
```

### 4.3 Metrics Agent：指标体系

职责：

- 设计北极星指标
- 设计核心指标
- 设计过程指标
- 设计护栏指标
- 定义指标口径、业务含义、依赖字段和分析维度
- 接收用户自定义新增、修改、删除指标
- 输出最终确认版指标体系

产出文件建议：

```text
docs/02_metrics_framework.md
```

### 4.4 SQL Agent：SQL 分析与数据库接口

职责：

- 读取 mock schema 或真实数据库 schema
- 判断字段是否足够支持指标计算
- 生成分析 SQL
- 解释 SQL 查询目的、字段、过滤条件、聚合逻辑和风险
- 可选执行只读查询
- 禁止执行写入、删除、修改、建表等危险 SQL

产出文件建议：

```text
docs/03_sql_analysis.md
```

### 4.5 Insight Agent：洞察归因

职责：

- 基于业务问题、指标和 SQL 结果提出洞察
- 设计归因假设
- 提出维度拆解路径
- 给出验证方式
- 输出业务结论和下一步分析建议

产出文件建议：

```text
docs/04_insights.md
```

### 4.6 Visualization Agent：图表方案

职责：

- 推荐图表类型
- 设计 dashboard 或报告结构
- 说明每张图回答什么业务问题
- 判断图表是否支撑分析结论

产出文件建议：

```text
docs/05_visualization_plan.md
```

### 4.7 Review Agent：质量评审

职责：

- 判断分析是否回答原始业务问题
- 检查指标口径是否一致
- 检查 SQL 是否存在风险
- 检查是否缺少关键维度
- 检查数据质量风险
- 给出通过、有条件通过或不通过结论

产出文件建议：

```text
docs/06_review_report.md
```

## 5. 核心工作流

```text
用户输入业务分析需求
↓
Business Agent 拆解业务问题
↓
用户确认 / 修改 / 补充
↓
Metrics Agent 生成指标体系
↓
用户确认 / 新增指标 / 修改指标 / 删除指标
↓
Metrics Agent 输出最终指标体系
↓
SQL Agent 基于最终指标生成 SQL
↓
用户确认 / 修改 SQL 方向 / 补充查询
↓
Insight Agent 生成洞察和归因假设
↓
用户确认 / 补充业务假设
↓
Visualization Agent 设计图表和报告结构
↓
用户确认 / 调整图表方案
↓
Review Agent 评审整体分析质量
↓
用户确认
↓
Main Agent 汇总 docs/07_final_report.md
```

## 6. 用户确认机制

Skill 必须遵守：

```text
每个 Agent 完成阶段产出后，必须暂停并等待用户确认。
用户未确认前，不得自动进入下一个 Agent。
```

用户可使用以下指令：

```text
继续
确认，进入下一步
修改：...
补充：...
重新生成这一阶段
跳过当前阶段
```

指标阶段支持额外指令：

```text
新增指标：...
修改指标：...
删除指标：...
确认最终指标体系
```

## 7. 指标自定义功能

Metrics Agent 输出初版指标后，用户可以编辑指标体系。

每个指标建议包含：

```text
指标名称
指标类型
计算口径
业务含义
分析维度
依赖字段
是否必选
风险说明
```

指标类型建议：

```text
北极星指标
核心指标
过程指标
护栏指标
诊断指标
```

用户新增指标示例：

```text
新增指标：首日关注率
业务含义：衡量新用户首日是否建立社交关系
计算口径：注册当日完成关注行为的新用户数 / 注册当日新用户数
分析维度：渠道、设备、地区、注册日期
```

Metrics Agent 需要将用户新增或修改的指标合并为最终指标体系，再传递给 SQL Agent。

## 8. 数据库接口需求

### 8.1 支持模式

Skill 应支持三种数据来源：

```text
1. mock schema：无真实数据库时用于演示和面试
2. uploaded schema：用户提供表结构、字段说明、样例数据
3. read-only database：连接真实 MySQL / PostgreSQL / SQLite / DuckDB 等数据库
```

v1.0 优先支持：

```text
mock schema
MySQL read-only connection
```

### 8.2 数据库安全规则

真实数据库接口必须默认只读。

禁止执行：

```text
INSERT
UPDATE
DELETE
DROP
ALTER
CREATE
TRUNCATE
REPLACE
MERGE
GRANT
REVOKE
```

允许执行：

```text
SELECT
WITH
SHOW
DESCRIBE
EXPLAIN
```

执行真实查询前必须：

1. 检查 SQL 是否只读
2. 优先添加 LIMIT
3. 避免全表大扫描
4. 告知用户查询目的
5. 在用户确认后执行

### 8.3 连接配置

真实数据库连接信息不写入 skill 文件。

建议通过环境变量或本地配置提供：

```text
MYSQL_HOST
MYSQL_PORT
MYSQL_USER
MYSQL_PASSWORD
MYSQL_DATABASE
```

## 9. Skill 文件结构建议

正式 skill 建议结构：

```text
growth-insight-agent/
├─ SKILL.md
├─ agents/
│  └─ openai.yaml
├─ references/
│  ├─ workflow.md
│  ├─ agent-roles.md
│  ├─ metric-framework.md
│  ├─ sql-standards.md
│  ├─ database-connectors.md
│  └─ report-template.md
├─ scripts/
│  ├─ inspect_schema.py
│  ├─ test_db_connection.py
│  └─ run_readonly_query.py
└─ assets/
   └─ final-report-template.md
```

## 10. 最终报告结构

最终报告建议输出到：

```text
docs/07_final_report.md
```

报告包含：

```text
1. 业务背景
2. 分析目标
3. 分析范围和非目标
4. 指标体系
5. 数据来源和字段说明
6. SQL 查询方案
7. 查询结果摘要
8. 关键洞察
9. 归因假设
10. 可视化方案
11. 数据质量风险
12. 评审意见
13. 下一步建议
```

## 11. v1.0 MVP 范围

v1.0 必做：

- Codex skill 形态设计
- 6 个 Agent 角色流程
- 每个 Agent 阶段用户确认
- 指标体系支持用户新增、修改、删除
- mock schema 支持
- MySQL 只读接口设计
- SQL 安全检查规则
- 最终报告模板

v1.0 可暂缓：

- 前端可视化界面
- 多数据库完整适配
- 自动绘制图表
- 复杂数据质量检测脚本
- 真实 dashboard 生成
- 多用户和权限管理

## 12. 面试表述

可以这样介绍项目：

```text
这个项目不是数仓项目，也不是传统 BI 平台。我开发的是一个 Codex skill，用来模拟真实数据分析团队的协作流程。用户输入业务问题后，skill 会按业务理解、指标设计、SQL 分析、洞察归因、可视化方案和质量评审等阶段推进。每个阶段完成后都需要用户确认，尤其在指标设计阶段，用户可以自定义新增或修改指标，后续 SQL 和报告都会基于最终确认的指标体系生成。系统支持 mock schema，也预留真实 MySQL 只读接口，重点是让数据分析流程可控、可追踪、可复盘。
```


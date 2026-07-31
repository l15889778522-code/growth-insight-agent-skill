# 多 Agent 数据分析 Skill v1.1

这是一个面向个人工作流的 Codex Skill。根任务使用 **Codex 原生子 Agent** 动态选择 Business、Metrics、SQL、Insight、Visualization、Review 和 Report 角色，并在每个阶段完成后等待用户确认。

它的重点不是“同时开很多 Agent”，而是把数据分析变成可审计、可修订、可恢复的标准流程：

- 子 Agent 是可见的原生任务线程，不由根任务模拟角色。
- 先确认动态路由，再启动第一个角色。
- Business 到 Review 的每份产物都绑定版本与 SHA-256，确认后才能传给下游。
- Metrics 阶段支持用户新增、修改和删除指标，并检查稳定 `metric_id` 与版本递增。
- SQL 阶段确认和真实数据库查询确认相互独立。
- MySQL、SQLite 通过统一只读适配器接入。
- 查询结果、指标血缘、PNG/HTML 图表和最终建议可以互相追溯。
- 状态快照、事件哈希链和运行锁支持阶段边界恢复。

## 安装

建议使用 Python 3.11 或更高版本创建项目虚拟环境：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

安装 Skill 本体到当前账号的 `$HOME/.agents/skills`，并创建隔离运行环境：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_skill.ps1 `
  -Scope Personal `
  -InstallDependencies `
  -PythonExecutable .\.venv\Scripts\python.exe `
  -Force
```

再安装七个原生 Agent 到当前 Codex 账号：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_custom_agents.ps1 -Scope Personal -Force
powershell -ExecutionPolicy Bypass -File .\scripts\codex_agents_preflight.ps1 -Scope Personal
```

也可以只安装到某个测试项目：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_skill.ps1 -Scope Project -ProjectPath C:\path\to\project -Force
powershell -ExecutionPolicy Bypass -File .\scripts\install_custom_agents.ps1 -Scope Project -ProjectPath C:\path\to\project
```

安装或更新 Skill、Agent 后建议重启 Codex；部分运行时可能动态刷新。静态预检通过不等于运行时已加载，无论是否重启，都需实际启动可见子 Agent 完成 smoke test。

安装后的 Skill 始终以其自身 `SKILL.md` 所在目录解析脚本、Schema 和私有 `.venv`，不依赖当前项目目录。分析运行目录和用户数据仍写入当前工作项目。

## 跨设备同步

在另一台同账号设备上克隆同一个 Git 仓库，创建 `.venv` 并安装 `requirements-dev.txt`，然后依次运行 `install_skill.ps1` 与 `install_custom_agents.ps1`。数据库环境变量和凭据只在新设备本地重新配置，不提交到 Git。最后重启 Codex，并运行 Agent 预检与一个可见子 Agent smoke test。

## 使用

在 Codex 中调用：

```text
$multi-agent-data-analysis-skill 分析这份数据。每个角色完成后先让我确认；指标阶段允许我新增、修改或删除指标。
```

首次响应只展示路由，不启动角色。常用命令：

- `确认路由`
- `修改路由：...`
- `确认，进入下一步` 或 `继续`
- `修改：...`、`补充：...`、`重新生成当前阶段`
- `新增指标：...`、`修改指标：...`、`删除指标：...`
- `回退到：<角色>`
- `终止分析`

指标编辑会先规范化为机器可校验的 `metric-edit.json`，新 Metrics 修订必须精确落实指定操作和字段。若需要真实查询，Skill 会额外展示完整 SQL、数据源标识、非敏感物理数据源指纹、超时、最大行数、最大结果字节数和 SQL 哈希。只有确认该查询指纹后，确定性脚本才会连接数据库。

## 数据库

SQLite：

```text
DB_TYPE=sqlite
DB_SOURCE_ID=local-analytics
SQLITE_PATH=C:\data\analytics.db
```

MySQL：

```text
DB_TYPE=mysql
DB_SOURCE_ID=mysql-readonly
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=readonly_user
MYSQL_PASSWORD=...
MYSQL_DATABASE=analytics
```

凭据只通过环境变量或本地忽略文件提供。SQL 使用 `sqlglot` AST 校验，数据库会话再启用只读限制；MySQL 账号本身仍必须只有只读权限。

外部 CSV、Schema 或既有结果先复制并登记到运行目录：

```powershell
python scripts/runctl.py ingest --run-dir <run> --source <file> --kind user_result
```

## 运行产物

```text
multi-agent-data-analysis-runs/<run_id>/
  request.json
  route-plan.json
  run-state.json
  events.jsonl
  approvals.jsonl
  stages/<stage_id>/attempt-<n>/
  data/query.sql
  data/query-request.json
  data/query-manifest.json
  data/result.csv
  data/result-profile.json
  lineage/metric-lineage.json
  charts/*.png
  charts/*.html
  final/final-report.md
  final/run-summary.json
```

恢复和审计：

```powershell
python scripts/runctl.py audit --run-dir <run>
python scripts/runctl.py recover --run-dir <run>
python scripts/runctl.py resume --run-dir <run>
python scripts/runctl.py summary --run-dir <run> --output final/run-summary.json
python scripts/runctl.py finalize --run-dir <run>
```

带 Report 的流程在 Report 落盘后先进入 `finalizing`；重建最终血缘并验证图表后，`finalize` 才写入运行摘要并提交 `completed`。

## 测试与评测

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

三模式评测使用固定用例，缺少真实运行结果时标记为 `not_run`：

```powershell
python scripts/run_evals.py `
  --results-root tests/evals/results `
  --output-json tests/evals/evaluation-results.json `
  --output-markdown tests/evals/evaluation-results.md
```

对比模式为单 Codex、v1.0 固定流程和 v1.1 原生动态多 Agent。规则检查与人工质量评分分开汇总，Token 仅在运行时可获得时记录。

## 当前边界

- SQLite 端到端测试和 MySQL 驱动契约测试已自动化。
- MySQL 只有在真实只读测试实例上完成集成测试后，才能标记为真实集成通过。
- Agent 的实际模型由当前 Codex 原生运行时解析；TOML 不绑定 DeepSeek 或其他外部模型提供商。
- v1.1 保证阶段边界恢复，不恢复中断时尚未返回的模型推理内容。

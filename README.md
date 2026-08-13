# 多 Agent 数据分析 Skill v1.2

这是一个面向个人工作流的 Codex Skill。根任务使用 **Codex 原生子 Agent** 动态选择 Business、Metrics、SQL、Insight、Visualization、Review 和 Report 角色，并在每个阶段完成后等待用户确认。

它的重点不是“同时开很多 Agent”，而是把数据分析变成可审计、可修订、可恢复的标准流程：

- 子 Agent 是可见的原生任务线程，不由根任务模拟角色。
- 先确认动态路由，再启动第一个角色。
- 每个原生 Agent 响应都保存原文、Agent ID、配置哈希和可获得的模型/Token 元数据；Business 到 Report 的每份产物确认后才能传给下游。
- Metrics 阶段支持用户新增、修改和删除指标，并检查稳定 `metric_id` 与版本递增。
- SQL 阶段确认和真实数据库查询确认相互独立。
- MySQL、SQLite 通过统一只读适配器接入。
- 查询、指标血缘、PNG/HTML 图表和最终报告使用不可变版本目录、artifact ID 与 SHA-256 互相追溯。
- 状态快照、完整事件哈希链、运行锁和执行租约支持阶段与发布边界恢复。
- CLI 捕获的确认透明标记为 `self_asserted`；只有宿主可观察或签发的信息才会声明更高信任等级。

## 安装

建议使用 Python 3.11 或更高版本创建项目虚拟环境：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

安装 Skill 本体到当前账号的 `$HOME/.agents/skills`，并创建隔离运行环境：

```powershell
.\.venv\Scripts\python.exe scripts\install_skill.py install `
  --scope personal `
  --install-dependencies `
  --force
```

再安装七个原生 Agent 到当前 Codex 账号：

```powershell
.\.venv\Scripts\python.exe scripts\install_skill.py install-agents --scope personal --force
powershell -ExecutionPolicy Bypass -File .\scripts\codex_agents_preflight.ps1 -Scope Personal
```

也可以只安装到某个测试项目：

```powershell
.\.venv\Scripts\python.exe scripts\install_skill.py install --scope project --project-path C:\path\to\project --force
.\.venv\Scripts\python.exe scripts\install_skill.py install-agents --scope project --project-path C:\path\to\project
```

项目级 Agent 只在 `--project-path` 对应目录本身作为 Codex 工作区根目录打开时可靠生效。如果 Codex 打开的是它的上级目录，而仓库只是嵌套子目录，个人级同名 Agent 可能优先或继续被当前任务缓存；此时应改用个人级安装，或把该仓库单独作为工作区重新打开。

安装或更新 Agent 后必须重启 Codex。静态预检通过只证明磁盘配置一致，不证明当前进程已重新加载；重启后仍需实际启动可见子 Agent 完成 smoke test。

安装后的 Skill 始终以其自身 `SKILL.md` 所在目录解析脚本、Schema 和私有 `.venv`，不依赖当前项目目录。分析运行目录和用户数据仍写入当前工作项目。

## 跨设备同步

在另一台同账号设备上克隆同一个 Git 仓库，创建 `.venv` 并安装 `requirements-dev.txt`，然后运行 `install_skill.py install` 与 `install_skill.py install-agents`。数据库环境变量和凭据只在新设备本地重新配置，不提交到 Git。最后重启 Codex，并运行 Agent 预检与一个可见子 Agent smoke test。完整安装、升级、验证和卸载命令见 `references/installation.md`。

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
MYSQL_SSL_MODE=VERIFY_IDENTITY
MYSQL_SSL_CA=C:\certs\mysql-ca.pem
```

凭据只通过环境变量或本地忽略文件提供。SQL 使用 `sqlglot` AST 校验，数据库会话再启用只读限制；MySQL 账号本身仍必须只有只读权限。查询审批指纹绑定服务器 UUID/版本、连接账号、TLS 模式与证书校验状态，不包含密码或私钥口令。真实 MySQL 验证步骤见 `references/mysql-integration.md`。

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
  data/queries/<query_id>/revision-<n>/query.sql
  data/queries/<query_id>/revision-<n>/query-request.json
  data/queries/<query_id>/revision-<n>/result/query-manifest.json
  data/queries/<query_id>/revision-<n>/result/result.csv
  data/queries/<query_id>/revision-<n>/result/result-profile.json
  lineage/revisions/<identity>/metric-lineage.json
  lineage/latest.json
  charts/revisions/<identity>/*.{png,html}
  charts/latest.json
  final/reports/<identity>/final-report.md
  final/reports/<identity>/final-report-manifest.json
  final/summaries/run-summary-r<n>.json
```

恢复和审计：

```powershell
python scripts/runctl.py audit --run-dir <run>
python scripts/runctl.py recover --run-dir <run>
python scripts/runctl.py recover-executions --run-dir <run>
python scripts/runctl.py resume --run-dir <run>
python scripts/runctl.py publish-final-report --run-dir <run>
python scripts/runctl.py finalize --run-dir <run>
```

Report 也必须经过用户确认。流程随后进入 `finalizing`；重建最终血缘、验证图表并发布带 manifest 的最终报告后，`finalize` 才写入版本化运行摘要并提交 `completed`。

## 测试与评测

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

当前受控 Skill 模式从真实 v1.2 run 重算规则证据，再汇总三模式结果：

```powershell
python scripts/capture_eval_record.py `
  --run-dir <run> `
  --case-id full-diagnosis-sqlite `
  --output tests/evals/results/controlled-skill/full-diagnosis-sqlite/repeat-01.json
```

缺少真实运行结果时标记为 `not_run`：

```powershell
python scripts/run_evals.py `
  --results-root tests/evals/results `
  --output-json tests/evals/evaluation-results.json `
  --output-markdown tests/evals/evaluation-results.md
```

正式对比模式为单 Codex、Codex 原生自由子 Agent 编排和当前受控 Skill。三个模式必须使用同一输入快照，并按用户指令一次只运行一个模式；覆盖不一致时不形成横向结论。Schema、SQL、安全门、证据和恢复规则尽量从产物计算；质量分只接受统一盲评记录，Token 不可获得时保留为 `null`。

## 当前边界

- SQLite 端到端测试和 MySQL 驱动契约测试已自动化。
- MySQL 只有在真实只读测试实例上完成集成测试后，才能标记为真实集成通过。
- Agent 的实际模型由当前 Codex 原生运行时解析；TOML 不绑定 DeepSeek 或其他外部模型提供商。
- v1.2 保证已提交阶段和原子发布边界的恢复，不恢复中断时尚未返回的模型推理内容。
- 当前 CI 不连接真实 MySQL；本地 live 测试必须显式启用并提供预先核对的数据源指纹。

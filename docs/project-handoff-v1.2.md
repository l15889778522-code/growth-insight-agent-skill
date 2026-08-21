# Multi-Agent Data Analysis Skill v1.2 项目交接文档

文档状态：`CURRENT_HANDOFF`
交接基线日期：`2026-08-13`
适用仓库：`l15889778522-code/growth-insight-agent-skill`
当前合同版本：`1.2`

## 0. 使用说明

本文件是下一次 Codex 任务恢复本项目时的首要入口。后续任务应先阅读本文件，再按第 6 节的顺序继续。其他历史设计和任务清单只能作为背景资料，若与本文件冲突，以本文件记录的当前产品决策和发布状态为准。

状态定义：

| 状态 | 含义 |
|---|---|
| `DONE` | 功能代码和对应验证已经完成，当前没有已知的实现缺口 |
| `PARTIAL` | 已有基础实现，但仍缺真实环境验证、自动化能力或完整交付范围 |
| `BLOCKED` | 完成路径已知，但当前被环境、额度、外部设备或发布条件阻塞 |
| `NOT_STARTED` | 已确认要做，但尚未开始实现 |
| `DEFERRED` | 已记录的长期工作，不属于当前最近版本的发布范围 |

本文件严格区分四个层次：

1. **代码存在**：仓库中已经有实现。
2. **自动测试**：实现已由本地或 CI 自动测试覆盖。
3. **真实验证**：已经在真实 Agent、真实数据库或真实第二台设备上运行。
4. **正式发布**：变更已合并、打标签并创建 GitHub Release。

代码存在或 mock 测试通过不等于真实集成完成；分支已经推送也不等于正式发布。

## 1. 项目最终目标

### 1.1 产品定位

本项目最终要成为一个面向个人工作流的 **Codex 原生多 Agent 数据分析 Skill**。它使用 Codex 原生子 Agent 承担专业分析角色，并用确定性的状态、合同、审批和产物机制约束协作过程。

最终能力包括：

- 根据分析需求选择最短的专业角色流程，而不是每次固定执行七个角色。
- 在 Metrics 阶段允许用户新增、修改、删除和最终确认指标。
- 每个角色完成后必须等待用户确认；只有获批产物才能作为下游角色的输入。
- 保留真实数据库接口，并在独立查询确认后安全执行只读 SQL。
- 将指标、SQL、查询结果、洞察、图表、Review 和最终报告形成可验证、可追溯的完整血缘。
- 支持任务中断后的阶段边界恢复、Review 返工、审批追踪、执行回执和运行审计。
- 最终提供 Markdown、HTML 和 Excel 三种可交付分析成果。
- 数据库按 SQLite、MySQL、PostgreSQL 的顺序扩展，不同时引入大量数据库类型。

### 1.2 最终用户流程

```text
提出问题
  -> 推荐最短路由
  -> 用户确认路由
  -> 当前专业角色执行
  -> 展示本阶段结论、变化、风险与下一角色
  -> 用户确认
  -> 必要时单独确认数据查询
  -> 洞察与图表
  -> Review
  -> 发现问题时精确返工
  -> 最终报告
```

目标体验不是让用户管理底层状态文件，而是让用户在 Codex 对话中清楚知道：当前执行了什么、证据来自哪里、哪些内容发生变化、下一步会启动谁，以及自己的确认具体授权了什么。

### 1.3 明确非目标

- 不把项目改造成通用 Agent 框架。
- 不把外部模型设为默认运行后端；Codex 原生模型仍是首选。只有 Codex 额度不足且用户明确启用时，才评估或使用 DeepSeek 备用后端。
- 不增加更多固定角色；优先提高现有七个角色的路由和协作效率。
- 不开发复杂的可视化管理后台。
- 不建设多租户企业审批平台或独立身份认证系统。
- 不恢复尚未返回的模型内部推理，只恢复已经提交的阶段和发布边界。

## 2. 当前仓库与发布状态

### 2.1 仓库信息

| 项目 | 当前值 | 状态 |
|---|---|---|
| GitHub 仓库 | `https://github.com/l15889778522-code/growth-insight-agent-skill` | `DONE` |
| 当前分支 | `agent/sequential-multi-agent-pipeline` | `DONE` |
| 发布 PR | `#1 Implement auditable data analysis skill v1.2`；已获用户批准，可转为 Ready 并合并 | `PARTIAL` |
| PR 地址 | `https://github.com/l15889778522-code/growth-insight-agent-skill/pull/1` | `DONE` |
| v1.2 主提交 | `a5e42a3 Implement auditable data analysis skill v1.2` | `DONE` |
| 最新 v1.2.1 收尾实现提交 | `83f2274 Harden interrupted MySQL streams` | `DONE` |
| 本地与远端分支 | 已同步到确定性发布门全绿基线，本文档状态提交后再次推送 | `DONE` |
| PR 合并 | 本文档冻结时尚未合并；用户已授权执行 | `PARTIAL` |
| `v1.2.1` 标签 | 本文档冻结时尚未创建；用户已授权执行 | `PARTIAL` |
| GitHub Release | 本文档冻结时尚未创建；用户已授权执行 | `PARTIAL` |

v1.2 主实现已经位于 GitHub 功能分支；本轮新增的确定性发布收尾以 `83f2274` 为最新实现基线。Actions run `31656838504` 已在同一最新代码基线上完成 Windows/Ubuntu 六版本矩阵、两套干净安装和真实 MySQL 8.4，九项检查全部通过。2026-08-13 用户明确接受第二台 Codex 尚未验收的已知风险，并授权先发布 v1.2.1。真实三模式评测继续延期，发布后只能按用户指令一次运行一个模式。本文档记录的是发布操作前的冻结状态；正式发布结果以 PR、`v1.2.1` 标签和 GitHub Release 页面为准。

### 2.2 版本语义

- 仓库根目录的 `VERSION=1.2` 是**运行合同版本**，用于状态、审批、Agent 输出、Schema 和迁移判断。
- 后续补丁发布使用 Git 标签 `v1.2.1`。
- v1.2.1 只处理兼容、验证和发布收尾，不把合同版本改成 `1.2.1`。
- 保持 `VERSION=1.2` 可避免将发布补丁号误当成新的运行合同，现有 v1.2 产物无需因此迁移。

### 2.3 已取消方向

此前考虑过将项目抽象为通用多 Agent 工作流 Skill，该方向已经明确取消。当前项目继续聚焦 Codex 原生多 Agent 数据分析。

工作区外的 `multi-agent-workflow-skill-v2-migration-plan.md` 仅是历史构想，不属于本仓库、不属于当前产品路线，也不得作为后续实施依据。

## 3. 已经实现的部分

### 3.1 Codex 原生多 Agent

总体状态：`DONE`

| 能力 | 实现 | 自动测试 | 真实验证 | 发布状态 |
|---|---|---|---|---|
| 七个专业角色配置 | `DONE` | `DONE` | `PARTIAL` | `PARTIAL` |
| 原生模型继承与只读 sandbox | `DONE` | `DONE` | `DONE` | `PARTIAL` |
| Agent 原始响应与执行回执 | `DONE` | `DONE` | `DONE` | `PARTIAL` |
| Business -> Metrics -> Review smoke | `DONE` | `DONE` | `DONE` | `PARTIAL` |
| 七角色完整真实链路 | `DONE`（配置与运行合同） | `DONE`（离线） | `PARTIAL` | `PARTIAL` |

已实现细节：

- 已有 Business、Metrics、SQL、Insight、Visualization、Review 和 Report 七个专业角色。
- 各角色职责、输入范围、输出 Schema 和推理强度分离。
- 当前 Agent 配置默认继承 Codex 原生模型，不绑定 DeepSeek、外部模型提供商或 API 密钥；DeepSeek 目前只是一条尚未验证的条件性备用路线。
- Agent 默认使用 `read-only` sandbox；当前 Codex 未提供的细粒度工具白名单不会被项目虚构为已强制执行。
- 每次 Agent 执行可以保存原始响应、Agent ID、线程 ID、配置哈希、推理强度、时间以及宿主可提供的模型和 Token 信息。
- 宿主未提供模型或 Token 时会记录缺失原因，不填写伪造值。
- Business -> Metrics -> Review 已通过真实 Codex 原生 Agent smoke，包含独立 Agent 身份、阶段产物、确认、执行回执和指标血缘。
- smoke 证据已进入 Git，并已从提交对象导出到干净目录重新执行 `audit`，证明克隆后的提交可重放。

当前边界：七个角色配置都已存在，但尚未把完整七角色链作为一次发布级真实 Agent smoke 全程跑完。当前真实证据链止于 Review，不能宣称真实 Report 链路已完成。

### 3.2 路由和人工确认

总体状态：`PARTIAL`

已实现并验证：

- 已定义完整分析、指标设计、已有指标转 SQL、已有结果分析、单独 Review 和最终报告等预设路线。
- 路由 Schema 和校验器检查角色唯一性、顺序、依赖、所需输入、已提供输入、缺失输入和 fallback。
- Report 必须以 Review 为祖先，不能绕过 Review 直接发布。
- 首次响应只展示路由并进入 `awaiting_route_confirmation`，不会自动启动 Business。
- 路由必须绑定真实注册产物和输入 SHA-256；只有同名字符串不能满足输入要求。
- 每个阶段完成后进入 `awaiting_user_confirmation`，未获批准不能释放下游阶段。
- SQL 阶段批准与真实查询批准是两个独立门；修改 SQL 或限制后必须重新确认查询。
- 路由变化后，只有阶段身份、输入哈希和产物哈希均未变化的批准产物才能通过 `reused_approved_artifacts` 复用。
- 跳过阶段时会检查下游是否仍有合法输入，不能制造缺失交接。

尚未完整实现：

- 根 Codex 仍根据 `SKILL.md` 和路由规则判断任务类型并构造路由；尚无确定性的预设路由生成命令。
- “已有 SQL -> Insight -> Visualization -> Review -> Report”和“只检查 SQL -> SQL -> Review”等目标路线需要在 v1.3 中补充成明确、可自动生成和测试的预设。
- 用户确认摘要尚未形成统一机器产物，目前核心确认对象可审计，但对话展示仍可进一步压缩为结论、变化、风险、缺失信息和下一角色。

### 3.3 指标能力

总体状态：`PARTIAL`

已实现并验证：

- Metrics 阶段支持新增、修改和删除指标。
- 指标使用稳定 `metric_id`；修改现有指标必须递增版本，不能静默覆盖。
- 指标类型已支持 `north-star`、`core`、`process`、`guardrail` 和 `diagnostic`。
- 每个指标记录名称、业务定义、公式、粒度、分析维度、时间窗口、字段依赖、状态、来源、负责人和数据风险。
- 指标编辑使用结构化 `metric-edit.json`，绑定当前 Metrics 产物 SHA-256 和用户原始文字。
- 新 Metrics 修订必须准确落实待处理编辑，否则运行时拒绝记录该阶段。
- Metrics 输出已有 `dependency_gaps` 和 `conflict_checks` 字段。

尚未完整实现：

- `conflict_checks` 目前主要是文本结论，尚未形成可定位冲突指标、冲突类型、严重性和解决状态的结构化对象。
- `field_dependencies` 已记录指标依赖的数据字段，但尚未建立指标与指标之间的结构化依赖图。
- 尚未实现指标依赖的循环检查和下游影响分析。
- 尚未实现个人级和项目级指标模板。
- 尚未实现项目模板覆盖同 ID 个人模板时的确定性优先级和审计记录。

### 3.4 数据库和 SQL

总体状态：`DONE`（v1.2 当前支持的 SQLite/MySQL 范围）

| 能力 | 代码存在 | 自动测试 | 真实环境 | 结论 |
|---|---|---|---|---|
| SQLite 只读查询 | `DONE` | `DONE` | `DONE`（本地文件数据库） | `DONE` |
| MySQL 连接器 | `DONE` | `DONE` | MySQL 8.4.11 已真实验证 | `DONE` |
| SQL AST 只读校验 | `DONE` | `DONE` | `DONE` | `DONE` |
| TLS 和服务器身份指纹 | `DONE` | `DONE`（mock/合同和 opt-in 测试） | MySQL 8.4 已验证 | `DONE` |
| 流式查询和限制 | `DONE` | `DONE` | `DONE` | `DONE` |

已实现细节：

- SQLite 已完成只读查询端到端自动化测试。
- MySQL 已有真实连接器代码，支持 host、port、database、账号和 TLS 配置。
- MySQL TLS 模式支持 `DISABLED`、`PREFERRED`、`REQUIRED`、`VERIFY_CA` 和 `VERIFY_IDENTITY`。
- 数据源指纹绑定 endpoint、数据库、服务器 UUID、服务器版本、认证账号、请求 TLS 模式和实际协商验证状态，不包含密码或私钥口令。
- SQL 通过 `sqlglot` AST 校验，拒绝非只读语句、多语句和不允许的结构。
- 查询审批绑定最终 SQL SHA-256、方言、数据源标签、物理数据源指纹、超时、最大行数和最大结果字节数。
- MySQL 使用非缓冲或流式读取；SQLite 使用统一迭代接口。
- CSV 写入期间逐行累计 UTF-8 字节，达到限制时停止并拒绝发布部分结果。
- Decimal 以精确十进制文本序列化，避免二进制浮点改写。
- 查询结果保存到不可变的 `data/queries/<query_id>/revision-<n>/` 目录。
- GitHub Actions 已增加独立 MySQL 8.4 服务任务，自动创建 2505 行 `utf8mb4` 测试数据、`REQUIRE SSL` 且仅授予 `SELECT` 的临时账号，并从实际服务器身份导出不含凭据的数据源指纹。
- opt-in 发布门测试已覆盖真实 TLS 协商、账号授权、写入拒绝、流式读取、1000 行截断、100 字节结果拒绝、Decimal 精确值、Unicode、服务端/连接超时、连接中断和新连接重试。

真实验证结果：

- Actions run `31656838504` 已真实启动 MySQL 8.4.11，完成 TLS 只读账号、2505 行 fixture、物理身份指纹、Decimal/Unicode、行数截断、结果字节拒绝、写入拒绝、普通 `SELECT` 锁等待超时、中途断连和新连接重试。
- `83f2274` 保持 `SLEEP()` 禁止规则不变，使用管理员写锁验证超时，并在大 payload 流式查询传输中验证强制断连；未通过放宽 SQL 安全规则制造测试便利。
- 断连后的连接器重连由真实 opt-in 测试覆盖；执行租约的中断、abort 和重新申请仍由离线确定性测试覆盖，两类证据不能混写。
- 因此 MySQL 可在 v1.2 当前支持范围内标记为真实集成 `DONE`；PostgreSQL 仍是 v1.4 的 `NOT_STARTED` 项，不影响本结论。

### 3.5 数据质量、图表、血缘和报告

总体状态：`PARTIAL`

已实现并验证：

- 查询完成后生成 result profile 和有界样本，不把全部结果加载到内存中再分析。
- 已检测空结果、小样本、较高空值比例、结果截断、近似 distinct、混合类型、非有限数值、空字符串和时区混用。
- 已生成 PNG 和 HTML 图表以及机器可校验的 chart manifest。
- 指标、查询、结果、图表和报告通过 artifact ID、路径和 SHA-256 建立追踪关系。
- 指标血缘使用不可变修订目录和 `latest` 指针。
- 已生成最终 Markdown 报告、final report manifest 和独立运行摘要。
- 完成运行前会重新检查 Report、Review、血缘和图表引用是否指向当前有效版本。
- 完成状态要求唯一有效的最终报告、报告 manifest 和运行摘要，不能把不完整运行标记为完成。

尚未完整实现：

- 当前 HTML 是图表交付，不是完整的 HTML 分析报告。
- 尚未生成 Excel 工作簿。
- 重复键检查需要用户或 Schema 明确声明业务唯一键，当前不会根据列名猜测。
- 业务范围、数据完整性、新鲜度、关联关系和口径一致性尚未形成可配置、需用户确认的数据质量策略。
- PostgreSQL 尚未实现，因此跨数据库完整血缘和报告验证只覆盖当前 SQLite/MySQL 合同范围。

### 3.6 状态、审计和恢复

总体状态：`DONE`

已实现并验证：

- 每个运行拥有 `run-state.json`、`events.jsonl` 和 `approvals.jsonl`。
- v1.2 事件对完整规范化事件载荷、前一事件和状态哈希建立审计链。
- 第一个事件和每 25 个事件保存完整 checkpoint，其余事件保存确定性 state delta。
- 支持事件尾部短写检测和从最后合法提交状态恢复。
- 使用操作系统字节范围锁保护运行目录；进程退出后由操作系统释放所有权。
- 查询、图表、血缘和最终报告使用 `prepared -> executing -> publishing -> completed/aborted` 执行租约。
- 租约绑定路由修订和精确输入哈希；路由、输入、超时或运行状态变化会阻止旧任务发布。
- 不可变产物和原子发布避免覆盖历史或留下被误认成有效的部分结果。
- Review `FAIL` 会生成绑定 Review、目标产物、修复要求和路由修订的 rollback plan。
- 回退批准后，只将目标阶段置为 revising，并使其后代 stale；无关历史保留。
- v1.1 运行可以读取和审计；任何修改都要求显式迁移，不会静默升级待确认运行。

当前限制：

- Review Agent 在 finding 中提供 `responsible_stage`，并在失败输出中选择 `rollback_stage`。
- 运行时会验证回退目标存在、属于当前路线并且是 Review 的祖先。
- 运行时尚未根据全部 findings、证据和依赖图，自动计算“最早责任角色”；该增强属于 v1.3。

### 3.7 安装、测试和评测基础设施

总体状态：`PARTIAL`

#### 安装器

状态：`DONE`（功能实现），`PARTIAL`（跨设备发布验证）

- 支持个人级和项目级安装 Skill。
- 支持个人级和项目级安装七个原生 Agent。
- 支持 dry-run、安装文件清单、SHA-256 校验、升级、verify 和卸载。
- 安装失败时支持事务回滚，并保留不受当前 manifest 管理的用户文件。
- 已覆盖深层中文 Windows 路径和隔离运行环境；安装器内部继续使用长路径，调用 Python 和 pip 时转换为兼容的标准绝对路径。
- `.venv` 和可再生成的 `scripts/__pycache__` 由安装器管理，升级清理陈旧缓存，卸载不会把缓存误当作用户文件残留。
- 当前 Windows 机器已在全新中文目录完成 Skill/Agent 安装、依赖检查、静态预检、verify、卸载、无托管残留和用户文件保留的完整自动 smoke。
- 当前机器已做安装相关验证，但尚未在真实第二台同账号 Codex 完成最终跨设备验收。

#### 自动测试与 CI

| 证据 | 当前结果 | 状态 |
|---|---|---|
| 本地离线测试 | `137 passed, 2 skipped`，2026-08-13 | `DONE` |
| 跳过测试 | 本地离线运行跳过两个 opt-in MySQL 测试；Actions 已显式启用并通过 | `DONE` |
| Windows 安装器专项 | `10 passed` | `DONE` |
| Windows 全新中文目录安装生命周期 | 依赖、预检、verify、卸载和残留检查全部通过 | `DONE` |
| 分支覆盖率 | 约 `75%` | `PARTIAL` |
| Ubuntu Python 3.11/3.12/3.13 | Actions run `31656838504` 全部通过 | `DONE` |
| Windows Python 3.11/3.12/3.13 | Actions run `31656838504` 全部通过 | `DONE` |
| GitHub 干净安装 Ubuntu/Windows | Actions run `31656838504` 两套全部通过 | `DONE` |
| GitHub MySQL 8.4 | Actions run `31656838504` 完整发布门通过 | `DONE` |

原 Windows 失败由 `Get-FileHash` 在 runner PowerShell 中不可用造成。`2b18c38` 改用 .NET `System.Security.Cryptography.SHA256` 后，Actions run `31656838504` 的 Windows Python 3.11、3.12、3.13 与 Windows 干净安装全部通过，该阻塞项已经关闭。

#### 评测基础设施

状态：基础设施 `DONE`，真实评测结论 `NOT_STARTED`

- 已有机器可校验的 evaluation record Schema。
- 已有固定案例集、质量 rubric、盲评合同和结果聚合器。
- 可汇总完成率、规则通过率、质量得分、耗时、Token、人工修改次数和重复运行波动。
- 对于不可获得的 Token 或质量分，保持 `null`，不会当作零或自动通过。
- 正式聚合模式已经更新为 `single-codex`、`codex-native-free` 和 `controlled-skill`；旧 `v1.0`、`v1.1`、`v1.2` 模式值仅保留 Schema 读取兼容，不进入正式比较。
- 尚未完成三种模式的真实 Agent 重复运行，不存在可发布的比较结论。

## 4. 未实现和未完成的部分

### 4.1 v1.2.1 发布阻塞项

总体状态：`PARTIAL`（确定性发布门已完成，跨设备和真实评测经用户批准延期）
优先级：最高，完成 GitHub 发布操作后再开始 v1.3。

1. `DONE`：已使用兼容 Windows PowerShell 的 .NET SHA-256 文件计算函数替换两处 `Get-FileHash`，本地专项通过。
2. `DONE`：Windows、Ubuntu 上 Python 3.11、3.12、3.13 已在 Actions run `31656838504` 全部通过。
3. `DONE`：CI 已调整为 PR 检查加 `main` push，功能分支 push 不再与 PR 同时生成两套矩阵。
4. `DONE`：本地 Windows 中文目录生命周期和 GitHub 干净检出的 Ubuntu/Windows 独立任务均已通过。
5. `DONE`：GitHub Actions 临时 MySQL 8.4 服务、一次性 Schema、2505 行测试数据、TLS 只读账号和无凭据身份输出已在 run `31656838504` 真实运行。
6. `DONE`：真实 MySQL 已通过只读拒绝、TLS、超时、行数/字节限制、流式读取、`utf8mb4`、Decimal、中途断连和新连接重试。执行租约恢复继续由离线测试证明。
7. `DEFERRED`：真实第二台同账号 Codex 的安装、重启和可见 Agent smoke 未执行。用户已明确同意该项不再阻塞 v1.2.1，本次 Release 必须列为已知验证缺口。
8. `DEFERRED`：真实三模式评测按用户当前指令暂缓。发布后按“单 Codex -> 原生自由编排 -> 当前受控 Skill”分开执行；每次只运行一个模式，且每个模式开始前都必须获得用户的单独指令。
9. `DONE`：用户已在接受上述延期项后授权先发布，可将草稿 PR 转为 Ready、合并到 `main`、创建 `v1.2.1` 标签和正式 GitHub Release。

本次 v1.2.1 发布完成定义：Windows 和 Ubuntu CI 全绿、全新克隆安装可用、真实 MySQL 闭环通过，PR 已合并且 `v1.2.1` 标签和正式 Release 已创建。第二台 Codex 与三模式评测作为用户接受的延期项写入 Release；不得宣称已完成跨设备验证，也不得宣称 Skill 已证明优于其他模式。

### 4.2 v1.3 效率提升

总体状态：`NOT_STARTED`

#### 内部结构整理

- 先把当前约 3232 行的 `runctl.py` 按路由、审批、指标、回退、审计和发布职责拆分为内部模块。
- 保持现有 CLI 命令、参数、JSON 输出、状态文件和事件语义兼容。
- 先完成无行为变化重构及回归，再增加 v1.3 功能，避免在单一超大文件上继续叠加高风险状态逻辑。

#### 确定性最短路由

- 增加确定性预设路由生成器，根 Codex 只提交任务类型和已提供输入，由脚本生成并校验路线。
- 正式支持完整分析、指标设计、已有指标转 SQL、已有 SQL、已有查询结果、只检查 SQL、只做 Review 和最终报告等最短路线。
- 缺少必要输入时生成明确的 missing input 或能力受限 fallback，不让 Agent 假装拥有 Schema、数据或查询结果。
- 路由仍需用户确认，自动生成不等于自动执行。

#### 确认体验和审批复用

- 每阶段确认只展示本阶段结论、相对上版变化、风险、缺失信息和将要启动的下一角色。
- 自动识别输入和产物哈希完全不变的已批准产物，生成候选复用清单。
- 新路由仍需用户确认；只有确认后复用事件才生效。
- 任何指标、SQL、数据源、结果或上游产物变化都会使相关复用候选失效。

#### 指标增强

- 增加指标之间的结构化依赖关系，而不只记录字段依赖。
- 拒绝循环依赖、缺失依赖和指向已删除指标的依赖。
- 将文本 `conflict_checks` 升级为结构化冲突，至少记录冲突类型、涉及指标、严重性、依据和解决状态。
- 支持个人模板和项目模板两层。
- 同一 `metric_id` 下项目模板优先于个人模板；覆盖关系必须显式显示。
- 应用模板必须生成新的、可审计的 Metrics 修订，不能直接修改已批准产物。

#### 精确返工

- 根据 Review findings 的 `responsible_stage`、证据引用和当前依赖图，确定最早合法责任阶段。
- 多个阻断 finding 指向不同阶段时，回退到能覆盖全部必要修复的最早共同责任点。
- 自动建议回退目标仍需用户确认 rollback plan 后才执行。
- 只失效回退目标及其后代，保留无关且哈希未变化的批准产物。

### 4.3 v1.4 数据闭环

总体状态：`NOT_STARTED`

#### PostgreSQL

- 增加 PostgreSQL 只读连接器、TLS 配置、服务器/数据库/账号身份指纹和 SQL 方言验证。
- 延续 MySQL 的审批原则：凭据不进入审批和产物，物理数据源身份和执行限制进入查询指纹。
- 在一次性真实 PostgreSQL 环境完成发布级集成验证后，才能标记为完整支持。

#### 数据源配置

- 提供 SQLite、MySQL、PostgreSQL 的非敏感配置模板。
- 密码、客户端私钥口令和其他凭据只允许来自进程环境变量或本地忽略配置。
- 配置诊断和错误信息必须清除凭据，不把连接串写入日志、报告或运行产物。

#### 查询结果缓存

- 缓存键必须绑定规范化只读 SQL、参数、方言、物理数据源身份、数据快照身份、行数限制、字节限制和执行策略。
- 输入或任一限制变化时禁止复用。
- SQLite 可使用文件身份和快照信息判断复用。
- 没有可靠快照身份的 MySQL/PostgreSQL 默认不跨运行复用结果，避免把变化中的数据库误当成同一数据版本。
- 缓存命中必须生成可审计事件和产物引用，不能静默跳过查询。

#### 正式交付物

- 保留 Markdown 最终报告。
- 增加完整 HTML 分析报告，而不只是独立 HTML 图表。
- 增加 Excel 工作簿，至少包含摘要、指标、发现、建议、数据画像、血缘和受限结果样本。
- 原始大结果继续保留为独立 CSV，不默认完整嵌入 Excel，避免工作簿失控和敏感明细扩散。
- 三种报告必须绑定同一组已批准 Report、Review、血缘和图表版本。

#### 数据质量策略

- 增加用户确认的质量策略，支持唯一键、数值或日期范围、完整性、新鲜度、关联关系和指标口径检查。
- 策略必须显式声明字段、阈值、严重性和失败动作。
- 未声明业务语义时不根据列名自动猜测唯一键、 cohort 成熟度或业务范围。
- 质量发现进入 Review 和最终报告，阻断级问题不得被 Report 隐藏。

### 4.4 长期质量建设

总体状态：`DEFERRED`

- 扩充真实业务评测集，覆盖完整分析、指标设计、已有结果、SQL 安全、数据质量、回退和恢复。
- 记录每个角色的耗时、重试次数、用户修改次数以及宿主可提供的 Token 信息。
- 增加运行历史列表、单次运行诊断和常见失败原因汇总命令。
- 持续保留 v1.1、v1.2 和未来合同版本的读取、审计和显式迁移回归测试。
- 用户额度恢复后，正式比较以下三种方式：
  - 单 Codex：主任务独立完成，不启动子 Agent。
  - Codex 原生自由编排：允许 Codex 自行决定子 Agent 数量和协作方式，不使用本 Skill 的固定审批与合同。
  - 当前受控 Skill：使用动态路由、逐阶段确认、合同、状态和审计。
- 旧 v1.0 只保留为历史档案，不进入正式三模式结论。
- 真实评测任务数量需要在额度恢复后缩减并重新确认；当前不得按原八案例直接启动大规模重复运行。

### 4.5 DeepSeek 额度备用路线

总体状态：`DEFERRED`（用户已确认意图，技术兼容尚未验证）

#### 启用条件

- Codex 原生模型继续作为默认和发布基准。
- 只有 Codex 额度不足，并且用户明确要求当前任务切换到 DeepSeek 时才启用备用路线。
- 不允许根据额度猜测自动切换供应商；每次切换必须在任务开始前展示实际供应商和模型。
- Windows CI、MySQL、安装器等确定性测试不需要模型额度，应继续按原路线执行，不因 DeepSeek 备用方案而延迟。

#### 当前兼容性结论

- Codex 官方配置支持用户级自定义 `model_providers`，可以声明 `base_url` 和从环境变量读取的 `env_key`。
- Codex 当前自定义供应商的 `wire_api` 只支持 `responses`。
- DeepSeek 官方文档当前展示 OpenAI Chat Completions 和 Anthropic 兼容接口，示例入口为 `/chat/completions`，未证明其直接兼容 Codex 所需的 Responses 协议。
- 因此不能把 `https://api.deepseek.com` 直接填入 Codex 后就视为已经可用，也不能在未测试时将七个 Agent 的模型改为 DeepSeek。
- 参考资料：Codex [Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference)；DeepSeek [API Docs](https://api-docs.deepseek.com/)。

#### 候选接入方式

按以下优先级验证：

1. 若 DeepSeek 后续提供与 Codex 兼容的 Responses 端点，优先使用 Codex 用户级自定义 model provider 直接接入。
2. 若只有 Chat Completions/Anthropic 接口，则使用独立的本地协议适配层：对 Codex 暴露 Responses 接口，再把请求转换成 DeepSeek 支持的协议。
3. 若适配层不能完整保留流式输出、工具调用、结构化 JSON、取消、重试和 usage 元数据，则停止接入，不用普通 HTTP 文本调用冒充 Codex 原生 Agent。

供应商和认证配置属于机器本地配置。Codex 官方文档说明项目级 `.codex/config.toml` 不能覆盖 `model_provider` 或 `model_providers`，因此仓库只能提供无密钥示例，实际 provider 必须配置在用户级 Codex 配置或独立 profile 中。

#### 安全和审计要求

- API 密钥只通过 `DEEPSEEK_API_KEY` 等环境变量或本机安全存储提供，禁止写入仓库、Agent TOML、运行产物、测试日志或交接文档。
- 切换到 DeepSeek 前必须提示：发送给模型的业务问题、Schema、指标和阶段输入会离开 Codex 原生模型路径并发往所配置的外部 API。
- 每份执行回执必须记录实际 `provider`、`model`、接入方式、可获得的 usage 和缺失元数据原因。
- 通过外部 API 或协议适配层得到的响应不得标记为 Codex 原生 Agent 回执；只有宿主实际返回可见 Agent ID 时，才能继续声明原生子 Agent 身份。
- DeepSeek 响应仍必须通过现有 handoff Schema、角色 Schema、输入哈希、阶段审批和 Review 门，不能绕过状态机。
- Codex 原生和 DeepSeek 产物不得在同一未修订阶段内静默混用；供应商变化必须创建新 attempt 并进入确认流程。

#### 最小兼容 smoke

用户要求正式启用 DeepSeek 后，先用合成数据运行最小验证，不直接运行完整业务分析：

1. 验证用户级 provider/profile 能启动一个普通 Codex 任务。
2. 验证流式响应、取消、超时和错误重试。
3. 验证工具调用或等价的 Codex 执行动作不会因协议转换丢失。
4. 验证 Business 单阶段能返回严格符合 v1.2 Schema 的 JSON。
5. 验证 Business -> Metrics -> Review 三阶段逐次确认、独立 attempt 和执行回执。
6. 验证 API 密钥不会出现在原始响应、日志、状态、事件或安装 manifest 中。
7. 使用同一合成任务与 Codex 原生结果做一次质量和合同通过率对照。

只有上述 smoke 全部通过，DeepSeek 才可标记为 `PARTIAL` 可用；完成完整角色链、恢复和真实业务小样本后，才可标记为 `DONE`。DeepSeek 备用接入不是 v1.2.1 Release 的阻塞项。

## 5. 已确认的产品决策

以下决策已经由用户确认，后续 Codex 不应在没有新指令时重新改回：

| 决策 | 已确认选择 |
|---|---|
| 产品方向 | 继续数据分析专用 Codex Skill |
| 通用 Agent Skill | 不实施 |
| 外部模型 | Codex 原生优先；额度不足且用户明确启用时，允许验证 DeepSeek 备用后端 |
| MySQL 发布验证 | 使用 GitHub Actions 临时 MySQL 8.4 |
| 跨设备验收 | v1.2.1 明确延期，不再阻塞本次发布；未来仍使用真实第二台同账号 Codex 补验 |
| 正式评测模式 | 单 Codex、原生自由编排、当前受控 Skill |
| 旧 v1.0 | 只保留历史档案 |
| 评测执行 | 暂缓；发布后按模式分步进行，每次只在用户明确指令下启动一个模式 |
| 指标模板 | 个人层和项目层两层 |
| v1.3 开发顺序 | 先整理运行时内部结构，再增加功能 |
| v1.4 首批报告 | Markdown、HTML、Excel |
| 数据库扩展 | MySQL 验证完成后优先 PostgreSQL，不同时扩展大量数据库 |

额度限制是当前执行约束：用户明确表示 Codex 额度不足。在用户明确恢复或要求启用已通过兼容 smoke 的 DeepSeek 备用后端之前，不得启动真实三模式 Agent 评测或其他大规模子 Agent 测试。DeepSeek 尚未通过兼容验证，当前不能直接作为可用替代模型。

## 6. 下一任务恢复顺序

下一位 Codex 必须按以下顺序继续：

1. 阅读本文件并确认当前分支仍为 `agent/sequential-multi-agent-pipeline`，实现基线不早于 `83f2274`。
2. 检查工作区现有改动，保留用户或其他任务产生的文件，不进行无关回退。
3. 检查 PR `#1`、`v1.2.1` 标签和 GitHub Release：若发布操作尚未完成，使用已经通过的确定性发布证据完成发布，不重新启动 Agent 评测。
4. 第二台 Codex 验收保持 `DEFERRED`，除非用户之后明确要求补验。
5. 三模式 Agent 评测保持 `DEFERRED`。用户指定某个模式后，只运行该模式并保存独立结果；不得顺带启动另外两个模式。
6. 推荐评测顺序为单 Codex、Codex 原生自由编排、当前受控 Skill，但用户的新指令可以调整顺序。
7. 若改用 DeepSeek，先执行第 4.5 节最小兼容 smoke；DeepSeek 不改变 v1.2.1 的 Codex 原生发布基准。
8. v1.2.1 正式发布后，才开始 v1.3 的运行时内部整理和效率功能。

恢复任务汇报时必须分别说明：代码是否完成、自动测试是否通过、真实环境是否验证、是否已经正式发布。不得用“已上传”替代“已发布”，不得用 mock 测试替代真实 MySQL 结论。

## 7. 当前交接验收记录

2026-08-13 已完成不消耗模型额度的自动测试和发布收尾；未启动子 Agent、真实 Agent smoke、三模式评测或 DeepSeek 兼容测试。同日用户明确批准跳过第二台 Codex 验收并先发布，三模式评测改为发布后按用户指令一次执行一个模式。

| 检查项 | 预期 |
|---|---|
| 分支 | `agent/sequential-multi-agent-pipeline` |
| 实现提交基线 | `83f2274 Harden interrupted MySQL streams` |
| PR | `#1` 在本文档冻结时仍为草稿；已获用户发布授权 |
| 合同版本 | `1.2` |
| 发布标签 | 本文档冻结时 `v1.2.1` 尚未创建；已获用户发布授权 |
| 文档敏感信息 | 不包含密码、令牌、私钥或可用数据库连接串 |
| 本地测试执行 | `137 passed, 2 skipped`；安装器专项 `10 passed` |
| 干净安装 | 当前 Windows 中文目录和 GitHub Ubuntu/Windows 全部通过 |
| 远端 CI | Actions run `31656838504` 九项全部通过 |
| MySQL | MySQL 8.4.11 真实发布门完整通过，状态 `DONE` |
| 子 Agent | 本次不启动 |
| 功能代码 | 仅修改确定性发布门，不修改 Agent 角色能力或模型配置 |
| GitHub 推送 | 实现和证据已推送；本文档最终状态提交后再推送一次 |

后续如果仓库、分支、PR、测试结果、真实验证或发布状态发生变化，应在同一交付任务中更新本文件，避免下一次 Codex 依据过期状态继续工作。

## 8. v1.2.1 发布后评测进度（2026-08-13）

本节是发布后增量记录；与第 2、4、6、7 节的“发布前冻结状态”冲突时，以本节为准。

### 8.1 发布状态

- `DONE`：PR `#1` 已合并，合并提交为 `53fc58c`。
- `DONE`：`v1.2.1` 标签和正式 GitHub Release 已创建。
- `DEFERRED`：第二台 Codex 验收仍按用户决定跳过，不补写为已完成。

### 8.2 模式 2：Codex 原生自由编排

总体状态：单案例执行 `DONE`，模式覆盖 `PARTIAL`，三模式比较结论 `NOT_STARTED`。

- 用户已单独授权启动模式 2；模式 1 和模式 3 均未启动。
- 已冻结并执行 `metric-design-custom-edit` 案例的第 1 次重复，覆盖率为 `1/8`。
- 执行根使用 Codex 原生默认 Agent，不调用本项目 Skill、`growth-*` 自定义 Agent、审批门、数据库、SQL 或图表流程。
- 根 Agent 自主判断无需继续委派，因此本次没有后代子 Agent。这是自由编排的实际决策结果，不得写成已经验证并行子 Agent 协作。
- 已保存冻结输入、原始响应、宿主观察、执行清单和 SHA-256 绑定的 evaluation record。
- 评测专项自动测试为 `12 passed`；完整离线回归为 `139 passed, 2 skipped`。跳过项仍是显式 opt-in 的外部环境测试。
- 当前只确认路由角色覆盖和证据可解析，两项适用规则为 `2/6`；其余规则属于该案例或该模式不适用，记录为 `null`，不计为成功或失败。
- 质量分、幻觉数、权限违规数和 Token 统计均保持 `null`。只有其他模式达到相同案例覆盖后，才能进行模式盲评并形成比较结论。
- 宿主未向父任务暴露后代工具调用轨迹、实际模型标识和 Token 明细；记录中明确保留这些元数据缺口。

### 8.3 下一步约束

1. 当前停止评测，不顺带运行模式 1 或模式 3。
2. 用户下一次明确指定某个模式后，只执行该模式。
3. 若要形成三模式比较，后续模式必须复用同一冻结输入和等量重复次数。
4. 在三种模式同案例覆盖一致前，不发布优劣、质量或成本结论。

### 8.4 模式 3：受控 Skill 修复后重跑

总体状态：本案例 `DONE`；模式覆盖仍为 `PARTIAL`；三模式比较结论仍为 `NOT_STARTED`。

- 修复前运行：`eval-controlled-skill-metric-design-custom-edit-r01`。
- 修复前状态：`finalizing`，`completion_kind=null`；Business、Metrics、Review 均已批准，但终止器错误地强制要求 Report，因此没有完成。
- 修复后运行：`eval-controlled-skill-metric-design-custom-edit-r02`。
- 修复后状态：`completed`，`completion_kind=review_terminal`，`revision=39`，`audit.errors=[]`。
- 修复后仍使用相同冻结请求和路由：请求 SHA-256 为 `bf8fb064db57b37aafdbbaf8788dc87d976171c8af3b1fb8a117dc08acaae572`，路由 SHA-256 为 `4f747cc20eae26666b83f4dc82082d308676263b8c7acdfb6c224e690d461ea0`。
- 修复后未生成 Report，这是正确行为；该案例的合法终点是批准 Review，而不是为了满足旧终止器伪造 Report。
- 运行摘要、批准记录、证据哈希、Review 输入包和审计链均保留；中途的 Metrics 校验失败、Review 初次校验失败和中断尝试也保留在 attempt history 中。
- 修复后五项自动规则均通过：`sql_safe`、`schema_valid`、`evidence_resolvable`、`no_gate_bypass`、`recovery_success`。
- 新评测记录为 `tests/evals/results/controlled-skill/metric-design-custom-edit/repeat-01-fixed.json`；没有新的盲评，因此 `quality_scores=null`，不能据此宣称质量提升。
- 前后对照报告为 `tests/evals/results/controlled-skill/metric-design-custom-edit/repeat-01-fixed-comparison.md`。

### 8.5 本轮代码与回归验证

- `DONE`：Review-terminal、Report-terminal、非法终止、中断恢复、Metrics workbench 导出/导入/精确修订和 Review 输入继承测试已加入 `tests/test_runctl.py`。
- `DONE`：补充了相对路径调用 `finalize_run` 的回归测试，修复 CLI 路径解析导致的最终摘要写入失败。
- `DONE`：完整离线回归为 `146 passed, 2 skipped`；跳过项仍是显式 opt-in 的外部环境测试。
- `DONE`：`git diff --check` 和 Python 编译检查通过。
- `NOT_STARTED`：没有启动下一批真实 Agent 评测，没有进行新的盲评，也没有形成三模式优劣、质量或成本结论。

### 8.6 下一批评测建议

暂不扩展大批量用例。下一次只运行一个案例：`review-fail-rollback`，验证 Review 失败时能否定位最早责任阶段、使下游产物失效并完成精确返工。该案例通过后，再运行 `route-skip-unused-stages`，验证第二条最短路线。两者都完成后，再决定是否进入 SQL 或真实数据库案例。

## 8.7 当前发布与工作区整理状态（2026-08-13）

本节是当前发布记录。若前文冻结记录与本节冲突，以本节为准。

### 发布状态

- `DONE`：PR #2 已合并到 `main`，合并提交为 `9af6098abf26a81a7e45ea25e173a27bb0735b81`。
- `DONE`：整理提交通过 PR #3 已合并到 `main`，合并提交为 `7aa656b187679449bdb4980f4b62e4b6d9b5a311`。
- `DONE`：GitHub Release `v1.2.2` 已发布：[v1.2.2 Release](https://github.com/l15889778522-code/growth-insight-agent-skill/releases/tag/v1.2.2)。
- `DONE`：工作分支为 `agent/native-free-eval`，远端最新提交为 `8b35a1e6825979778506efb0f43965018686955c`。
- `DONE`：GitHub Actions 运行 `31678867821` 的 9 项 Ubuntu、Windows、全新安装和 MySQL 8.4 检查全部通过。
- `DONE`：最近一次本地完整回归记录为 `146 passed, 2 skipped`；本次整理没有启动新的模型评测。
- `UNCHANGED`：`VERSION=1.2` 仍是运行合同版本；`v1.2.2` 是补丁发布，不引入新的运行合同版本。

### 评测汇总状态

- `DONE`：固定 Skill 修复后记录加入结果目录后，评测汇总已重新生成。
- `DONE`：修复前失败的受控 Skill 记录作为历史证据保留，修复后的 Review-terminal 记录作为对照证据保留。
- `DEFERRED`：不宣称完整三模式质量结论。修复后受控 Skill 记录没有新的盲评质量分，早期原生自由编排运行的模型身份也无法完全验证。
- `DEFERRED`：下一批评测必须由用户明确批准，并在额度受限期间保持小规模。

### 工作区整理状态

- `DONE`：已增加 `.test-tmp-*/` 和 `tests/.artifacts/` 的 Git 忽略规则。
- `DONE`：`.coverage` 已删除。
- `DONE`：可访问的 `stages/`、`multi-agent-data-analysis-r02/` 和 `scripts/__pycache__/` 已删除。
- `BLOCKED`：剩余 `.test-tmp-*`、`tests/.artifacts` 和 `.pytest_cache` 生成目录在本次 Codex 会话中因 Windows 拒绝访问且当前会话没有管理员令牌，未能物理删除。它们是测试生成物，不是源文件；下一次应在管理员 PowerShell 中清理后再做全新工作区检查。
- `PRESERVE`：`multi-agent-data-analysis-runs/` 保存正式运行审计证据，不得删除。
- `PRESERVE`：`.venv/` 是本地运行环境，不属于本次清理目标。

### 发布后续顺序

1. 在管理员 PowerShell 中删除被 ACL 保护的测试生成目录。
2. 确认工作区只保留源代码、文档、评测证据和有意保留的发布元数据。
3. `DONE`：本次整理提交已合并到 `main`；后续只需在管理员 PowerShell 中完成本地 ACL 保护目录清理。
4. 发布与工作区记录一致后，再开始 v1.3。

## 8.8 Instagram 完整 Skill smoke 后加固（2026-08-21）

总体状态：代码与离线回归 `DONE`；真实 Agent/MySQL 修复后复验 `NOT_STARTED`；补丁发布 `NOT_STARTED`。

本次真实 smoke 覆盖 Business、Metrics、SQL、Insight 和 Review，并独立验证了 Review 失败时 Report 返回 `BLOCKED`。Visualization 按用户要求未测试。smoke 暴露了两类问题：

- 逻辑问题：活动用户分母被 `event_type` 分组缩小；已批准指标未全部映射到 SQL/结果/Insight；单日数据不足以证明趋势；测试用户排除策略缺失。
- 流程问题：Review 重试错误失效上游指标血缘；Review 启动前未强制检查血缘；`confirmed_decisions` 允许对象但运行语义要求精确继承；Agent 被迫重复粘贴全部历史决策；Review 输入包未绑定全部当前支持证据。

本轮已完成的代码加固：

- `DONE`：SQL 阶段必须映射全部批准指标或明确列为不支持；受支持指标必须进入可执行查询。
- `DONE`：SQL、Metrics、Insight、Review 角色规则增加分子/分母粒度、比较窗口覆盖、零值与缺失覆盖区分、测试用户处理要求。
- `DONE`：v1.2 `confirmed_decisions` 统一为唯一非空字符串，只记录当前阶段新增或明确重申的决策；不再允许包装对象。
- `DONE`：取消下游 Agent 重复抄写全部历史决策的要求。批准决策继续由批准产物继承，Review 由不可变输入包集中读取。
- `DONE`：Review 输入包增加当前指标血缘、查询 manifest、查询结果、结果 profile 和图表 manifest 的路径及 SHA-256 绑定。
- `DONE`：包含 Metrics 的路线在缺少当前有效血缘时，`start-stage` 在启动 Review Agent 前拒绝执行。
- `DONE`：仅重试 Review 且 Review/Report 尚无产物时，不再误删上游血缘；需要时允许在 `revising` 状态重建血缘。
- `DONE`：Review 合法输出 `FAIL` 后仍登记不可变 Review artifact，并创建哈希绑定的 rollback plan；正式 Report 继续被阻止。

验证记录：

- 针对性回归：`44 passed`。
- 完整离线回归：`152 passed, 2 skipped`。
- Python 编译检查：通过。
- `git diff --check`：通过。
- 跳过项仍为显式 opt-in 的外部环境测试；本轮没有消耗模型额度运行真实 Agent，也没有重新执行 MySQL 查询。

仍需后续完成：

1. 使用新的 SQL attempt 修复 period 级总体活跃用户分母，并重新走 SQL 审批与查询审批。
2. 使用覆盖多个自然日的数据重新执行查询，确认 baseline、comparison、recent 均有完整覆盖。
3. 重新运行 Insight 与 Review，验证不会把缺失期间转换为零值趋势，也不会遗漏已批准指标。
4. Review 通过后再测试正式 Report；Visualization 仍需用户另行授权测试。
5. 当前工作分支为 `agent/native-free-eval`，远端基线提交为 `ba2d9ab`。本节记录的是未提交工作区改动，不得写成已推送、已合并或已发布。

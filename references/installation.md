# 安装、升级与卸载

`scripts/install_skill.py` 是 Windows、Linux 和 macOS 共用的安装入口，仅依赖 Python 3.11+ 标准库。PowerShell 脚本保留为兼容包装。

## 常用命令

```powershell
python scripts/install_skill.py install --scope personal
python scripts/install_skill.py install-agents --scope personal
python scripts/install_skill.py verify --scope personal
python scripts/install_skill.py verify-agents --scope personal
```

项目级安装可使用 `--scope project --project-path <path>`，也可用 `--destination <path>` 指定测试目录。`project-path` 必须是 Codex 实际打开的工作区根目录；如果仓库只是当前工作区的嵌套子目录，仓库内 `.codex/agents` 不足以证明运行时会优先加载它，应改用个人级 Agent 安装或单独打开该仓库。安装运行依赖时使用 `install --install-dependencies`。安装器在最终目标路径创建 `.venv`，依赖版本由 `constraints.txt` 约束。

`--dry-run` 只读取和校验源文件与现有安装，不创建目标目录、staging、manifest 或虚拟环境。PowerShell 的 `-DryRun` 和 `-WhatIf` 都会映射到这一行为。

```powershell
python scripts/install_skill.py install --destination C:\very\deep\中文路径\skill --dry-run --json
powershell -File scripts/install_skill.ps1 -Scope Project -ProjectPath C:\work\project -DryRun
```

## 事务与长路径

安装器优先在与目标相同文件系统的短目录创建 `.mada-s-*` staging，完整复制并校验 SHA-256 后再执行目录交换。升级会先把旧目录改名为 `.mada-b-*` 备份；新版提交或依赖安装失败时，安装器把已迁移的用户文件移回备份并恢复旧目录。这样避免 PowerShell 递归复制在 Windows 深层或中文路径下额外放大路径长度。

捕获到的文件系统错误和依赖安装错误会触发回滚。进程被强制终止、系统断电或文件系统本身损坏不在进程内回滚保证范围内；此时应先检查 staging 根目录中残留的 `.mada-b-*` 和 `.mada-s-*`，确认内容后再处理。

## Manifest 与完整性

Skill manifest 只列出运行时必要资源：`VERSION`、`SKILL.md`、运行脚本、schemas、references、Agent TOML、报告模板、运行依赖与约束。`VERSION` 是 Python 运行时和安装 manifest 的当前合同版本源。仓库 `README.md`、`run_evals.py`、测试、评估夹具和 `skill-test-harness` 不会安装。

每个托管文件记录相对路径、大小和 SHA-256；`package_sha256` 由排序后的托管清单确定。`verify` 同时对照当前源包、manifest 和目标文件。未列入 manifest 的文件不参与包完整性计算。

升级前若托管文件被本地修改、丢失，或新的托管路径与现有用户文件冲突，安装器会停止。只有确认要替换这些路径后才使用 `--force`（PowerShell 为 `-Force`）。

## 用户文件策略

目标目录中未列入旧 manifest、且不与新版托管路径冲突的文件和目录视为用户文件，升级时原样保留。Skill 的 `.venv` 和 `scripts/__pycache__` 是可变但由安装器拥有的路径：普通升级会保留 `.venv` 并清理可再生成的 Python 缓存；带 `--install-dependencies` 的升级会重建 `.venv` 并在失败时恢复旧环境。

默认卸载移除 manifest 托管文件、Skill `.venv` 和可再生成的 `scripts/__pycache__`，其他用户文件仍留在目标目录。使用 `--keep-environment` 可在卸载 Skill 时保留 `.venv`。`--purge --force` 会删除目标中的未托管用户文件，属于显式破坏性操作。

```powershell
python scripts/install_skill.py uninstall --scope project --project-path C:\work\project
python scripts/install_skill.py uninstall-agents --scope project --project-path C:\work\project
```

Agent 目录通常与其他配置共享。`uninstall-agents` 默认只移除该 manifest 管理的七个 TOML 和 manifest，并保留其他 Agent 文件。

静态 `verify-agents` 和预检只证明磁盘文件及哈希一致。安装或升级后必须启动新的 Codex 任务并真实调用目标角色；只有返回的合同版本、Agent ID 和执行回执都通过运行时校验，才能认定角色已加载。

## CI 边界

GitHub Actions 在 Windows 与 Linux 上分别运行 Python 3.11、3.12、3.13，共六个组合。依赖安装同时读取 `requirements-dev.txt` 和 `constraints.txt`。测试使用临时 SQLite 数据库；MySQL 仅验证 mock 驱动契约，不读取 secrets，也不连接真实 MySQL。

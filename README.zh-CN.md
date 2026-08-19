<p align="center">
  <a href="./README.md">English</a> | <strong>中文</strong>
</p>

# Agent Session Vault

Agent Session Vault 是运行在 OPL Fleet 上的本地优先会话投影、Tokscale 聚合与
Codex 完整归档任务。

默认产品合同刻意保持精简：从每个已批准的 Fleet 节点构建体积小、可复现的统计投影，
不让 Tokscale 读取客户端实时目录，并且只在控制节点提交一次。完整会话归档是另一条
需要显式执行的工作流。

## 职责

| 负责人 | 职责 |
| --- | --- |
| OPL Fleet | 节点清单、网络路由、准入、任务投放与产物传输 |
| Agent Session Vault | 投影语义、导入、stable analytics 副本、Tokscale 执行与 Codex 归档 |
| Tokscale | 下游用量计算与提交 |
| Codex、Gemini CLI、OpenClaw、Antigravity IDE、ZCode | 各自的实时会话目录和 API |

跨机只有一条路径，Tokscale 只有一个受管视图，两者都由本仓负责，不在本机配置中重复维护。

## 安装

```bash
git clone <your-repo-url> agent-session-vault
cd agent-session-vault
uv tool install --python 3.12 --editable .
```

创建本机配置：

```bash
mkdir -p ~/.config/agent-session-vault
cp config/agent-session-vault.example.toml \
  ~/.config/agent-session-vault/config.toml
agent-session-vault config --json
```

真实机器路径、归档位置、回执、投影和会话数据都保存在仓库外。

## 日常使用

刷新控制节点和全部已批准的 Fleet 节点：

```bash
agent-session-vault sync fleet --json
```

检查或运行 Tokscale 的官方预览：

```bash
agent-session-vault tokscale env --json
agent-session-vault tokscale exec -- submit -c codex,gemini,openclaw,antigravity,zcode --dry-run
```

执行完整的每日同步与单次提交：

```bash
agent-session-vault ops daily-tokscale --mirror-stable --json
```

归档完整 Codex 会话，但不删除本机源文件：

```bash
agent-session-vault archive init --json
agent-session-vault ops archive-cycle --due-only --deep --json
```

本机裁剪与恢复都必须经过显式计划。执行前请阅读操作手册。

## 当前保证

- Tokscale 只读取受管投影和受管 extras，不读取客户端实时目录。
- 只有控制节点执行一次聚合提交，Fleet 节点不会分别提交。
- 上游节点删除旧文件时，已导入的投影仍保留累计用量历史。
- stable 层用于恢复统计连续性，不参与日常运行读取。
- 完整归档使用不可变 snapshot object、catalog、深度校验、staging 恢复和受控裁剪。
- 云同步目录或 NAS 只是存储位置，不是客户端实时目录。

## 文档

- [文档职责与生命周期](docs/README.md)
- [中文操作手册](docs/zh/OPERATIONS.md)
- [中文配置参考](docs/zh/CONFIGURATION.md)
- [中文架构说明](docs/zh/ARCHITECTURE.md)
- [Operations guide](docs/en/OPERATIONS.md)
- [Configuration reference](docs/en/CONFIGURATION.md)
- [Architecture](docs/en/ARCHITECTURE.md)
- [Primary Agent Skill](skills/agent-session-vault/SKILL.md)

## 验证

```bash
ruff check .
python3 -m pytest
```

本项目采用 [Apache License 2.0](LICENSE)。

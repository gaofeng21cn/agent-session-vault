---
name: agent-session-vault
description: 管理 OPL Fleet 的多机会话投影、Tokscale 统计、stable analytics 恢复、Codex 完整归档、本机冷数据裁剪与 staging 恢复。用户提到会话历史、Tokscale、Codex 磁盘回收或 NAS 会话归档时使用；不用于修改上游客户端。
---

# Agent Session Vault

本 Skill 只负责把用户任务路由到仓库拥有的 `agent-session-vault` CLI，并守住操作授权边界。
仓库源码、配置 schema、CLI help 和运行回执是执行合同；不要在 Skill 中重写同步、投影、
归档或 Tokscale 实现。

## 开始

先确认 CLI 和生效配置，除非当前任务已经提供同一次运行的可信回执：

```bash
command -v agent-session-vault
agent-session-vault config --json
```

CLI 不存在时说明安装缺失并停止；只有用户明确要求安装时才安装。真实配置默认位于
`~/.config/agent-session-vault/config.toml`，不要输出完整私有配置。

OPL Fleet 是节点、网络、准入、任务投放和产物传输的唯一来源。不要复制节点清单、读取 Fleet
私有 route，或用裸 IP 替换既有 node identity。

## 任务路由

| 用户目标 | CLI 路径 | 授权边界 |
| --- | --- | --- |
| 查看配置、存储、Tokscale 环境或归档状态 | `config --json`、`storage summary --json`、`tokscale env --json`、`archive list --json` | 只读 |
| 刷新多机统计输入 | `sync fleet --json` | 更新本机和 Fleet projection/import 状态 |
| 导入明确的临时 Codex home | `sync local-codex --source <root> --json` | 只写受管 local extras |
| 预览 Tokscale 提交 | `tokscale exec -- submit -c codex,gemini,openclaw,antigravity,zcode --dry-run` | 运行官方 preview，不提交 |
| 正式每日统计提交 | `ops daily-tokscale --mirror-stable --json` | 只有用户明确要求提交时执行 |
| 创建或验证完整 Codex 快照 | `ops archive-cycle --due-only --deep --json` | 写 archive；不删除本机会话 |
| 恢复 analytics stable 层 | `storage restore-stable --dest-root <staging> --json` | 只写独立目标 |
| 查找和恢复旧会话 | `archive plan-restore ... --plan-path <plan> --json`，再 `archive restore --plan <plan> --json` | 只恢复到 staging |
| 回收本机冷归档空间 | `archive prune-plan ... --json`，再 `archive prune-apply ... --json` | 仅限用户直接要求清理或已明确授权的自动化 |

所有自动化或需要机器读取回执的调用都传 `--json`。报告实际 status、关键数值、snapshot/plan id
和 receipt/restore path；字段缺失时写 `unavailable`，不要推测成功。

## Projection 与 Tokscale

跨机只有 `sync fleet` 一条路径。逐节点读取结果，不为失败或跳过的节点建立第二套配置或传输面。

Tokscale 只读取 Vault 准备的一份受管 projection 和 managed extras，不直接读取 live `HOME`。
不要裸跑 `tokscale`，也不要 patch Tokscale、Codex、Gemini CLI 或 OpenClaw 上游。

`tokscale exec --dry-run -- ...` 只打印 Vault 将执行的命令；官方统计预览必须把 `--dry-run`
放到 `--` 之后传给 Tokscale。需要指定 Tokscale 包时，只为当前命令设置
`AGENT_SESSION_VAULT_TOKSCALE_PACKAGE=<package>`。

## Stable 与完整归档

默认 stable profile 是统计恢复层，不是日常读取面。只有用户明确要求完整换机会话副本，并且
客户端写入已停止时，才使用 `storage mirror-stable --include-live-sessions`。

完整 Codex archive 是独立的 snapshot/catalog 域，不是 live root，也不能符号链接到
`~/.codex`。`archive-cycle` 只 snapshot、publish、deep verify，不做本机裁剪。

恢复只允许 staging。不得手工把恢复文件复制进 `~/.codex`，不得合并
`session_index.jsonl`。

## 裁剪授权

只有用户明确目标是释放本机磁盘，或存在已授权的周期任务时，才能进入裁剪。必须先生成并审阅
带 digest 的 plan。计划与执行必须证明：archive deep verification、stable imports coverage、
projection coverage、无 live/external hard link，以及删除前后官方 Tokscale preview 一致。

任一校验失败、候选为空或状态改变时都停止并报告；不得改用 `rm`、移动 source 或绕过 plan。

## 禁止的捷径

- 不把 NAS、OneDrive 或 iCloud 当作 live client root。
- 不直接删除、移动或链接 `~/.codex`、`sessions` 或 `archived_sessions`。
- 不把 projection 同步隐式升级为完整归档、stable live migration、裁剪或 Tokscale 提交。
- 不把 stable analytics、测试通过或 snapshot 存在单独当作可恢复或可删除证明。

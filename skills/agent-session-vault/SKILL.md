---
name: agent-session-vault
description: 管理 OPL Fleet 的多机会话同步、Tokscale 统计、Codex 完整归档、冷数据裁剪与 staging 恢复。用户提到会话历史、Tokscale、Codex 磁盘回收或 NAS 会话归档时使用；不用于修改上游客户端。
---

# Agent Session Vault

本 Skill 只路由到仓库拥有的 `agent-session-vault` CLI。仓库是命令语义、配置 schema
和操作合同的唯一来源；CLI 是唯一执行者和状态负责人。不要重写同步、投影、归档或
Tokscale 调用逻辑。

## 开始

先确认 CLI 与生效配置，除非当前任务已经给出了同一运行的可信 receipt：

```bash
command -v agent-session-vault
agent-session-vault config --json
```

若 CLI 不存在，说明安装缺失并停止；只有用户明确要求安装时才安装。真实配置默认位于
`~/.config/agent-session-vault/config.toml`，不读取或展示其完整内容。

OPL Fleet 是节点、网络、准入和任务投放的唯一来源。不要复制节点清单、读取 Fleet 私有
route，或改用裸 IP 替换既有 machine identity。

## 按任务路由

| 用户目标 | 使用的 CLI 路径 | 写入边界 |
| --- | --- | --- |
| 查看配置、存储或归档状态 | `config --json`、`storage summary --json`、`archive list --json` | 只读 |
| 同步多机统计输入 | `sync fleet --json` | 只更新 projection/import 状态 |
| 预览 Tokscale 统计 | `tokscale exec --mode raw -- submit -c codex,gemini,openclaw --dry-run` | 不提交 |
| 真正的日常统计提交 | `ops daily-tokscale --mirror-stable --json` | 仅在用户明确要求提交时使用 |
| 创建/验证完整 Codex 会话快照 | `ops archive-cycle --due-only --deep --json` | 写 NAS 快照；不删除本机会话 |
| 回收本机冷归档空间 | `archive prune-plan ... --json`，再 `archive prune-apply ... --json` | 仅在用户直接要求清理，或明确授权的周期自动化中 |
| 查找与恢复旧会话 | `archive plan-restore --mode staging ... --json`，再 `archive restore --plan ... --json` | 只恢复到 staging |

所有自动化或需要机器读取回执的调用都传 `--json`。报告实际状态、关键数值、snapshot 或
plan id、以及 receipt/restore 路径；缺失字段写 `unavailable`，不要猜测成功。

## Tokscale

Tokscale 只读取 Vault 准备的 projection 与 managed extras，不直接读取 live `HOME`。不要
裸跑 `tokscale`，也不要 patch Tokscale、Codex、Gemini CLI 或 OpenClaw 上游。

默认走 `projection-first`。`sync auto <machine>` 只用于用户明确要求的旧配置兼容或单机诊断。
需要更严格的内部口径时，使用：

```bash
agent-session-vault tokscale exec --mode canonical --omx-replay-dedupe strict -- submit -c codex,gemini,openclaw --dry-run
```

## 完整会话归档与裁剪

完整会话归档是独立于 Tokscale projection 的 NAS 快照域。它不是 Codex live root，也不应被
符号链接到 `~/.codex`。普通归档使用 `archive-cycle`；它只 snapshot、publish、deep verify。

只有用户明确目标是释放本机磁盘，或已有已授权的周期任务，才能进入裁剪。先生成带 digest
的 plan；plan 必须证明 NAS 深度校验、稳定 analytics 覆盖、projection 覆盖、无外部硬链接和
pinned Tokscale dry-run 一致。任何校验失败、候选为空或删除前后 Tokscale 不一致都停止，不能
改用 `rm`、`offload-tree` 或绕过 plan。

不要把 `sessions`、live client roots 或未明确指定的 raw tree 当成可裁剪目标。

## 恢复

恢复先查 catalog，再生成带 digest 的 staging plan。当前 `codex-live` 恢复尚未启用，即使 CLI
展示该参数也不要请求或尝试它；不要手工复制恢复文件进 `~/.codex` 或合并 `session_index.jsonl`。

## 禁止的捷径

- 不把 NAS、OneDrive 或 iCloud 当作 Codex live 根。
- 不对 `~/.codex`、`sessions` 或 `archived_sessions` 直接 `rm`、移动或创建符号链接。
- 不把日常 projection 同步隐式升级为完整归档、裁剪或 Tokscale 提交。
- 不把 stable analytics 备份说成完整正文的 live migration。

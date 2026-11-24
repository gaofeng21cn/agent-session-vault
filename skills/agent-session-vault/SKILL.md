---
name: agent-session-vault
description: 仅在用户明确要求 OPL Agent Session Vault 的多机同步、Tokscale 统计、Codex 会话归档或 staging 恢复、本机冷数据裁剪时使用。不要因一般对话历史、侧边栏任务恢复、普通磁盘清理或客户端设置触发；不用于修改上游客户端。
---

# Agent Session Vault

本 Skill 只负责把用户任务路由到仓库拥有的 `agent-session-vault` CLI，并守住操作授权边界。
仓库源码、配置 schema、CLI help 和运行回执是执行合同；不要在 Skill 中重写同步、投影、
归档或 Tokscale 实现。

## 开始

从当前 Skill 的 `../..` 定位已安装 Package 根目录，使用同包的 `vault.py` 调用 owner。
它直接执行随包提供的 CLI，无须再安装另一份 Python 工具。需要 Python 3.12 或更高版本。
先确认入口和生效配置，除非当前任务已经提供同一次运行的可信回执：

```bash
python3 <package-root>/vault.py --help
python3 <package-root>/vault.py config --json
```

下表中的 `agent-session-vault` 均指上述同包入口；独立 CLI 安装仍供终端用户使用。
Package 入口或 Python 版本不可用时报告所缺依赖。真实配置默认位于
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
| 查看 `restic` 仓库去重与存储统计 | `archive content-status --json` | 只读已提交快照元数据与 `restic stats`，不修改仓库 |
| 只读检测旧版 `archive-v1` 存量仓库 | `archive legacy-status --json` | 纯只读；不写入、不加锁，供迁移前盘点 |
| 显式整理或按策略裁剪 `restic` 归档仓库 | `archive compact [--dry-run] [--keep-last N] --json` | 仅在用户授权后执行；调用 `restic prune` / `forget --prune` + `check --read-data` |
| 已授权双周维护 | `ops archive-maintenance --json`；`--status --json` 只读核对 | 各阶段只启动一次；含严格保护的会话裁剪 |
| 冷数据完整批次下沉 | `ops cold-inventory` → AI 用途审阅 → `ops cold-plan` → `ops cold-pack` → `ops cold-cleanup`，均带本轮 `--run-dir`、`--json` | 仅限明确空间优化授权；先核验 TAR 与恢复样本，再清空本轮专属 Trash |
| 恢复 analytics stable 层 | `storage restore-stable --dest-root <staging> --json` | 只写独立目标 |
| 审阅既有计划、覆盖和收据 | `archive review --plan <plan> --expected-revision <plan_digest> --json`；共享视图 `vault.archive.review` | 只读；刷新不重试，partial/unknown 先对账 |
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

完整归档与 stable 恢复层均由 `restic` 承接底层 CDC 分块去重、zstd 压缩、加密和仓库锁。
重复运行固定来源或路径迁移不新增数据块；回执 `dedup` 同时报告逻辑文件复用与 `restic` 实际块增长。
遇到旧版 `agent-session-vault-archive-v1` 根目录时，只使用 `archive legacy-status --json` 只读检查，
绝不原地改写；另建 `restic-v2` 根目录并完成深检后再决定旧目录去留。maintenance fence 存在时只读
核对原 runner、日志与真实 `exit_code`，不移除 fence 绕过保护。

恢复只允许 staging。不得手工把恢复文件复制进 `~/.codex`，不得合并
`session_index.jsonl`。

## 裁剪授权

只有用户明确目标是释放本机磁盘，或存在已授权的周期任务时，才能进入裁剪。必须先生成并审阅
带 digest 的 plan。计划与执行必须证明：archive deep verification、stable imports coverage、
projection coverage、无 live/external hard link，以及删除前后官方 Tokscale preview 一致。

任一校验失败或状态改变时都停止并报告；不得改用 `rm`、移动 source 或绕过 plan。
新计划为空只跳过会话 prune-apply；已授权的冷数据阶段仍可继续。

双周维护和冷数据流程按 [OPERATIONS](../../docs/zh/OPERATIONS.md#双周维护与冷数据下沉) 执行，
复用正式 CLI，不重新生成上轮一次性脚本。detached runner 缺少真实退出码或终态 JSON 时，只读
核对原日志，不能用 receipt/state 推断退出码、重发命令或继续删除。用途和完成状态由 AI 结合证据
判断，代码检查路径范围、30 天整批冷度、当前引用、进程、源变化及 SHA-256。
保留当前配置、automation 控制面、活跃工作与在线数据库；逻辑清空、移入 Trash 与物理空闲量分别报告。

## 禁止的捷径

- 不把 NAS、OneDrive 或 iCloud 当作 live client root。
- 不直接删除、移动或链接 `~/.codex`、`sessions` 或 `archived_sessions`。
- 不把 projection 同步隐式升级为完整归档、stable live migration、裁剪或 Tokscale 提交。
- 不把 stable analytics、测试通过或 snapshot 存在单独当作可恢复或可删除证明。

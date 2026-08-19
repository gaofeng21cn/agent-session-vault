# 操作手册

本文档只负责支持的命令流程、操作检查、回执和破坏性操作边界。配置字段见
[配置参考](CONFIGURATION.md)，职责和数据流见[架构说明](ARCHITECTURE.md)。

## 前置条件

- 已按根 README 完成安装
- 可读取的本机配置
- 用于跨机投影任务的 OPL Fleet
- Tokscale 所需的 `npm` 与 `npx`
- stable 打包和完整归档所需的 `bsdtar` 与 `zstd`
- 执行归档前已挂载且可写的 archive root

如需使用非默认配置文件，把 `--config <path>` 放在顶层命令之前。

## 只读检查

诊断时先使用范围最小的相关回读：

```bash
agent-session-vault config --json
agent-session-vault storage summary --json
agent-session-vault tokscale env --json
agent-session-vault archive list --json
```

`tokscale env` 只报告受管投影环境，不刷新投影，也不运行 Tokscale。

## 刷新投影

日常跨机命令会刷新控制节点投影，通过 OPL Fleet 在所有已批准的候选节点运行投影任务，
导入返回的产物，并逐节点报告结果：

```bash
agent-session-vault sync fleet --json
```

必须查看各项 `results[].status`。Fleet 无法准入或完成的节点会连同 Fleet 回执保留在结果中；
本仓不会为它另建一套机器配置。

只诊断当前机器输入时，可以单独刷新本机：

```bash
agent-session-vault sync local-home-projection --json
```

如果临时 Codex home 不在标准实时目录中，需要显式导入到受管、只增不减的 extras：

```bash
agent-session-vault sync local-codex --source /path/to/runtime-root --json
```

多个明确来源可以重复传入 `--source` 或 `--source-glob`。缺失来源会出现在
`missing_sources` 中；命令不会从历史 runtime 或 cold archive 布局中静默发现来源。

## 运行 Tokscale

先检查实际生效的 projection-only 环境：

```bash
agent-session-vault tokscale env --json
```

在该视图上运行 Tokscale 官方预览：

```bash
agent-session-vault tokscale exec -- submit -c codex,gemini,openclaw,antigravity,zcode --dry-run
```

`--` 后面的参数全部传给 Tokscale。相对地，把 Vault 自己的 `--dry-run` 放在分隔符前，
只会打印 `npx` 调用：

```bash
agent-session-vault tokscale exec --dry-run -- submit -c codex,gemini,openclaw,antigravity,zcode
```

除 Vault 自身的命令打印 dry run 外，`tokscale exec` 会先刷新本机 HOME 投影，再调用
Tokscale。不要裸跑 `tokscale`，否则会绕过受管 HOME 和 extra roots。

人工运行需要固定版本时，只为该次调用指定包：

```bash
AGENT_SESSION_VAULT_TOKSCALE_PACKAGE=tokscale@<version> \
  agent-session-vault tokscale exec -- submit -c codex,gemini,openclaw,antigravity,zcode --dry-run
```

## 每日聚合提交

正式操作入口会刷新本机和 Fleet 投影、验证结果视图、解析当前 Tokscale 包、在新提交合同
出现时检查官方 help 和 preview，并且只从控制节点提交一次：

```bash
agent-session-vault ops daily-tokscale --mirror-stable --json
```

这是一次真实的外部提交，只有确实要提交时才能运行。命令使用进程锁，并在配置文件所在目录
的 `ops/daily-tokscale` 下写入 `current.json`、逐次日志和终态 `receipt.json`；
`--run-root` 可以覆盖该位置。

投影前，runner 会使用同一个当前 Tokscale 包在每个可用节点刷新 Antigravity IDE 官方 RPC
cache。Antigravity language server 不可用时记为 `skipped_unavailable`，但不会丢弃旧 cache。
ZCode SQLite 会先生成一致快照，再转为只含用量的 JSONL；不会从数据库复制对话正文。

只有 `status: confirmed` 加本次回执的统计值才能证明本轮确认完成。`already_running`、
`failed`、`unconfirmed` 和 `incomplete_receipt` 都不等于提交成功。stable mirror 告警不会
推翻已经确认的提交，需要单独检查 `stable_mirror.status`。

## Stable Analytics 恢复层

先预览，再创建 imports、受管 extras、配置和 Tokscale custom pricing 的 packed、verified
恢复副本：

```bash
agent-session-vault storage mirror-stable --dry-run --json
agent-session-vault storage mirror-stable --json
agent-session-vault storage migration-plan --json
```

只能从 verified stable manifest 恢复，并且必须写到独立目标；已存在的目标 item path 会被拒绝：

```bash
agent-session-vault storage restore-stable \
  --dest-root /path/to/restore-staging \
  --json
```

重复传入 `--label` 可以只恢复指定项目。默认 stable profile 只保证 analytics continuity。
`--include-live-sessions` 是显式迁移副本，不是日常 Tokscale 输入，也不替代 Codex archive
catalog；使用前必须停止客户端写入。

## 完整 Codex 归档

先初始化配置的 archive root，之后运行按周期判断的归档：

```bash
agent-session-vault archive init --json
agent-session-vault ops archive-cycle --due-only --deep --json
```

归档周期会扫描配置的 Codex 来源、创建增量不可变对象、发布 snapshot、重建 catalog segment、
执行深度校验并写入回执。它绝不裁剪本机来源。`not_due` 是成功的空操作；`partial` 需要检查，
并返回非零退出码。

恢复或诊断时也可以逐阶段执行：

```bash
agent-session-vault archive snapshot --json
agent-session-vault archive publish --staging-root /path/from/snapshot-output --verify-staged --json
agent-session-vault archive verify --snapshot <snapshot-id> --deep --json
agent-session-vault archive catalog-rebuild --json
```

可按时间、机器、会话或来源查询 catalog，例如：

```bash
agent-session-vault archive list --session-id <session-id> --json
```

## Staging 恢复

根据 archive catalog 生成带 digest 的计划，审阅后恢复到 staging 目录：

```bash
agent-session-vault archive plan-restore \
  --destination /path/to/restore-staging \
  --session-id <session-id> \
  --plan-path /path/to/restore-plan.json \
  --json
agent-session-vault archive restore --plan /path/to/restore-plan.json --json
```

当前唯一恢复模式是 staging。命令校验 archive member 和恢复文件 checksum，不会合并到实时
Codex home。

## 本机裁剪

裁剪是唯一的破坏性流程。先停止 Codex 写入，再生成并审阅计划，最后应用同一份计划：

```bash
agent-session-vault archive prune-plan \
  --plan-path ~/.config/agent-session-vault/prune-plans/current.json \
  --json
agent-session-vault archive prune-apply \
  --plan ~/.config/agent-session-vault/prune-plans/current.json \
  --json
```

计划只接纳同时被当前 deep-verified archive snapshot、当前 projection 和 verified stable imports
mirror 覆盖的冷 `archived_sessions` 文件；共享 hard link 和外部 hard link 会被拒绝，并记录
Tokscale 官方 preview。执行时再次校验 plan digest、来源 metadata、checksum、archive snapshot、
stable coverage，以及删除前后的 Tokscale parity。任一前提变化都必须重新生成计划，不得手工删除来源。

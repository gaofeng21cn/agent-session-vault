# 操作手册

本文档只负责支持的命令流程、操作检查、回执和破坏性操作边界。配置字段见
[配置参考](CONFIGURATION.md)，职责和数据流见[架构说明](ARCHITECTURE.md)。

## 前置条件

- 已按根 README 完成安装
- 可读取的本机配置
- 用于跨机投影任务的 OPL Fleet
- Tokscale 所需的 `npm` 与 `npx`
- 去重加密的 stable 恢复副本与完整归档所需的 `restic`
- 辅助冷数据批次打包及 `.jsonl.zst` 解析所需的 `bsdtar` 与 `zstd`
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

定时任务在任何写入前先运行 NAS 预检，确认 `Personal-Drive` 挂载、归档身份以及
`archive/stable/staging` 路径。预检把 SMB 路径检查放在独立 worker 中，并有 45 秒硬超时；
若返回 `status: blocked` 或超时，任务必须停止在提交前，并根据输出的 `phase` 恢复文件访问。
预检未返回 `status: ready` 时不得启动下面的真实提交命令。

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
如果官方日志明确报告 `trajectory not found`，runner 会把已确认且已陈旧的本地候选移入
带 manifest 的可恢复 quarantine，最多重试一次官方同步；近期或无法证明陈旧的会话不会移动。
回执中的 `source_sync.antigravity.repair` 和 `attempts` 记录修复与重试证据。
ZCode SQLite 会先生成一致快照，再转为只含用量的 JSONL；不会从数据库复制对话正文。

只有 `status: confirmed` 加本次回执的统计值才能证明本轮确认完成。`already_running`、
`failed`、`unconfirmed` 和 `incomplete_receipt` 都不等于提交成功。stable mirror 告警不会
推翻已经确认的提交，需要单独检查 `stable_mirror.status`。

指定 `--mirror-stable` 且本机投影成功后，runner 在每条终态路径上都会尝试镜像，包括 Fleet
发现或提交失败；镜像覆盖失败前已成功生成的投影。镜像成功不证明数据已提交到 Tokscale，
镜像失败也不会覆盖主运行结果。
请求发出后的传输中断意味着送达状态未知；runner 不会自动重试真实 submit。
提交未确认时，`tokscale.submit_error` 保留 CLI 的实际错误及可用的传输细节，并写入
`error.message`。定价告警不等于提交失败；进一步诊断以完整的 `submit_log` 为准，
服务端报错也不能证明数据一定没有写入。

本机调度器应调用一个固定启动脚本，由脚本负责工作目录、只读 HTTPS 检查、原样 NAS 预检、
完整的 Python detached 启动命令、启动锁和持久启动记录，提示词不再拼接业务命令。
仅只读连接检查可以重试；存活或状态未明确的原启动必须继续核对。脚本提供只读状态查询，
绑定原 runner 的 receipt 和真实退出码。提交端点对 GET 返回 405 只证明 HTTPS 连接可用，
不证明 POST 已送达。部署路径、机器身份、日志和调度配置均留在仓库外。

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

`--due-only` 先按路径和内容哈希检查 `archived_sessions` 文件是否已被该来源最新的
deep-verified snapshot 覆盖；存在未覆盖或已变化的历史 session 时立即执行，不等待
`cadence_days`。全部历史 session 已覆盖时，`cadence_days` 才作为保底周期。

归档周期会扫描配置的 Codex 来源、将固定后的文件备份至 `restic` 仓库（`<archive.root>/repo`）、
提交带校验的 snapshot manifest、生成 catalog segment、执行深度校验（`restic ls` +
`restic check --read-data`）并写入回执。它绝不裁剪本机来源。`not_due` 表示保底周期尚未到达且历史
session 已全部覆盖，是成功的空操作；`partial` 需要检查，并返回非零退出码。

`restic` 原生提供内容分块去重（CDC）、zstd 压缩、认证加密与并发锁。会话从 `sessions` 移到
`archived_sessions`，或重复运行固定来源时，不新增数据块；回执同时报告逻辑文件复用统计
（`reused_files`、`reused_logical_bytes`、`historical_reused_files`、`same_cycle_reused_files`）
与 `restic` 实际块增量（`restic_data_blobs`、`restic_data_added_packed`）。定时维护在生成裁剪计划前
检查复用统计字段齐全、非负且总量一致。

### 仓库整理与保留策略

经用户授权进行存储优化时，使用 `archive compact` 直接调用 `restic prune` 或
`restic forget --prune` 并随后执行 `restic check --read-data`：

```bash
agent-session-vault archive compact --dry-run --json
agent-session-vault archive compact --json
agent-session-vault archive compact --keep-last 8 --json
```

提供保留参数（`--keep-last`、`--keep-daily`、`--keep-weekly`、`--keep-monthly`）时，按主机与
`source:<source_id>` 标签维度分别应用保留策略并 prune、深检，最后写入不可变回执。

### 旧版 `archive-v1` 只读检测

存量 NAS 上使用已退役 `agent-session-vault-archive-v1` 格式的归档根可通过以下命令只读检测，
不会修改或加锁原目录：

```bash
agent-session-vault archive legacy-status --json
```

所有写、初始化、校验、恢复、整理和裁剪命令遇到旧版 `archive-v1` 都会 fail-closed 拒绝修改。
请为 `restic-v2` 配置新的 `[archive].root`，运行 `archive init` 与
`ops archive-cycle --no-due-only --deep`，在覆盖核验完成前只读保留旧版 v1 目录。

只读查看 `restic` 仓库去重与存储统计：

```bash
agent-session-vault archive content-status --json
```

该命令验证已提交快照元数据并查询 `restic snapshots` 与 `restic stats`（`raw-data` 与
`restore-size`），不修改仓库状态。

`archived_sessions` 下的 Codex `.jsonl.zst` 会作为 opaque full-fidelity 成员纳入归档，
不会加入 Tokscale projection。它们满足冷度、深度归档和恢复校验后可以独立裁剪；裁剪前后
Tokscale dry-run 必须保持一致。

构建 snapshot 时，发现的 Codex 文件先在临时私密 capture 中固定 inode 再计算哈希和解析，随后写入
独立 staging payload 副本并立即释放临时硬链接，确保本机源文件的 `st_nlink` 不被长期占用。
capture 优先使用硬链接，跨文件系统时退回打开文件句柄复制，不创建符号链接。若移动恰好发生在固定
引用之前，只查找归档目录中同名的精确文件，并记录 `relocated-during-capture` 警告。

snapshot 构建、发布、深检、state 或 receipt 写入失败均返回结构化终态 JSON（含 `status: "failed"`、
失败阶段 `reason`、`error` 和精确 `staging_root`）及非零退出码。已有 cycle staging 不会被覆盖。
`receipt_path` 指向成功写入的 cycle 回执；进程退出仍以 detached runner 的真实退出码为准。

`verified` cycle 在写入 verified state 和 receipt 前删除自己创建的精确 cycle staging，终态 JSON
返回 `staging_cleanup: "removed"`。清理失败时返回 `partial`、
`reason: "staging_cleanup_failed"`，剩余 staging 留作诊断。未完成全部来源验证的 cycle
不会尝试清理。清理成功后若写入 state 或 receipt 失败，staging 已被删除，应检查已发布的
snapshot。手工 `archive publish` 不删除调用方传入的 staging root。

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

一次性导入历史数据时，使用独立配置显式指定来源，再执行同一套 snapshot、publish、
深度验证和 staging 恢复命令。`file_tree` 来源将配套文件作为 `client = "files"`
存入同一归档库，查询或生成恢复计划时使用 `--client files`。定期任务配置仍只指向
实时会话来源。旧容器的内容全部得到覆盖且恢复验证成功后，才能撤下旧容器；导入的
统计投影不能描述为完整对话。

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

会话裁剪与下述冷数据清理是破坏性流程。先确保候选没有活跃写入，再生成并审阅计划，最后应用同一份计划：

```bash
agent-session-vault archive prune-plan \
  --plan-path ~/.config/agent-session-vault/prune-plans/current.json \
  --json
agent-session-vault archive prune-apply \
  --plan ~/.config/agent-session-vault/prune-plans/current.json \
  --json
```

计划只接纳同时被当前 deep-verified archive snapshot 和 verified stable imports mirror 覆盖的冷
`archived_sessions` 文件。普通 JSONL 仍要求当前 projection 文件；opaque `.jsonl.zst` 有意不要求，
因为 Tokscale 不读取它们。共享 hard link 和外部 hard link 会被拒绝，并记录 Tokscale 官方
preview。执行时再次校验 plan digest、来源 metadata、checksum、archive snapshot、stable coverage，
以及删除前后的 Tokscale parity。任一前提变化都必须重新生成计划，不得手工删除来源。

## 双周维护与冷数据下沉

已授权的本机调度使用一个正式入口，不在提示词中重新拼接会话阶段：

```bash
agent-session-vault ops archive-maintenance --json
agent-session-vault ops archive-maintenance --status --json
```

首次调用预检 NAS 后，以本机 `~/.config/agent-session-vault/run-detached.py` 启动维护 runner；
该脚本的维护源码是 [`scripts/run_detached.py`](../../scripts/run_detached.py)。每阶段独占预留启动记录，
分别保留 PID、stdout、stderr、真实 `exit_code`。启动或 supervisor 错误另存，不伪造业务退出码。
`--status` 只读，支持 `--run-dir <本轮目录>`，不会继续、重发或删除。未知终态、未结束 runner
或尚未完成的冷数据阶段阻止新一轮启动；先核对原目录证据。

维护 runner 只执行一次 `archive-cycle --no-due-only --deep`，验证成功后执行一次 prune-plan；
把新计划独占保存到本轮 `prune-plan.json`，校验生成时间、ID 和 digest，再按此副本执行一次
prune-apply。空计划只跳过 apply。每阶段重做 NAS 预检；任何失败或未验证终态都停止。
它不调用 Tokscale submit，不自动刷新 stable 绕过保护，也不替代外部双周调度器。

会话 runner 退出 `0/session_verified` 后，后续阶段均通过同一 detached 启动器各启动一次：

```bash
agent-session-vault ops cold-inventory --run-dir <本轮目录> --json
agent-session-vault ops cold-plan --run-dir <本轮目录> --decisions <本轮目录>/decisions.json --json
agent-session-vault ops cold-pack --run-dir <本轮目录> --json
agent-session-vault ops cold-cleanup --run-dir <本轮目录> --json
```

inventory 只测量完整成果/会话/运行批次；用本轮时间滚动减 30 天，综合整批 mtime、ctime 和
创建时间。近期根目录直接保留，避免因旧依赖包而遍历和裁掉近期工作。范围仅含 visualizations、
generated_images、独立恢复/备份历史、RedCube workspaces、automation 历史批次、旧 standalone
release 与 marketplace `.staging` 子批次；数据库、live sessions、统计输入和当前控制面不进入范围。
所有类别（包括无候选）都有摘要。清单不跟随链接，跨卷、特殊文件和活动锁材料保留。

AI 依据完成记录、当前指针、进程和当前工作判断材料用途，在同一本轮目录写入 decisions；
格式见 [`config/codex-cold-decisions.example.json`](../../config/codex-cold-decisions.example.json)。
每项须解释整批完成和当前引用检查，并引用持久证据。runtime 另须明确 `retired_runtime: true`。
未选择或不能确认的项保留；空 `archive` 数组是正常无候选。代码检查实际范围、当前控制内容、
包括不带扩展名 ID 的指针、进程与打开文件；资产引用搜索有命中或输入错误均保留该组。
当前 CLI、current 指针和最近两个其他 release 保留；存在引用的其他版本也保留。

plan 绑定本轮 inventory/decisions SHA-256，包含绝对源路径、整批边界、冷度、文件数、逻辑字节、
逐文件 SHA-256 和元数据。pack/cleanup 使用本轮原始 runner 输出中的 plan SHA-256，不沿用旧计划。
打包前后及移动前重新检查来源和当前引用。TAR 使用明确文件清单与 `--no-recursion --no-xattrs
--no-mac-metadata --no-acls --no-fflags`，写入本轮独占文件，避免 macOS 压缩 stdout 的末尾填充。
一遍解码核对成员结构、路径、内容 SHA-256 和硬链接逻辑内容，再真实恢复一个样本到本轮独立 staging。

不可覆盖的包写到配置 archive 根的同级 `auxiliary/codex-home-archives/<run-id>/`，清单和回执写到
`operations/space-optimization/<run-id>/`；无候选类别不创建空包。cleanup 再次验证 NAS 包、来源、
证据和引用，移到同卷独占 `.Trash/codex-cold-<run-id>/`，核对全部内容后才清空精确集合。
文件删除使用不跟随链接的目录描述符，目录只用 `rmdir`，不递归清空任意目录。变更、权限错误或未知
状态立即停止，保留源、包、Trash 和逐项进度，不重试删除，也不清空其他用户 Trash。

`space-before.json` 和 cleanup receipt 分别记录目录分配量、卷可用字节与只读数据库占用；
逻辑清空量不等于 SSD 物理回收量。APFS 快照和同期写入影响卷净值，本流程不维护数据库或清理系统快照。
恢复时将相应 TAR 解包到独立目录，按 `cold-plan.json` 的相对路径、字节和哈希核对，再决定按需取回。

## 只读归档审阅

Agent 结果与 OPL 共享视图读取同一 owner 数据：

```bash
agent-session-vault archive review --json
agent-session-vault archive review --plan /path/to/prune-plan.json --expected-revision <plan_digest> --json
agent-session-vault archive review --plan /path/to/prune-plan.json --receipt /path/to/receipt.json --json
```

`review` 读取当前 stable 覆盖和已存在的 restore/prune 计划，校验计划摘要，
并拒绝与指定 revision 不符的计划。Prune 审阅核对当前本地源文件身份、stable
imports 覆盖和归档对象校验和，展示精确 snapshot ID。归档成员的深度验证与
Tokscale 一致性仍由 `prune-apply` 执行；审阅结果不等于执行授权。

共享 `vault.archive.review` 视图通过 `agent.session.vault.v1#review` 接收可选的绝对
路径 `config_path`、`plan_path`、`receipt_path` 及 `expected_revision`。Adapter 调用
同一 Python owner，不提供动作，也不创建计划、收据或运行状态。Descriptor 为
`development_only` 源码接入，不表示已经发行或安装载体；其源码树须保留与
adapter 同级的 `src/` 包。

选中 prune 计划后，视图自动读取同一计划摘要的 owner 收据。`partial`、`failed`
或 `unknown` 收据与当前源文件检查分别展示；文件缺失本身不能证明由该次操作删除。
先对照收据、剩余源文件和已记录的归档 snapshot，再通过已有 CLI 创建新计划。
刷新或重开不会重试操作。Restore 计划仍绑定原 staging 目标与碰撞处理策略。

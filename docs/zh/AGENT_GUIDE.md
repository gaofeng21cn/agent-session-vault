# Agent 使用指南

## Primary Skill

`agent-session-vault` 是调用本仓库的 Primary Skill。它把自然语言任务路由到唯一的
`agent-session-vault` CLI，不拥有第二套状态、调度器或归档实现。Skill 的权威源码位于
[`skills/agent-session-vault/SKILL.md`](../../skills/agent-session-vault/SKILL.md)。

在已安装该 Skill 的 Codex 环境中，用户或 Agent 应直接使用 `$agent-session-vault`。它会按任务
选择只读、projection、Tokscale、完整归档、裁剪或 staging 恢复路径。没有用户明确授权时，它不会
提交 Tokscale、裁剪会话、恢复到 Codex live root 或修改上游客户端。

仓库负责 Skill 源码；本仓推送到权威 GitHub 后，通过 `$skill-installer` 从
`skills/agent-session-vault` 路径安装到 Codex Skill 根后才可自动发现。不要把安装副本当成源码
或在其中手改规则；本机已安装副本必须与仓库源码保持一致。

## Agent 应该默认怎么理解这个仓库

当一个 Agent 被要求使用这个仓库时，默认应该采用以下假设：

- OPL Fleet 是节点、网络和任务投放 SSOT；不要在 Session Vault 重建机器控制面
- Session Vault 是标准 Fleet 数据任务；不要要求节点单独声明 capability 或逐机安装
- `projection-first` 是默认同步主链路
- raw Tokscale 默认只读取本机/远端 projections 与 managed extras，不直接读取 live HOME
- raw sync 仍然可用，但必须显式请求
- Tokscale 是下游，不应该由本仓库去打补丁
- 机器配置里既可能有 home-level roots，也可能有 project-level root globs

## 推荐提示词

```text
安装并使用这个仓库。通过 OPL Fleet 纳管全部机器，把 Session Vault 作为 Fleet 投放的 projection/Tokscale 聚合任务；不要重复维护节点地址或 SSH route，并在不修改 Tokscale 上游的前提下准备 Tokscale raw 或 canonical 视图。
```

## 常见 Agent 任务

### 纳管机器

先在 OPL Fleet 完成 join/reconcile。Session Vault 不需要再写一份节点地址、SSH route 或 HOME 路径。

### 导入一台远端机器

优先使用：

```bash
agent-session-vault sync fleet --json
```

`sync auto <machine>` 只保留给旧配置兼容或单节点诊断。

### 为 Tokscale 准备环境

日常提交应使用本仓确定性 runner。它会解析 npm latest，通过本仓 Tokscale 入口传入明确包版本，并只在包版本变化时重新检查 help 与官方 preview。

```bash
agent-session-vault ops daily-tokscale --mirror-stable --json
```

runner 会先增量刷新 `imports/local-home/.raw`，再同步远端。raw 日常链路默认不重建 canonical tree。`--mirror-stable` 完成默认 Tokscale analytics continuity；不要因为可选的 `full_fidelity_restore_ready=false` 把默认流程报告成失败。

一次性人工检查仍应使用本仓 `tokscale exec` 入口，不要裸跑 Tokscale。raw exec 会自动刷新本机 projection；`tokscale env` 只做 readback。

更贴近提交行为的口径：

```bash
agent-session-vault tokscale exec --mode raw -- submit -c codex,gemini,openclaw --dry-run
```

更严格的内部统计口径：

```bash
agent-session-vault tokscale exec --mode canonical --omx-replay-dedupe strict -- submit -c codex,gemini,openclaw --dry-run
```

### 归档冷数据

只有当任务目标明确是“降低本地存储压力”时，再进入 archive 命令；如果只是为了让 Tokscale 看到最新远端用量，不应该默认走 archive。

完整归档先使用：

```bash
agent-session-vault ops archive-cycle --due-only --deep --json
```

它不会删除本机会话。只有用户明确要求空间回收时，才先生成 `archive prune-plan`，确认成功且有
候选后再运行 `archive prune-apply`。任一 NAS、stable analytics、projection 或 Tokscale parity
校验失败时停止，不得改用直接删除。

恢复只允许先 `archive plan-restore --mode staging`，再以 `archive restore --plan ...` 写入 staging
目录；不得手工合并到 `~/.codex`。

## Agent 不应该做什么

- 不要读取 Fleet 私有 route 文件或复制节点 inventory
- 不要把 hostname 风格 machine identity 静默替换成裸 IP
- 不要为了统计方便去 patch Tokscale、OMX、Gemini CLI、OpenClaw 上游
- 不要把日常同步流程和 destructive 删除 live roots 绑在一起
- 不要把完整会话迁移当作默认要求；只有用户明确需要正文恢复、搜索或继续会话时才使用 `--include-live-sessions`

## 比较稳妥的执行顺序

1. 先检查 Fleet 节点状态与本机 paths
2. 用 `sync fleet` 并发同步所有受管节点
3. 构造 `raw` 或 `canonical` Tokscale 视图
4. 只有在操作者真的需要冷热分层时，才继续进入 archive 或显式 raw sync

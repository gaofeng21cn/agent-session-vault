<p align="center">
  <a href="./README.md">English</a> | <strong>中文</strong>
</p>

<h1 align="center">Agent Session Vault</h1>

<p align="center"><strong>运行在 OPL Fleet 之上的 local-first session projection 与 Tokscale 聚合任务</strong></p>
<p align="center">Fleet 全节点投放 · Projection Delta-First · Tokscale 视图构造 · 可归档存储分层</p>

<table>
  <tr>
    <td width="33%" valign="top">
      <strong>主要用途</strong><br/>
      管理分散在多台机器、多个客户端、多个项目目录里的 session 历史，而不把云盘抬升为真相源
    </td>
    <td width="33%" valign="top">
      <strong>操作入口</strong><br/>
      Python CLI，覆盖配置检查、同步编排、Tokscale 投影、存储摘要与归档流程
    </td>
    <td width="33%" valign="top">
      <strong>当前旗舰场景</strong><br/>
      让 Tokscale 在 <code>Codex</code>、<code>Gemini CLI</code>、<code>OpenClaw</code>、多机与多根目录环境下依然可用
    </td>
  </tr>
</table>

> 默认产品合同是维护一份可供 Tokscale 持续重算和提交的精简 analytics projection。完整会话正文迁移保留为显式可选能力，不属于默认日常链路。

## 项目定位

现在的 agent session 很少会老老实实待在一个整洁目录里。更常见的情况是同时分散在：

- `~/.codex`、`~/.gemini`、`~/.openclaw` 这样的 home-level roots
- `~/workspace/<project>/.codex` 这样的 project-scoped roots
- 多台机器，包括 macOS、Linux、WSL2
- 长线任务产生的大体积、重放型 session 历史

`agent-session-vault` 的职责，就是在不改上游客户端、不把 OneDrive/NAS/iCloud 当成权威真相的前提下，把这些历史变成可管理、可迁移、可审计的运行面。

## 它解决什么问题

- 按机器、客户端、项目目录发现 session roots
- 默认走 `projection delta-first` 的跨机同步路径
- 为 Tokscale 构造 `raw` 与 `canonical` 两种视图
- 为更冷、更重的存储层保留显式 raw sync 与 archive 流程
- 把目录型 relay 当作传输面，而不是把云服务 SDK 写进核心逻辑

## 为什么 Tokscale 需要它

Tokscale 是一个很好的 exporter，但它不是下面这些问题的控制面：

- 跨机 session 发现
- 项目级 root 发现
- relay bundle 与 projection delta 管理
- 像 OMX 风格 replay dedupe 这类显式规范化口径
- 旧 raw tree 的归档规划

`agent-session-vault` 负责准备 Tokscale 应该读取的 session 视图，而不是改 Tokscale 上游。

## 快速开始

先克隆仓库并安装 CLI：

```bash
git clone <your-repo-url> agent-session-vault
cd agent-session-vault
python3 -m pip install -e ".[dev]"
```

准备本地配置：

```bash
mkdir -p ~/.config/agent-session-vault
cp config/agent-session-vault.example.toml ~/.config/agent-session-vault/config.toml
```

真实配置刻意放在仓库外。实际 machine 名、SSH target、用户名、绝对路径和运行输出只保留在该本地配置及配置指定的状态目录中；不要把 session 数据、bundle、receipt 或日志复制进仓库。

OPL Fleet 已纳管的机器无需在这里重复维护地址、SSH route 或 HOME 路径。执行常见主链路：

```bash
agent-session-vault config --json
agent-session-vault sync fleet --json
agent-session-vault tokscale exec --mode raw -- submit -c codex,gemini,openclaw --dry-run
```

如果你要更严格的内部统计口径：

```bash
agent-session-vault tokscale exec --mode canonical --omx-replay-dedupe strict -- submit -c codex,gemini,openclaw --dry-run
```

## 常用工作流

查看加载后的配置：

```bash
agent-session-vault config --json
```

查看存储摘要：

```bash
agent-session-vault storage summary --json
agent-session-vault storage migration-plan --json
```

运行默认的 Fleet 全节点 projection 同步：

```bash
agent-session-vault sync fleet --json
```

OPL Fleet 负责节点清单、标准 Python/SSH 能力、fresh admission、任务投放和产物通道；本仓只负责 session projection、增量 state、导入与 Tokscale 语义。所有 approved Fleet 节点都会自动成为候选，不需要声明单独的 Session Vault capability；不满足任务条件的节点会带明确原因跳过。

单独刷新本机 HOME 的精简 analytics projection：

```bash
agent-session-vault sync local-home-projection --json
```

把本机临时 Codex runtime home 增量同步到只增不减的 Tokscale extras 树：

```bash
agent-session-vault sync local-codex \
  --source /path/to/quest-or-runtime-root \
  --json
```

执行确定性的每日 projection 同步与 Tokscale 提交：

```bash
agent-session-vault ops daily-tokscale --mirror-stable --json
```

该命令先增量投影控制节点的本机 `HOME`，再由 OPL Fleet 向所有通过 fresh data-job admission 的 approved 节点投放同一 projection 任务。Fleet 把远端 projection 产物带回控制节点，各节点不会分别执行 Tokscale submit。控制节点构造唯一 projection-only raw view、持有单一进程锁，并且只执行一次聚合提交。Tokscale 按 session identity 处理 Codex active/archive 重复副本；Session Vault 保持机器根目录隔离，并验证在冻结输入、Tokscale 版本、pricing 与 dedupe policy 相同的前提下，换到任一准入执行节点仍得到相同数字。`--mirror-stable` 会在 submit confirmed 后把 analytics 层写成可增量复用的 zstd 分片包，避免 OneDrive 承载数万个零碎文件。

默认换机只需恢复 analytics stable 层即可延续 Tokscale。若还需要完整聊天正文、搜索和继续会话，可显式 dry-run 可选的 full-fidelity migration：

```bash
agent-session-vault storage mirror-stable --include-live-sessions --dry-run --json
```

验证 packed stable 层的恢复：

```bash
agent-session-vault storage restore-stable --dest-root /path/to/restore-staging --json
```

只准备 Tokscale 运行环境：

```bash
agent-session-vault tokscale env --mode raw --json
agent-session-vault tokscale env --mode canonical --omx-replay-dedupe strict --json
```

归档一个冷数据树：

```bash
agent-session-vault archive offload-tree \
  --source ~/.config/tokscale/imports/machine-a/.raw/codex \
  --bundle-name machine-a-codex-raw \
  --json
```

## 当前边界

- OPL Fleet 是唯一多机节点、网络、准入与任务投放基座；Session Vault 不维护第二套机器控制面。
- Session Vault 是 Fleet 的标准数据任务，不是节点需单独声明或安装的 capability。
- approved 节点执行 projection；唯一控制节点持有聚合 raw view、运行锁与 exactly-once Tokscale submit。
- 跨节点数值等价要求冻结的 projection 输入、Tokscale 包、custom pricing 和 dedupe policy 一致，不要求把完整聚合数据复制到每台节点。
- 默认跨机链路是 `projection delta-first`，完整 raw sync 仍保持显式模式。
- 默认 raw Tokscale 视图是 projection-only；本机 live HOME 不直接参与扫描。
- 默认 stable mirror 只承诺 Tokscale analytics continuity；完整会话迁移是显式可选能力。
- 云同步工具只被视为目录型 relay，而不是一等后端。
- 当前重点兼容对象是 `Codex`、`Gemini CLI`、`OpenClaw`。
- 这个仓库管理的是 session 视图与传输面，不处理 provider 计费真相。
- live client roots 不会被 destructive 改写。

## 面向 Agent

建议直接使用本仓库 CLI，而不是重写同步、投影或归档逻辑。

典型 Agent 任务：

- 通过 OPL Fleet 纳管机器，然后执行 `sync fleet`
- 仅在兼容或诊断旧路径时配置 machine/root rules 并执行 `sync auto <machine>`
- 当本机 Codex session 位于易清理的 runtime home 下时，先执行 `sync local-codex --source <root>`
- 构造 `raw` 或 `canonical` Tokscale 视图
- 在本地空间需要收缩时，把旧 raw tree 打包归档

## 文档

- [Docs index (English)](docs/en/README.md)
- [Configuration guide (English)](docs/en/CONFIGURATION.md)
- [Architecture guide (English)](docs/en/ARCHITECTURE.md)
- [Agent guide (English)](docs/en/AGENT_GUIDE.md)
- [文档索引（中文）](docs/zh/README.md)
- [配置说明（中文）](docs/zh/CONFIGURATION.md)
- [架构说明（中文）](docs/zh/ARCHITECTURE.md)
- [Agent 使用指南（中文）](docs/zh/AGENT_GUIDE.md)

内部规划文档仍然保持 repo-local、中文优先，只有在显式升格后才进入公开双语面。

## 技术验证

```bash
python3 -m pytest
```

## 许可证

本项目采用 [Apache License 2.0](LICENSE)。

<p align="center">
  <a href="./README.md">English</a> | <strong>中文</strong>
</p>

<h1 align="center">Agent Session Vault</h1>

<p align="center"><strong>面向多机 AI Agent 工作流的 local-first session 控制层</strong></p>
<p align="center">Projection Delta-First 同步 · Tokscale 视图构造 · 可归档存储分层</p>

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

> 对外，`agent-session-vault` 是一个面向多机 AI 工作流的 local-first session 管理层。对内，它是一套目录原生的发现、投影、relay 同步、规范化与归档控制面。

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

编辑 `~/.config/agent-session-vault/config.toml` 中的 machine 定义后，执行常见主链路：

```bash
agent-session-vault config --json
agent-session-vault sync auto imac --json
agent-session-vault tokscale exec --mode raw -- submit --codex --gemini --openclaw --dry-run
```

如果你要更严格的内部统计口径：

```bash
agent-session-vault tokscale exec --mode canonical --omx-replay-dedupe strict -- submit --codex --gemini --openclaw --dry-run
```

## 常用工作流

查看加载后的配置：

```bash
agent-session-vault config --json
```

查看存储摘要：

```bash
agent-session-vault storage summary --json
```

运行默认的 projection-first 同步：

```bash
agent-session-vault sync auto imac --json
```

把本机临时 Codex runtime home 增量同步到只增不减的 Tokscale extras 树：

```bash
agent-session-vault sync local-codex \
  --source /path/to/quest-or-runtime-root \
  --json
```

自动化可以扫描配置里的 `workspace_root`，同步所有发现到的 runtime roots：

```bash
python3 scripts/sync_local_codex_tokscale_sources.py --json
```

只准备 Tokscale 运行环境：

```bash
agent-session-vault tokscale env --mode raw --json
agent-session-vault tokscale env --mode canonical --omx-replay-dedupe strict --json
```

归档一个冷数据树：

```bash
agent-session-vault archive offload-tree \
  --source ~/.config/tokscale/imports/imac/.raw/codex \
  --bundle-name imac-codex-raw \
  --json
```

## 当前边界

- 默认跨机链路是 `projection delta-first`，完整 raw sync 仍保持显式模式。
- 云同步工具只被视为目录型 relay，而不是一等后端。
- 当前重点兼容对象是 `Codex`、`Gemini CLI`、`OpenClaw`。
- 这个仓库管理的是 session 视图与传输面，不处理 provider 计费真相。
- live client roots 不会被 destructive 改写。

## 面向 Agent

建议直接使用本仓库 CLI，而不是重写同步、投影或归档逻辑。

典型 Agent 任务：

- 配置机器定义与 root rules
- 执行 `sync auto <machine>`
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

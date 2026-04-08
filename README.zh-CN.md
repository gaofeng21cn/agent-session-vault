# Agent Session Vault

面向多机 AI 工作流的 local-first session 管理层。

它当前最成熟、最重要的旗舰场景，是把 Tokscale 从“单机、单默认目录、单客户端布局”的假设里解放出来。

[English](README.md) | [中文](README.zh-CN.md)

## 项目动机

现在的 agent session 很少会老老实实只待在一个目录里。

它们通常分散在：

- `~/.codex`、`~/.gemini`、`~/.openclaw` 这样的 home-level roots
- `~/workspace/<project>/.codex` 这样的 project-scoped roots
- 多台机器，包括 macOS、Linux、WSL2
- 长线 agent 任务产生的大体积、重放型 session 历史

Tokscale 作为 exporter 很好用，但它不是 session discovery、跨机同步、projection、分层归档、去重统计的控制面。

`agent-session-vault` 要解决的，就是这层缺口。

它把 raw history、轻量 projection bundle、更严格的 canonical 视图，以及 archive bundle 收束到同一个 CLI 里，同时保持目录原生、local-first，不把 OneDrive 或 NAS 变成 source of truth。

## 核心能力

- 按机器、客户端、项目目录发现 session roots
- 默认走 `projection-first` 跨机同步，不要求先完整镜像所有 raw session
- 为 Tokscale 构造 `raw` 与 `canonical` 两种视图
- 保留显式 raw sync 与 archive 流程，用于仍然需要完整 materialization 的场景
- 把 OneDrive、NAS、iCloud 等视为目录型 relay，而不是把云 SDK 写进核心逻辑
- 当前重点兼容 `Codex`、`Gemini CLI`、`OpenClaw`

## 旗舰场景：Tokscale

当前最成熟的落地场景，是解决 Tokscale 在多机、多目录、多客户端情况下的统计入口问题。

典型情况包括：

- 本机有 `~/.codex`，远端机器也在持续积累 session
- 一部分 Codex history 不在 `~/.codex`，而是在项目目录里的 `.codex`
- `Gemini CLI` 与 `OpenClaw` 也需要进入同一套统计工作流
- 你需要两种口径：
  - `raw`：尽量贴近 upstream 原始布局与提交行为
  - `canonical`：更严格的内部分析口径，可显式开启 OMX 风格 replay dedupe

`agent-session-vault` 不改 Tokscale 上游，只负责准备 Tokscale 应该读取的 session 视图。

## Quick Start

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

编辑 `~/.config/agent-session-vault/config.toml` 里的 machine 定义，让它符合你的 hostname、SSH target、source home 和 root rules。

常见主链路：

```bash
agent-session-vault config --json
agent-session-vault sync auto imac --json
agent-session-vault tokscale exec --mode raw -- submit --codex --gemini --openclaw --dry-run
```

如果你要更严格的内部统计口径：

```bash
agent-session-vault tokscale exec --mode canonical --omx-replay-dedupe strict -- submit --codex --gemini --openclaw --dry-run
```

## 面向人类读者

适合你的场景：

- 你有多台机器，希望用一套稳定的 session 管理层替代零散脚本
- Tokscale 对你很重要，但你的 session 布局已经超出默认假设
- 你需要 session projection、备份、冷热分层、archive plan，而不想让云盘工具接管真相源

不适合你的期待：

- 它不是托管式同步服务
- 它不是 GUI 产品
- 它不是 Tokscale 本体
- 它不是 OMX、OpenClaw 等上游项目的补丁集合

## 面向 Agent

大多数用户不会手工读源码，而是把仓库交给自己的 Agent。

建议让 Agent 直接使用本仓库 CLI，而不是重写一遍同步和投影逻辑。

推荐提示词：

```text
安装并使用这个仓库。为当前机器集合配置 agent-session-vault，使用基于 hostname 的 machine 名称，把 projection-first 作为默认同步链路，同时发现 home-level 与 project-level session roots，并在不修改 Tokscale 上游的前提下准备 Tokscale raw 或 canonical 视图。
```

常见 Agent 任务：

- 配置 machine 定义与 root rules
- 执行 `sync auto <machine>` 做 projection-first 导入
- 构造 `raw` 或 `canonical` Tokscale 环境
- 当本地空间需要收缩时，把旧 raw tree 归档成 bundle

建议从这里开始：

- [文档索引](docs/zh/README.md)
- [配置说明](docs/zh/CONFIGURATION.md)
- [架构说明](docs/zh/ARCHITECTURE.md)
- [Agent 使用指南](docs/zh/AGENT_GUIDE.md)

## 当前边界

- 默认跨机链路是 `projection-first`，raw sync 仍然保留为显式模式
- 云同步工具只被当作目录型 relay，而不是一等后端
- 当前旗舰兼容对象是 `Codex`、`Gemini CLI`、`OpenClaw`
- provider 定价映射与真实账单校准不在本仓库解决范围内
- 本工具不会 destructive 改写 live client roots

## 文档

- [Docs Index (English)](docs/en/README.md)
- [Configuration Guide (English)](docs/en/CONFIGURATION.md)
- [Architecture Guide (English)](docs/en/ARCHITECTURE.md)
- [Agent Guide (English)](docs/en/AGENT_GUIDE.md)
- [文档索引（中文）](docs/zh/README.md)
- [配置说明（中文）](docs/zh/CONFIGURATION.md)
- [架构说明（中文）](docs/zh/ARCHITECTURE.md)
- [Agent 使用指南（中文）](docs/zh/AGENT_GUIDE.md)

<details>
<summary>高级说明：存储分层模型</summary>

这个仓库当前区分四层：

1. `live roots`
   - 上游客户端自己维护的目录，例如 `~/.codex` 或项目级 `.codex`
2. `imported raw projections`
   - `import_root/<machine>/.raw/*` 下的机器导入层
3. `canonical views`
   - 更严格的 Tokscale / 内部分析视图
4. `archive bundles`
   - 用于冷层存储的 `tar.zst` bundle

目标不是把所有 session 永久镜像成一份巨大的副本，而是让 session 状态可审计、可迁移、可分层管理。
</details>

<details>
<summary>高级说明：常用命令地图</summary>

查看配置：

```bash
agent-session-vault config --json
```

查看存储占用：

```bash
agent-session-vault storage summary --json
```

默认 projection-first 同步：

```bash
agent-session-vault sync auto imac --json
```

显式 raw sync helper：

```bash
agent-session-vault sync direct imac --dry-run
```

只构造 Tokscale 环境：

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
</details>

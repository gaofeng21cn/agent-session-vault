# Agent 使用指南

## Agent 应该默认怎么理解这个仓库

当一个 Agent 被要求使用这个仓库时，默认应该采用以下假设：

- machine name 应该是稳定 hostname，而不是临时 IP
- `projection-first` 是默认同步主链路
- raw sync 仍然可用，但必须显式请求
- Tokscale 是下游，不应该由本仓库去打补丁
- 机器配置里既可能有 home-level roots，也可能有 project-level root globs

## 推荐提示词

```text
安装并使用这个仓库。为我的机器集合配置 agent-session-vault，使用基于 hostname 的 machine 名称，同时发现 home-level 与 project-level agent session roots，把 projection-first 作为默认同步链路，并在不修改 Tokscale 上游的前提下准备 Tokscale raw 或 canonical 视图。
```

## 常见 Agent 任务

### 配置一台机器

典型结果应该包括：

- 在 `~/.config/agent-session-vault/config.toml` 中写好 machine stanza
- 为 `~/.codex`、`~/.gemini`、`~/.openclaw` 等固定 roots 配规则
- 为项目级 `.codex` 配 root globs

### 导入一台远端机器

优先使用：

```bash
agent-session-vault sync auto <machine> --json
```

除非操作者明确要求 raw direct 或 raw relay，否则这应该保持为默认建议。

### 为 Tokscale 准备环境

更贴近提交行为的口径：

```bash
agent-session-vault tokscale exec --mode raw -- submit --codex --gemini --openclaw --dry-run
```

更严格的内部统计口径：

```bash
agent-session-vault tokscale exec --mode canonical --omx-replay-dedupe strict -- submit --codex --gemini --openclaw --dry-run
```

### 归档冷数据

只有当任务目标明确是“降低本地存储压力”时，再进入 archive 命令；如果只是为了让 Tokscale 看到最新远端用量，不应该默认走 archive。

## Agent 不应该做什么

- 不要发明配置里没有定义的 root discovery heuristic
- 不要把 hostname 风格 machine identity 静默替换成裸 IP
- 不要为了统计方便去 patch Tokscale、OMX、Gemini CLI、OpenClaw 上游
- 不要把日常同步流程和 destructive 删除 live roots 绑在一起

## 比较稳妥的执行顺序

1. 先检查配置
2. 用 `projection-first` 同步机器
3. 构造 `raw` 或 `canonical` Tokscale 视图
4. 只有在操作者真的需要冷热分层时，才继续进入 archive 或显式 raw sync

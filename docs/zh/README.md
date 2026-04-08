# 文档索引

这个仓库的文档分成两层：

- 根 [README](../../README.zh-CN.md) 负责公开介绍与快速上手
- `docs/zh` 负责更稳定的配置、架构与 Agent 使用说明

建议阅读顺序：

1. [根 README](../../README.zh-CN.md)
2. [配置说明](CONFIGURATION.md)
3. [架构说明](ARCHITECTURE.md)
4. [Agent 使用指南](AGENT_GUIDE.md)

如果你的直接目标是 Tokscale：

1. 先看 [根 README](../../README.zh-CN.md)
2. 从 [`config/agent-session-vault.example.toml`](../../config/agent-session-vault.example.toml) 复制样例配置
3. 配好机器与 roots
4. 执行 `agent-session-vault sync auto <machine>`
5. 再执行 `agent-session-vault tokscale exec ...`

如果你的直接目标是冷热分层与归档：

1. 先看 [架构说明](ARCHITECTURE.md)
2. 查看根 README 里的 `archive` 命令
3. 在配置里增加 retention rules

公开文档：

- [配置说明](CONFIGURATION.md)
- [架构说明](ARCHITECTURE.md)
- [Agent 使用指南](AGENT_GUIDE.md)

English documentation:

- [Docs Index](../en/README.md)

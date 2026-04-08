# 路线图

这个文档只记录当前看得见、且值得继续推进的完善方向，不等于都要马上做。

## 1. 统计准确性继续收口

### Provider 定价校准层

当前 session 视图与 Tokscale 读取链路已经基本通了，但 provider 真实账单价格映射并不一定和 Tokscale 默认价格表一致。

后续可以考虑：

- 增加显式的 provider pricing overlay
- 支持“Tokscale tokens 口径”和“本地价格校准口径”并行输出

### Raw / Canonical 差异报告

现在已经有两种口径，但还缺少一个稳定的差异说明层。

可继续完善为：

- 汇总哪些 tokens 来自 raw-only replay
- 汇总哪些 roots 在 canonical 中被压缩或去重

## 2. Projection 覆盖面继续验证

### 更多真实目录样本验证

当前 `Codex`、`Gemini CLI`、`OpenClaw` 的 projection 已可用，但还可以继续用更多真实目录树做回归验证，特别是：

- 更多项目级 `.codex`
- 更多 `Gemini CLI` 状态树变体
- 更多 `OpenClaw` 文件命名变体

### Projection 审计输出

可以增加更清晰的 projection 审计摘要，例如：

- 每个 root 的 source bytes / projected bytes
- 每个客户端的压缩比例
- 哪些文件被规范化

## 3. Sync / Transport 体验继续优化

### SSH 直传路径去 OneDrive 依赖

当前如果配置了 relay 目录，某些路径下本地中转仍可能落到云盘同步目录，影响 `ssh` 直传体验。

后续可以继续收口为：

- `ssh` 模式完全使用本地临时 staging
- `relay` 模式才显式进入 OneDrive / NAS 目录

### 更强的增量策略可观测性

后续可补：

- 本轮选择 `ssh` 还是 `relay` 的解释输出
- bundle 大小阈值命中说明
- pending bundle 导入状态摘要

## 4. Archive 生命周期继续完善

### Materialize-on-demand

长期看，真正需要的是：

- 热层尽量小
- 冷层保留 bundle
- 当 Tokscale 或审计需要全量视图时，再按需 materialize

当前已有基础命令，但还可以继续做得更顺手。

### Bundle inventory 与恢复体验

后续可继续补：

- 更完整的 bundle inventory
- 按机器 / 客户端 / 时间范围恢复
- 恢复前的空间预估

## 5. 发布面继续完善

### 许可证与发布流程

在正式公开发布前，最好进一步收口：

- 明确 LICENSE
- 增加 CI
- 决定是否提供 tag / release 资产

### 面向第三方的安装体验

如果后续打算让更多人直接使用，而不是主要交给 Agent，可以继续考虑：

- 打包到 PyPI
- 更完整的发布说明
- 更稳定的版本化 changelog

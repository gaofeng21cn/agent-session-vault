# 架构说明

## 设计目标

`agent-session-vault` 不是要变成一个云同步平台。

它当前更具体、更务实的职责是：

- 发现 session roots
- 在机器之间搬运或投影这些 roots
- 为 Tokscale 等下游工具准备可读取的视图
- 让冷数据可以归档，而不是让热层无限膨胀

## 当前四层模型

最容易理解当前实现的方法，是把它看成四层。

### 1. Live Roots

这些是上游客户端自己维护的目录，例如：

- `~/.codex`
- `~/.gemini`
- `~/.openclaw`
- 项目级 `.codex`

本仓库不把自己当成这些目录的所有者。

### 2. Imported Raw Projection Layer

导入机器的数据会落到：

```text
import_root/<machine>/.raw/<client>
```

在默认链路下，这一层通常来自 projection bundle，而不是逐字节 raw mirror。

对于 Tokscale 工作流，这一层按 append-only 方式累积本地已导入历史。

远端后续删除 session 文件或整个 root 时，projection manifest / inventory 仍会反映这个变化，但本地 `.raw` 不会因此回收历史数据。

### 3. Canonical Layer

canonical 视图是更严格的本地 session 布局，供 Tokscale 或内部分析使用。

典型位置包括：

- `shadow_home`
- `import_root/<machine>/<client>`
- `local_workspace_extras/*/codex`

当前实现中，选择 canonical 模式时必须显式使用 `--omx-replay-dedupe strict`。

### 4. Archive Layer

冷数据可以被打成 `tar.zst` bundle，并下沉到 `archive_root`。

后端设计上是目录原生的，所以 bundle 可以放在：

- 本地磁盘目录
- NAS 挂载目录
- OneDrive 同步目录
- iCloud 同步目录

## Projection-First Sync

`sync auto` 是默认跨机路径。

高层流程是：

1. 按配置发现某台机器的 roots
2. 在远端生成完整 projection bundle
3. 根据 bundle 大小阈值决定走 `ssh` 还是 `relay`
4. 在本地把 bundle 导入到 `import_root/<machine>/.raw/*`
5. 按需要继续 canonicalize

这就是为什么面向 Tokscale 的主路径不再要求先做完整 raw mirror。

这里的重点是“传输增量”与“提交视图”分离：

- bundle 仍然按 snapshot/base snapshot 做真增量
- 本地 `.raw` / canonical 视图保持 Tokscale 所需的累计历史，不把远端清理直接传播成本地丢数

## 各客户端的 Projection 行为

### Codex

Codex projection 只保留后续用量统计和 session 身份需要的关键部分：

- 开头的 `session_meta`
- 第一条 `turn_context`
- `token_count` 事件
- `task_complete`、`turn_aborted` 等 terminal events

这样可以显著压缩大体积 Codex session，同时保留 usage 相关结构。

### Gemini CLI

Gemini projection 当前保留 `chats/` 下的 chat JSON 文件。

目标不是镜像整个 Gemini 状态树，而是保留 Tokscale 相关工作流真正需要的部分。

### OpenClaw

OpenClaw projection 会保留 usage 相关结构，同时裁掉大正文内容。

另外，它还会在必要时把非标准 `.jsonl` 后缀变体规范化成标准 `.jsonl` 路径，因为 Tokscale 只识别 OpenClaw 原始命名面的一部分。

## Raw 与 Canonical 两种 Tokscale 视图

### Raw

raw 模式会让 Tokscale 看到：

- 本机 live home
- 导入机器的 `.raw` 树
- 本机项目级 `.codex` roots

适合追求更接近 upstream 原始布局、同时保留本地累计提交历史的场景。

### Canonical

canonical 模式会让 Tokscale 看到：

- `shadow_home`
- canonical 化后的导入机器树
- canonical 化后的本机项目级 extras

适合需要更严格内部统计口径，并愿意显式启用 replay dedupe 的场景。

## 当前不解决什么

- 不宣称 provider 定价映射就是账单真相
- 不改 Tokscale 上游
- 不 destructive 改写上游客户端源目录
- 不把云厂商 SDK 写死到核心逻辑里

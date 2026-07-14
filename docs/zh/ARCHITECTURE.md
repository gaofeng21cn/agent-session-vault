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

本机和导入机器的 analytics projection 都落到：

```text
import_root/local-home/.raw/<client>
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

本机路径由 `sync local-home-projection` 增量刷新。它只读取当前标准 HOME roots，不扫描旧 workspace runtime 或 cold archive；日常 runner 会在远端同步前自动完成这一步。

高层流程是：

1. 按配置发现某台机器的 roots
2. 在 `remote_state_root` 刷新持久化 projection state，只重投影新增或变化文件
3. 按本地 base snapshot 生成 full 或 delta bundle
4. 根据 bundle 大小阈值决定走 `ssh` 还是 `relay`
5. 在本地把 bundle 导入到 `import_root/<machine>/.raw/*`
6. 按需要继续 canonicalize

这就是为什么面向 Tokscale 的主路径不再要求先做完整 raw mirror。

这里的重点是“计算增量”“传输增量”与“提交视图”分离：

- 远端 state 按 source path、size、mtime 和 projector version 复用未变化的 projection blob
- bundle 按 snapshot/base snapshot 做真增量；state 缺失或 schema 不兼容时才重新计算完整 projection
- 本地 `.raw` / canonical 视图保持 Tokscale 所需的累计历史，不把远端清理直接传播成本地丢数

## 各客户端的 Projection 行为

### Codex

Tokscale 的 Codex 统计 parser 带有状态机，因此 Codex projection 会保持所有可解析 JSON 记录的原始顺序，只在每条记录内保留 parser 所需字段，包括：

- 每一条 `session_meta` 的身份、fork/source、provider、agent 和 workspace 字段
- 每一条 `turn_context` 的 model 与 turn identifier
- `task_started`、区分 human/injected 的 `user_message`、`token_count` 与 terminal event 结构
- headless 记录顶层的 model、timestamp 与 usage

聊天正文和无关 payload 字段会被删除；human prompt 收缩为 `user` 哨兵，已知的 injected-context 前缀则保留分类信息。projector 版本会写入本机 state 和 projection manifest，schema 变化时强制重建，不会错误复用不兼容的 delta baseline。

这样既保持 Tokscale 数值不变，也能显著压缩大体积 Codex session；它不是完整会话正文副本。

### Gemini CLI

Gemini projection 当前保留 `chats/` 下的 chat JSON 文件。

目标不是镜像整个 Gemini 状态树，而是保留 Tokscale 相关工作流真正需要的部分。

### OpenClaw

OpenClaw projection 会保留 usage 相关结构，同时裁掉大正文内容。

另外，它还会在必要时把非标准 `.jsonl` 后缀变体规范化成标准 `.jsonl` 路径，因为 Tokscale 只识别 OpenClaw 原始命名面的一部分。

## Raw 与 Canonical 两种 Tokscale 视图

### Raw

raw 模式会让 Tokscale 看到：

- 空的 `projection_home`，用于阻止 Tokscale 直接扫描 live HOME
- `import_root/local-home/.raw/*` 下的本机 projection
- 导入机器的 `.raw` 树
- 已受管的本机 extras

这是默认提交口径。所有用量输入都先进入可镜像的 analytics 层，live client roots 不直接暴露给 Tokscale。

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

## 持久性与换机边界

四个状态不能混为一谈：

- `synced`：远端 projection 已应用到本机 hot imports
- `submitted`：Tokscale 服务端返回 confirmed
- `analytics mirrored`：imports、managed extras 与控制面配置已通过 stable source coverage readback
- `full-fidelity migration ready`：显式可选的 analytics profile 加本机 client-owned live sessions 都已通过 migration profile readback

默认产品只承诺 Tokscale analytics continuity：本机 projection、远端 projections、managed extras 和控制面配置通过 stable readback 后即可换机恢复并继续重算/提交。`full_fidelity_restore_ready=false` 不阻塞这个默认目标。

本机 `~/.codex/sessions`、`~/.codex/archived_sessions` 以及其他客户端 live roots 仅在需要完整正文、搜索或继续会话时才进入可选 migration profile。此时必须停止客户端写入，再显式执行 `storage mirror-stable --include-live-sessions`。

凭据不进入 stable mirror。新机器需要重新认证，然后从稳定副本恢复 session 数据并重建 canonical/cache。

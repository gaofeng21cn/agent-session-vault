# 配置说明

## 默认配置路径

CLI 默认读取：

```text
~/.config/agent-session-vault/config.toml
```

建议从这里开始：

[`config/agent-session-vault.example.toml`](../../config/agent-session-vault.example.toml)

## Public 仓库边界

示例配置可以提交，填入真实环境信息后的配置不可以。实际 machine 名、SSH target、home 路径、存储路径和 retention 选择只保留在 `~/.config/agent-session-vault/config.toml`。Session tree、projection bundle、archive、Tokscale receipt 和运行日志都属于本地数据，不应进入仓库。

## 配置结构

配置刻意保持为小而显式的几块：

- `[paths]`
- `[sync]`
- `[machines.<name>]`
- `[[retention.rules]]`

## `paths`

这些路径定义了本机派生状态应该落在哪里。

- `home`
  - 本机 live clients 的来源 home；Tokscale 不直接读取它
- `workspace_root`
  - 本机稳定层同步时，用来发现 workspace 内的 ingest sources
- `import_root`
  - 本机与远端 analytics projections 的本地根目录
- `projection_home`
  - raw Tokscale 使用的空 HOME，防止客户端自动发现 live roots
- `shadow_home`
  - canonical Tokscale home
- `local_workspace_extras`
  - 本机 project-level Codex 的 canonical 化结果
- `archive_root`
  - 冷层 bundle 的存放目录
- `relay_root`
  - projection 或 raw bundle 的本地 relay 目录

## Stable Layer

Tokscale 的稳定层由两部分组成：

```text
<import_root>
<local_workspace_extras>
```

`import_root` 保存远端机器导入到本机后的稳定缓存，例如：

```text
<import_root>/local-home/.raw/codex
<import_root>/machine-a/.raw/codex
<import_root>/machine-b/.raw/gemini
```

`local_workspace_extras` 保存本机 workspace 里零散、易清理来源被吸收后的稳定缓存，包括：

```text
<quest-root>/.ds/codex_homes
<quest-root>/.ds/cold_archive/codex_sessions
<workspace-project>/.codex
<home>/.codex/projects/<project>/archive/<timestamp>/codex
```

零散目录只是 ingest sources。默认 stable mirror 是面向 Tokscale 连续性的 `analytics` profile；它适合重建导入视图，但不等于本机客户端完整历史已经可迁移。日常任务可以先更新本机热稳定层，再镜像到 OneDrive/NAS 等冷副本：

```bash
agent-session-vault storage mirror-stable --json
```

默认目标是 `archive_root` 同级的 `stable/` 目录。若 `archive_root` 是：

```text
/path/to/agent-session-vault/archive
```

默认 stable 副本就是：

```text
/path/to/agent-session-vault/stable
```

该命令会同步：

```text
stable/tokscale/imports
stable/tokscale/local-workspace-extras
stable/config/config.toml
```

每次成功镜像都会写 `stable-layer-attempt.json`，只有传输成功且逐文件 source coverage readback 通过时才更新 `stable-layer-manifest.json`。若 source manifest 指纹未变化且目的端 coverage 仍完整，会复用上次 verified 状态并跳过 rsync，但不会跳过本轮 source/destination 回读。镜像采用 source-covered、no-delete 语义，云端多余旧文件不会被当成本轮 source coverage。

默认 analytics stable 层已经足够在新机器继续 Tokscale 重算和提交。只有需要完整聊天正文、搜索或继续会话时，才显式盘点可选 full-fidelity migration：

```bash
agent-session-vault storage migration-plan --json
agent-session-vault storage mirror-stable --include-live-sessions --dry-run --json
```

若选择这项能力，确认体积并停止 Codex、Gemini CLI、OpenClaw 的写入后，再去掉 `--dry-run`。`full_fidelity_restore_ready=false` 只表示这个可选 profile 尚未完成，不是默认 Tokscale continuity 的 blocker。

Tokscale submit 仍从本机热层运行，避免云盘小文件同步延迟影响运行。

## `sync`

`sync` 负责控制链路选择。

- `default_strategy`
  - 旧 raw sync 的默认策略
- `direct_max_delta_files`
  - raw direct sync 的文件数阈值
- `direct_max_delta_bytes`
  - raw direct sync 的字节阈值
- `projection_transport`
  - projection bundle 的默认传输方式，可选 `auto`、`ssh`、`relay`
- `projection_direct_max_bundle_bytes`
  - 当 `projection_transport=auto` 时，用于决定走 `ssh` 还是 `relay` 的 bundle 大小阈值

## 本机临时 Codex Homes

当前标准 HOME 由默认命令增量投影，不需要手工指定来源：

```bash
agent-session-vault sync local-home-projection --json
```

结果落在 `<import_root>/local-home/.raw/<client>`。`tokscale exec --mode raw` 和日常 runner 都会先刷新它，Tokscale 的 `HOME` 则指向空的 `projection_home`。

有些本机 runtime 会创建容易被清理的 Codex homes，例如：

```text
<quest-root>/.ds/codex_homes/run-*/sessions/
```

不要让 Tokscale 直接读取这些临时目录。先把它们增量同步到只增不减的本机 extras 树：

```bash
agent-session-vault sync local-codex --source <quest-root> --json
```

`sync local-codex` 只用于操作者明确指定的新 ingest source。当前 MAS/Codex 日常链路不扫描旧 workspace `.ds/codex_homes`、`.ds/cold_archive/codex_sessions`、旧 runtime 或 cold archive；已吸收到 managed extras 的历史数据继续保留，但不再反向依赖旧目录。

稳定的 Tokscale 根目录是：

```text
<local_workspace_extras>/volatile-codex-homes/codex
```

`raw` Tokscale 视图只自动包含带 `sync-state.json` 的 managed local sync extras，不再直接引用 workspace `.codex` 或 home project archive 原始路径。
`canonical` 视图继续包含所有 `local_workspace_extras/*/codex` 目录。

## `machines.<name>`

每台机器都应该使用稳定的逻辑主机名，而不是临时 IP 地址。

好的例子：

- `machine-a`
- `wsl2-main`
- `lab-linux`

不要把 Tailscale 或局域网 IP 直接当 machine name。

关键字段：

- `import_name`
  - 机器导入到本地后在 `import_root` 下使用的目录名
- `ssh_target`
  - 本机 SSH config 里的 alias
- `source_home`
  - 远端机器上的绝对 home 路径
- `remote_relay_root`
  - 远端用于 relay staging 的目录
- `remote_state_root`
  - 远端 raw relay 状态与 projection 计算缓存目录；projection 缓存位于 `<machine>/projection/`
- `clients`
  - 这台机器启用的客户端家族

## Root Discovery

当前只有两种显式规则：

### `[[machines.<name>.roots]]`

用于固定根目录，例如：

- `~/.codex`
- `~/.gemini`
- `~/.openclaw`

### `[[machines.<name>.root_globs]]`

用于 project-scoped roots，例如：

- `~/workspace/*/.codex`
- `~/projects/*/.codex`
- 任何你实际在使用、且愿意显式声明的目录族

目标是让 root discovery 可审计，而不是让工具替你猜目录布局。

## Retention Rules

retention rules 是显式 archive policy。

它不会自动扫描并删除 live roots。

当你要识别更冷的 imported 或 canonical 数据树，并把它们下沉为 `archive_root` 下的 bundle 时，再使用这一层。

## 一个典型工作流

1. 用 hostname 风格命名机器
2. 为 home-level clients 配固定 roots
3. 为 project-level Codex 配 root globs
4. 执行：

```bash
agent-session-vault config --json
agent-session-vault sync auto machine-a --json
```

5. 如果目标是 Tokscale，再执行：

```bash
agent-session-vault tokscale exec --mode raw -- submit -c codex,gemini,openclaw --dry-run
```

6. 如果需要把本机稳定层写入 OneDrive/NAS 副本，再执行：

```bash
agent-session-vault storage mirror-stable --json
```

这一步只产生 analytics 连续性副本；完整换机另用 `--include-live-sessions`。

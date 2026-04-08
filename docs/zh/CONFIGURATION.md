# 配置说明

## 默认配置路径

CLI 默认读取：

```text
~/.config/agent-session-vault/config.toml
```

建议从这里开始：

[`config/agent-session-vault.example.toml`](../../config/agent-session-vault.example.toml)

## 配置结构

配置刻意保持为小而显式的几块：

- `[paths]`
- `[sync]`
- `[machines.<name>]`
- `[[retention.rules]]`

## `paths`

这些路径定义了本机派生状态应该落在哪里。

- `home`
  - 构造 Tokscale 环境时使用的本机 home
- `workspace_root`
  - 构造 raw 视图时，用来发现本机 project-level `.codex`
- `import_root`
  - 远端机器导入数据的本地根目录
- `shadow_home`
  - canonical Tokscale home
- `local_workspace_extras`
  - 本机 project-level Codex 的 canonical 化结果
- `archive_root`
  - 冷层 bundle 的存放目录
- `relay_root`
  - projection 或 raw bundle 的本地 relay 目录

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

## `machines.<name>`

每台机器都应该使用稳定的逻辑主机名，而不是临时 IP 地址。

好的例子：

- `imac`
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
  - 远端 raw relay 状态目录
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
agent-session-vault sync auto imac --json
```

5. 如果目标是 Tokscale，再执行：

```bash
agent-session-vault tokscale exec --mode raw -- submit --codex --gemini --openclaw --dry-run
```

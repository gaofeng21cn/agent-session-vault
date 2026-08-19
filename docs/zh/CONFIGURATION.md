# 配置参考

本文档只负责配置 schema、默认值、约束和路径作用。操作步骤见[操作手册](OPERATIONS.md)。

默认配置文件是：

```text
~/.config/agent-session-vault/config.toml
```

如需加载其他文件，把 `--config <path>` 放在顶层命令之前。配置文件不存在时会使用默认值。
未知 table、未知字段、错误类型和不完整 source entry 都会被拒绝，避免退役配置静默改变运行行为。
只有下列字段属于当前合同。

## `[paths]`

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `home` | 当前进程 home | 权威本机用户目录，用于发现实时客户端 root 和本机 Tokscale 凭据 |
| `workspace_root` | `<home>/workspace` | 检查其直接子项目中的 project-scoped `.codex` root |
| `import_root` | `<home>/.config/tokscale/imports` | 只增不减的本机与 Fleet 投影导入目录 |
| `projection_home` | `<home>/.config/tokscale/projection-home` | 传给 Tokscale 的隔离 HOME；这里禁止存在实时客户端 root |
| `local_workspace_extras` | `<home>/.config/tokscale/local-workspace-extras` | 显式导入的本机 Codex 数据；只有带 `sync-state.json` 的 namespace 会进入 Tokscale 视图 |
| `stable_root` | `<home>/agent-session-vault/stable` | packed、verified stable analytics 副本及 manifest |

所有字段都是路径，加载时会展开 `~`。相对 archive source path 会在 `home` 下解析；其他路径字段
应使用绝对路径或 home-relative 路径。

## `[archive]`

| 字段 | 默认值 | 约束与含义 |
| --- | --- | --- |
| `root` | `<home>/agent-session-vault/archive` | 保存 immutable object、snapshot、catalog、state 和 receipt 的文件系统或已挂载 NAS root |
| `cadence_days` | `14` | 正整数，控制 `archive-cycle --due-only` |
| `cold_age_days` | `30` | 非负整数，用于筛选可进入 prune plan 的本机冷归档会话 |
| `staging_root` | `<home>/.cache/agent-session-vault/archive-staging` | 本机 snapshot 构建目录 |
| `machine_id_path` | `<home>/.config/agent-session-vault/machine-id` | 本机生成且稳定保存的 archive identity |
| `source_paths` | 自动发现 | 显式 Codex source root，规则见下文 |
| `require_quiescent_for_prune` | `true` | source 在扫描过程中变化时拒绝生成 prune plan |

`source_paths` 缺失或为空时，archive 会发现 `<home>/.codex` 和每个已存在的
`<workspace_root>/<project>/.codex`。每个 source 只收录非 symlink 的普通文件：
`sessions/**/*.jsonl`、`archived_sessions/**/*.jsonl`、对应的 `.gz` 文件和
`session_index.jsonl`。

紧凑字符串形式使用默认 `kind = "codex_home"`：

```toml
[archive]
source_paths = ["~/.codex", "~/workspace/project/.codex"]
```

table 形式可以显式指定 `kind` 和可选的人类可读 label：

```toml
[archive]
source_paths = [
  { path = "~/.codex", kind = "codex_home", label = "home" },
  { path = "~/workspace/project/.codex", kind = "project_root", label = "project" },
]
```

每个 table entry 都必须提供 `path`。空 entry 或无效 entry 不是兼容机制；缺失 `path` 会直接拒绝。

## 生效回读

用 CLI 查看进程实际读取并解析后的配置：

```bash
agent-session-vault config --json
```

回读只包含当前 `[paths]` 和 `[archive]` 字段。Fleet 节点、路由、准入和传输不属于本仓配置。

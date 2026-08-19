# Configuration Reference

This file owns the configuration schema, defaults, constraints, and path
effects. Operational sequences belong in [Operations](OPERATIONS.md).

The default file is:

```text
~/.config/agent-session-vault/config.toml
```

Pass `--config <path>` before the top-level command to load another file. A
missing file is valid and uses defaults. Unknown tables, unknown fields, wrong
types, and incomplete source entries are rejected so retired configuration
cannot silently change runtime behavior. Only the fields below are current.

## `[paths]`

| Field | Default | Meaning |
| --- | --- | --- |
| `home` | current process home | Authoritative local user home used to discover live client roots and local Tokscale credentials |
| `workspace_root` | `<home>/workspace` | Immediate child projects inspected for project-scoped `.codex` roots |
| `import_root` | `<home>/.config/tokscale/imports` | Append-only local and Fleet projection imports |
| `projection_home` | `<home>/.config/tokscale/projection-home` | Isolated HOME passed to Tokscale; live client roots are forbidden here |
| `local_workspace_extras` | `<home>/.config/tokscale/local-workspace-extras` | Explicit local Codex ingests; only namespaces with `sync-state.json` enter the Tokscale view |
| `stable_root` | `<home>/agent-session-vault/stable` | Packed, verified stable analytics copy and manifest |

All values are paths. `~` is expanded when loaded. Relative archive source
paths are resolved below `home`; other path fields should be absolute or
home-relative.

## `[archive]`

| Field | Default | Constraint and meaning |
| --- | --- | --- |
| `root` | `<home>/agent-session-vault/archive` | Filesystem or mounted NAS root for immutable objects, snapshots, catalog, state, and receipts |
| `cadence_days` | `14` | Positive integer controlling `archive-cycle --due-only` |
| `cold_age_days` | `30` | Non-negative integer used to admit local archived sessions for prune plans |
| `staging_root` | `<home>/.cache/agent-session-vault/archive-staging` | Local snapshot construction area |
| `machine_id_path` | `<home>/.config/agent-session-vault/machine-id` | Stable locally generated archive identity |
| `source_paths` | automatic | Explicit Codex source roots; see below |
| `require_quiescent_for_prune` | `true` | Reject prune planning when a source changes during the scan |

When `source_paths` is absent or empty, archive discovery uses `<home>/.codex`
and each existing immediate `<workspace_root>/<project>/.codex`. Each source
contributes only regular, non-symlink `sessions/**/*.jsonl`,
`archived_sessions/**/*.jsonl`, their `.gz` variants, and
`session_index.jsonl`.

The compact string form assigns the default `kind = "codex_home"`:

```toml
[archive]
source_paths = ["~/.codex", "~/workspace/project/.codex"]
```

The table form supports an explicit `kind` and optional human label:

```toml
[archive]
source_paths = [
  { path = "~/.codex", kind = "codex_home", label = "home" },
  { path = "~/workspace/project/.codex", kind = "project_root", label = "project" },
]
```

`path` is required for every table entry. Empty or invalid entries are not a
compatibility mechanism: an entry without `path` is rejected.

## Effective Readback

Use the CLI to see the resolved configuration consumed by the process:

```bash
agent-session-vault config --json
```

This readback includes only current `[paths]` and `[archive]` fields. Fleet
nodes, routes, admission, and transport do not belong in this configuration.

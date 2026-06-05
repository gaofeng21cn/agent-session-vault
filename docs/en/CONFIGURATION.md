# Configuration Guide

## Default Config Path

By default the CLI reads:

```text
~/.config/agent-session-vault/config.toml
```

Start from:

[`config/agent-session-vault.example.toml`](../../config/agent-session-vault.example.toml)

## Config Structure

The config is intentionally small and explicit.

It has four top-level areas:

- `[paths]`
- `[sync]`
- `[machines.<name>]`
- `[[retention.rules]]`

## `paths`

These paths define where local derived state lives.

- `home`
  - local home used for Tokscale environment construction
- `workspace_root`
  - local workspace root used by stable-layer sync to discover ingest sources
- `import_root`
  - imported machine data lives here
- `shadow_home`
  - canonical Tokscale home for stricter views
- `local_workspace_extras`
  - canonicalized local project-level Codex extras live here
- `archive_root`
  - colder bundle storage
- `relay_root`
  - local relay directory for projection or raw bundles

## Stable Layer

The Tokscale stable layer has two parts:

```text
<import_root>
<local_workspace_extras>
```

`import_root` stores stable local caches imported from remote machines, for example:

```text
<import_root>/imac/.raw/codex
<import_root>/m1max-mbp/.raw/gemini
```

`local_workspace_extras` stores stable local caches absorbed from scattered and cleanup-prone workspace sources, including:

```text
<quest-root>/.ds/codex_homes
<quest-root>/.ds/cold_archive/codex_sessions
<workspace-project>/.codex
<home>/.codex/projects/<project>/archive/<timestamp>/codex
```

Scattered directories are ingest sources. The stable layer is the data surface that submit, backup, and migration should depend on. A recurring task can update the local hot stable layer first, then mirror it to a colder OneDrive/NAS copy:

```bash
agent-session-vault storage mirror-stable --json
```

The default destination is `stable/` next to `archive_root`. If `archive_root` is:

```text
/Users/gaofeng/OneDrive/agent-session-vault/archive
```

the default stable copy is:

```text
/Users/gaofeng/OneDrive/agent-session-vault/stable
```

The command mirrors:

```text
stable/tokscale/imports
stable/tokscale/local-workspace-extras
stable/config/config.toml
```

Tokscale submit should still run from the local hot stable layer to avoid cloud-sync latency on many small files. Backup and migration can depend on the stable copy.

## `sync`

`sync` controls transport choice.

- `default_strategy`
  - legacy raw sync strategy selection
- `direct_max_delta_files`
  - raw direct sync threshold by file count
- `direct_max_delta_bytes`
  - raw direct sync threshold by bytes
- `projection_transport`
  - default transport for projection bundles: `auto`, `ssh`, or `relay`
- `projection_direct_max_bundle_bytes`
  - bundle size threshold for deciding `ssh` versus `relay` when projection transport is `auto`

## Local Volatile Codex Homes

Some local runtimes create short-lived Codex homes such as:

```text
<quest-root>/.ds/codex_homes/run-*/sessions/
```

Do not point Tokscale directly at those volatile directories. First sync them into the append-only local extras tree:

```bash
agent-session-vault sync local-codex --source <quest-root> --json
```

For the recurring local maintenance path, use the repo script to scan `workspace_root` plus home project archives and sync all discovered local ingest sources:

```bash
python3 scripts/sync_local_codex_tokscale_sources.py --json
```

The stable Tokscale root is:

```text
<local_workspace_extras>/volatile-codex-homes/codex
```

The `raw` Tokscale view includes managed local sync extras that have `sync-state.json`; it no longer points directly at workspace `.codex` or home project archive source paths.
The `canonical` view includes all `local_workspace_extras/*/codex` directories.

## `machines.<name>`

Each machine entry should use a stable logical hostname, not a changing IP address.

Good examples:

- `imac`
- `wsl2-main`
- `lab-linux`

Avoid naming a machine by a temporary Tailscale or LAN IP.

Important fields:

- `import_name`
  - local directory name under `import_root`
- `ssh_target`
  - SSH alias from your local SSH config
- `source_home`
  - absolute home path on the remote machine
- `remote_relay_root`
  - remote directory used as relay staging
- `remote_state_root`
  - remote state directory for raw relay tracking
- `clients`
  - enabled client families for that machine

## Root Discovery

There are two explicit rule types:

### `[[machines.<name>.roots]]`

Use this for fixed roots, for example:

- `~/.codex`
- `~/.gemini`
- `~/.openclaw`

### `[[machines.<name>.root_globs]]`

Use this for project-scoped roots, for example:

- `~/workspace/*/.codex`
- `~/projects/*/.codex`
- any other explicit directory family you actually use

The point is to make root discovery auditable. The tool does not guess arbitrary project layouts for you.

## Retention Rules

Retention rules are explicit archive policies.

They do not scan and delete live roots automatically.

Use them when you want to identify colder imported or canonicalized trees and move them into bundles under `archive_root`.

## Example Workflow

1. Define a machine with hostname-based naming.
2. Add fixed roots for home-level clients.
3. Add glob rules for project-level Codex roots.
4. Run:

```bash
agent-session-vault config --json
agent-session-vault sync auto imac --json
```

5. If Tokscale is the goal, run:

```bash
agent-session-vault tokscale exec --mode raw -- submit --codex --gemini --openclaw --dry-run
```

6. To mirror the local stable layer into a OneDrive/NAS copy, run:

```bash
agent-session-vault storage mirror-stable --json
```

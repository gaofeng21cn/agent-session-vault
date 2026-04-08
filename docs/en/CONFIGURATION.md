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
  - local workspace root used to discover project-level `.codex` directories for raw view construction
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

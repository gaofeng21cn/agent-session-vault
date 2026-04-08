# Agent Session Vault

Local-first session management for multi-machine AI workflows.

Its flagship use case today is extending Tokscale beyond a single machine, a single default session directory, and a single client layout.

[English](README.md) | [中文](README.zh-CN.md)

## Why This Exists

AI session history is rarely clean anymore.

It lives across:

- home-level roots such as `~/.codex`, `~/.gemini`, and `~/.openclaw`
- project-scoped roots such as `~/workspace/<project>/.codex`
- multiple machines, including macOS, Linux, and WSL2
- long-running agent workflows that can create replay-heavy or storage-heavy histories

Tokscale is excellent as an exporter, but it is not the control plane for session discovery, cross-machine sync, projection, archiving, or deduplication.

`agent-session-vault` fills that gap.

It gives you a directory-native session layer that keeps raw histories, slimmer projection bundles, stricter canonical views, and archival bundles under one CLI, without turning a cloud drive into the source of truth.

## What It Does

- Discovers agent session roots per machine, per client, and per project directory.
- Uses `projection delta-first` sync by default so cross-machine transfer does not need to mirror every raw session file.
- Builds both `raw` and `canonical` Tokscale views.
- Preserves explicit raw sync and archive flows for cases where full materialization is still needed.
- Treats OneDrive, NAS, iCloud, and similar tools as directory relays instead of baking cloud SDKs into the core.
- Supports the clients we actually needed in production: `Codex`, `Gemini CLI`, and `OpenClaw`.

## Flagship Use Case: Tokscale

The most mature use case today is making Tokscale usable when your session history is spread across machines and roots.

Typical examples:

- your main machine keeps local `~/.codex` history, but remote machines also accumulate sessions
- some Codex histories live in project-level `.codex` directories instead of only `~/.codex`
- `Gemini CLI` and `OpenClaw` histories should be counted in the same operational workflow
- you want two accounting views:
  - `raw`: close to upstream layout and submission behavior
  - `canonical`: stricter internal analysis, including OMX-style replay dedupe when you explicitly enable it

`agent-session-vault` does not patch Tokscale upstream. It prepares the session view that Tokscale should read.

## Quick Start

Clone the repository and install the CLI:

```bash
git clone <your-repo-url> agent-session-vault
cd agent-session-vault
python3 -m pip install -e ".[dev]"
```

Create a local config:

```bash
mkdir -p ~/.config/agent-session-vault
cp config/agent-session-vault.example.toml ~/.config/agent-session-vault/config.toml
```

Edit the machine section in `~/.config/agent-session-vault/config.toml` so it matches your hostname, SSH target, source home, and root rules.

Then run the common path:

```bash
agent-session-vault config --json
agent-session-vault sync auto imac --json
agent-session-vault tokscale exec --mode raw -- submit --codex --gemini --openclaw --dry-run
```

If you want the stricter internal view:

```bash
agent-session-vault tokscale exec --mode canonical --omx-replay-dedupe strict -- submit --codex --gemini --openclaw --dry-run
```

`sync auto` now tries to export a strict `projection_delta` by default. It falls back to `projection_full` only when:

- the local machine has no projection state yet
- the root set changed and the stored base snapshot is no longer compatible
- the remote relay no longer has the base snapshot required for a verified diff

## For Humans

Use this project if:

- you work across multiple machines and want one session management layer instead of ad hoc shell snippets
- Tokscale is important to you, but your histories no longer fit its default assumptions
- you need session projection, backup, tiering, or archive planning without giving cloud sync tools control over your source data

Do not expect this repository to be:

- a hosted sync service
- a GUI product
- a replacement for Tokscale itself
- a patch set for OMX, OpenClaw, or other upstream tools

## For Agents

Most people will not read the source tree manually. They will hand this repository to an agent and ask it to operate the workflow.

Use this repository through the CLI, not by re-implementing its logic from scratch.

Recommended prompt for an agent:

```text
Install and use this repository. Configure agent-session-vault for the current machines with hostname-based machine names, keep projection delta-first sync as the default path, discover both home-level and project-level session roots, and prepare Tokscale raw or canonical views without modifying Tokscale upstream.
```

Common agent tasks:

- configure machine definitions and root rules
- run `sync auto <machine>` for projection delta-first imports
- prepare `raw` or `canonical` Tokscale environments
- archive older raw trees into bundles when local storage should be reduced

Start here:

- [Docs Index](docs/en/README.md)
- [Configuration Guide](docs/en/CONFIGURATION.md)
- [Architecture Guide](docs/en/ARCHITECTURE.md)
- [Agent Guide](docs/en/AGENT_GUIDE.md)

## Current Boundaries

- `projection delta-first` is the default cross-machine path; raw sync remains explicit.
- Cloud tools are treated as directory relays, not as first-class backends.
- The current flagship integrations are `Codex`, `Gemini CLI`, and `OpenClaw`.
- Provider price reconciliation is not solved here; this repository manages session views, not billing truth.
- The tool does not destructively rewrite live client roots.
- Pending relay bundles are resolved from the current local base snapshot and prefer the newest directly applicable bundle instead of replaying obsolete sibling deltas.

## Documentation

- [Docs Index (English)](docs/en/README.md)
- [Configuration Guide (English)](docs/en/CONFIGURATION.md)
- [Architecture Guide (English)](docs/en/ARCHITECTURE.md)
- [Agent Guide (English)](docs/en/AGENT_GUIDE.md)
- [文档索引（中文）](docs/zh/README.md)
- [配置说明（中文）](docs/zh/CONFIGURATION.md)
- [架构说明（中文）](docs/zh/ARCHITECTURE.md)
- [Agent 使用指南（中文）](docs/zh/AGENT_GUIDE.md)

<details>
<summary>Advanced: Storage Model</summary>

The repository distinguishes four practical layers:

1. `live roots`
   - upstream client-owned directories such as `~/.codex` or project `.codex`
2. `imported raw projections`
   - machine-scoped imports under `import_root/<machine>/.raw/*`
3. `canonical views`
   - stricter local views for Tokscale or internal analysis
4. `archive bundles`
   - `tar.zst` bundles for colder storage layers

The point is not to force every history into one permanent mirror. The point is to make session state auditable and movable.
</details>

<details>
<summary>Advanced: Command Map</summary>

Inspect config:

```bash
agent-session-vault config --json
```

Inspect storage:

```bash
agent-session-vault storage summary --json
```

Projection delta-first sync:

```bash
agent-session-vault sync auto imac --json
```

Explicit raw sync helper:

```bash
agent-session-vault sync direct imac --dry-run
```

Prepare Tokscale environment only:

```bash
agent-session-vault tokscale env --mode raw --json
agent-session-vault tokscale env --mode canonical --omx-replay-dedupe strict --json
```

Archive a cold tree:

```bash
agent-session-vault archive offload-tree \
  --source ~/.config/tokscale/imports/imac/.raw/codex \
  --bundle-name imac-codex-raw \
  --json
```
</details>

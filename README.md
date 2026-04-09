<p align="center">
  <strong>English</strong> | <a href="./README.zh-CN.md">中文</a>
</p>

<h1 align="center">Agent Session Vault</h1>

<p align="center"><strong>Local-first session control for multi-machine AI agent workflows</strong></p>
<p align="center">Projection Delta-First Sync · Tokscale Views · Archive-Ready Storage</p>

<table>
  <tr>
    <td width="33%" valign="top">
      <strong>Primary Use</strong><br/>
      Manage session history that lives across machines, clients, and project-level roots without promoting a cloud drive into the source of truth
    </td>
    <td width="33%" valign="top">
      <strong>Interface</strong><br/>
      Python CLI for config inspection, sync orchestration, Tokscale projection, storage summaries, and archive workflows
    </td>
    <td width="33%" valign="top">
      <strong>Current Flagship</strong><br/>
      Making Tokscale usable when histories span <code>Codex</code>, <code>Gemini CLI</code>, <code>OpenClaw</code>, multiple machines, and multiple roots
    </td>
  </tr>
</table>

> Publicly, `agent-session-vault` is a local-first session management layer for multi-machine AI workflows. Internally, it is a directory-native control plane for discovery, projection, relay sync, canonicalization, and archival.

## Product Position

Modern agent session history rarely sits in one clean directory. It usually spreads across:

- home-level roots such as `~/.codex`, `~/.gemini`, and `~/.openclaw`
- project-scoped roots such as `~/workspace/<project>/.codex`
- multiple machines, including macOS, Linux, and WSL2
- long-running workflows that create replay-heavy or storage-heavy histories

`agent-session-vault` is the layer that makes those histories manageable without patching upstream clients or treating OneDrive, NAS, or iCloud as the authority.

## What It Helps You Do

- Discover session roots per machine, per client, and per project directory.
- Sync across machines with `projection delta-first` as the default path.
- Build both `raw` and `canonical` views for Tokscale.
- Keep explicit raw sync and archive flows for colder or heavier storage paths.
- Treat directory relays as transport surfaces instead of embedding cloud-provider logic into the tool.

## Why Tokscale Needs This

Tokscale is a strong exporter, but it is not the control plane for:

- cross-machine session discovery
- project-level root discovery
- relay bundles and projection deltas
- canonicalization rules such as explicit OMX-style replay dedupe
- archive planning for old raw trees

`agent-session-vault` prepares the session view that Tokscale should read. It does not modify Tokscale upstream.

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

Edit the machine definitions in `~/.config/agent-session-vault/config.toml`, then run the common path:

```bash
agent-session-vault config --json
agent-session-vault sync auto imac --json
agent-session-vault tokscale exec --mode raw -- submit --codex --gemini --openclaw --dry-run
```

If you want the stricter internal accounting view:

```bash
agent-session-vault tokscale exec --mode canonical --omx-replay-dedupe strict -- submit --codex --gemini --openclaw --dry-run
```

## Common Workflows

Inspect loaded config:

```bash
agent-session-vault config --json
```

Inspect storage:

```bash
agent-session-vault storage summary --json
```

Run the default projection-first sync:

```bash
agent-session-vault sync auto imac --json
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

## Current Boundaries

- `projection delta-first` is the default cross-machine path; full raw sync remains explicit.
- Cloud sync tools are treated as directory relays, not first-class backends.
- The current flagship client set is `Codex`, `Gemini CLI`, and `OpenClaw`.
- This repository manages session views and transport, not provider billing truth.
- Live client roots are not destructively rewritten.

## For Agents

Use the repository CLI rather than re-implementing sync, projection, or archive logic.

Typical agent tasks:

- define machines and root rules
- run `sync auto <machine>`
- build `raw` or `canonical` Tokscale views
- offload older raw trees into archive bundles when local storage should shrink

## Documentation

- [Docs index (English)](docs/en/README.md)
- [Configuration guide (English)](docs/en/CONFIGURATION.md)
- [Architecture guide (English)](docs/en/ARCHITECTURE.md)
- [Agent guide (English)](docs/en/AGENT_GUIDE.md)
- [文档索引（中文）](docs/zh/README.md)
- [配置说明（中文）](docs/zh/CONFIGURATION.md)
- [架构说明（中文）](docs/zh/ARCHITECTURE.md)
- [Agent 使用指南（中文）](docs/zh/AGENT_GUIDE.md)

Internal planning notes remain repo-local and Chinese-first unless they are explicitly promoted into the public bilingual surface.

## Technical Validation

```bash
python3 -m pytest
```

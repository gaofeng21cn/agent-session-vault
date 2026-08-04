# Agent Guide

## What An Agent Should Assume

When an agent is asked to use this repository, the safe default assumptions are:

- OPL Fleet is the node, network, and task-dispatch source of truth; do not rebuild a machine control plane in Session Vault
- Session Vault is a standard Fleet data job; do not require a separate per-node capability declaration or installation
- `projection-first` is the default sync path
- raw Tokscale reads local/remote projections and managed extras, not the live HOME
- raw sync remains available, but should be explicit
- Tokscale is downstream and should not be patched by this repository
- the repository may need both home-level roots and project-level root globs

## Recommended Agent Prompt

```text
Install and use this repository. Enroll every machine through OPL Fleet and treat Session Vault as a Fleet-dispatched projection/Tokscale aggregation job. Do not duplicate node addresses or SSH routes, and prepare Tokscale raw or canonical views without modifying Tokscale upstream.
```

## Common Agent Tasks

### Enroll Machines

Join and reconcile each node through OPL Fleet. Session Vault requires no duplicate node address, SSH route, or HOME path.

### Import A Remote Machine

Use:

```bash
agent-session-vault sync fleet --json
```

Use `sync auto <machine>` only for legacy configuration compatibility or focused single-node diagnosis.

### Prepare Tokscale

Routine daily submissions should use the repository-owned deterministic runner. It resolves npm latest, passes the package through the repository Tokscale entrypoint, and rechecks help plus the official preview only when the package version changes.

```bash
agent-session-vault ops daily-tokscale --mirror-stable --json
```

The runner incrementally refreshes `imports/local-home/.raw` before remote sync. The raw daily path does not rebuild canonical trees by default. `--mirror-stable` completes the default Tokscale analytics-continuity goal; do not report that goal as failed merely because optional `full_fidelity_restore_ready=false`.

For one-off manual inspection, continue to use the repository `tokscale exec` entrypoint rather than running Tokscale naked. Raw exec refreshes the local projection automatically; `tokscale env` remains read-only.

For a submission-aligned view:

```bash
agent-session-vault tokscale exec --mode raw -- submit -c codex,gemini,openclaw --dry-run
```

For a stricter internal accounting view:

```bash
agent-session-vault tokscale exec --mode canonical --omx-replay-dedupe strict -- submit -c codex,gemini,openclaw --dry-run
```

### Archive Cold Data

Use archive commands when the task is about storage pressure, not when the task is simply “make Tokscale see the latest remote usage”.

## What An Agent Should Not Do

- Do not read Fleet's private route file or copy its node inventory.
- Do not silently replace hostname-based machine identity with raw IP addresses.
- Do not patch Tokscale, OMX, Gemini CLI, or OpenClaw upstream just to make accounting easier.
- Do not delete live client roots as part of routine sync.
- Do not treat full conversation migration as a default requirement; use `--include-live-sessions` only when complete text, search, or session resumption is explicitly required.

## Good Operational Pattern

1. inspect Fleet node status and local paths
2. run `sync fleet` for concurrent all-node projection sync
3. build `raw` or `canonical` Tokscale view
4. only reach for archive or explicit raw sync when the operator actually needs those layers

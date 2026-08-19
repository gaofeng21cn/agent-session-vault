# Operations Guide

This guide owns supported command procedures, operational checks, receipts,
and destructive-operation boundaries. Configuration fields are defined in
[Configuration](CONFIGURATION.md); ownership and data flow are defined in
[Architecture](ARCHITECTURE.md).

## Prerequisites

- installation completed as described in the root README
- a readable local configuration
- OPL Fleet for cross-machine projection jobs
- `npm` and `npx` for Tokscale
- `bsdtar` and `zstd` for packed stable storage and full archives
- a mounted, writable archive root before archive operations

Use `--config <path>` before the top-level command when a non-default file is
required.

## Read-Only Inspection

Start every diagnosis with the narrowest relevant readback:

```bash
agent-session-vault config --json
agent-session-vault storage summary --json
agent-session-vault tokscale env --json
agent-session-vault archive list --json
```

`tokscale env` reports the managed projection environment. It does not refresh
any projection and does not run Tokscale.

## Refresh Projections

The routine cross-machine command refreshes the controller projection, asks
OPL Fleet to run the projection job on every approved candidate node, imports
returned artifacts, and reports one result per node:

```bash
agent-session-vault sync fleet --json
```

Read the per-node `results[].status` values. A node that Fleet cannot admit or
complete remains visible with its Fleet payload; it does not acquire a second
machine configuration in this repository.

Refresh only the current machine when diagnosing local inputs:

```bash
agent-session-vault sync local-home-projection --json
```

For a volatile Codex home that is outside the normal live roots, ingest it
explicitly into managed, append-only extras:

```bash
agent-session-vault sync local-codex --source /path/to/runtime-root --json
```

Use repeated `--source` or `--source-glob` options for multiple explicit
sources. Missing sources are returned in `missing_sources`; they are not
silently discovered from old runtime or cold-archive layouts.

## Run Tokscale

Inspect the effective projection-only environment first:

```bash
agent-session-vault tokscale env --json
```

Run the official Tokscale preview against that view:

```bash
agent-session-vault tokscale exec -- submit -c codex,gemini,openclaw --dry-run
```

Everything after `--` is passed to Tokscale. By contrast, placing Vault's
`--dry-run` before the separator only prints the `npx` invocation:

```bash
agent-session-vault tokscale exec --dry-run -- submit -c codex,gemini,openclaw
```

Except for Vault's command-printing dry run, `tokscale exec` refreshes the
current HOME projection before invoking Tokscale. Do not run a naked
`tokscale` command because that bypasses the managed HOME and extra roots.

When a manual run requires a specific package, pin it for that invocation:

```bash
AGENT_SESSION_VAULT_TOKSCALE_PACKAGE=tokscale@<version> \
  agent-session-vault tokscale exec -- submit -c codex,gemini,openclaw --dry-run
```

## Daily Aggregate Submit

The operational entrypoint refreshes local and Fleet projections, validates
the resulting view, resolves the current Tokscale package, checks a new submit
contract with official help and preview, and submits once from the controller:

```bash
agent-session-vault ops daily-tokscale --mirror-stable --json
```

This is a real external submission. Run it only when submission is intended.
It uses a process lock and writes `current.json`, per-run logs, and a terminal
`receipt.json` below the configured file's `ops/daily-tokscale` directory unless
`--run-root` overrides it.

Treat only `status: confirmed` plus the current receipt's statistics as a
confirmed run. `already_running`, `failed`, `unconfirmed`, and
`incomplete_receipt` are not equivalent to submission success. A stable mirror
warning does not invalidate an already confirmed submit; inspect
`stable_mirror.status` separately.

## Stable Analytics Recovery

Preview and then create a packed, verified restore copy of imports, managed
extras, configuration, and Tokscale custom pricing:

```bash
agent-session-vault storage mirror-stable --dry-run --json
agent-session-vault storage mirror-stable --json
agent-session-vault storage migration-plan --json
```

Restore only from a verified stable manifest and always into a separate
destination. Existing target item paths are rejected:

```bash
agent-session-vault storage restore-stable \
  --dest-root /path/to/restore-staging \
  --json
```

Use repeated `--label` options to restore selected items. The default stable
profile is analytics continuity. `--include-live-sessions` is an explicit
migration copy, not a routine Tokscale input and not a substitute for the Codex
archive catalog. Stop client writers before using that option.

## Full-Fidelity Codex Archive

Initialize the configured archive root once, then run the cadence-aware cycle:

```bash
agent-session-vault archive init --json
agent-session-vault ops archive-cycle --due-only --deep --json
```

The cycle scans configured Codex sources, creates incremental immutable
objects, publishes snapshots, rebuilds catalog segments, performs deep
verification, and writes receipts. It never prunes local sources. `not_due` is
a successful no-op; `partial` requires inspection and returns a non-zero exit.

The equivalent manual stages are available for recovery and diagnosis:

```bash
agent-session-vault archive snapshot --json
agent-session-vault archive publish --staging-root /path/from/snapshot-output --verify-staged --json
agent-session-vault archive verify --snapshot <snapshot-id> --deep --json
agent-session-vault archive catalog-rebuild --json
```

Query by time, machine, session, or source as needed:

```bash
agent-session-vault archive list --session-id <session-id> --json
```

## Staging Restore

Build a digest-protected plan from the archive catalog, review it, then restore
to a staging directory:

```bash
agent-session-vault archive plan-restore \
  --destination /path/to/restore-staging \
  --session-id <session-id> \
  --plan-path /path/to/restore-plan.json \
  --json
agent-session-vault archive restore --plan /path/to/restore-plan.json --json
```

The only restore mode is staging. The command verifies archive membership and
restored checksums; it does not merge files into a live Codex home.

## Local Pruning

Pruning is the only destructive workflow. Stop Codex writers, create and
review a plan, then apply that exact plan:

```bash
agent-session-vault archive prune-plan \
  --plan-path ~/.config/agent-session-vault/prune-plans/current.json \
  --json
agent-session-vault archive prune-apply \
  --plan ~/.config/agent-session-vault/prune-plans/current.json \
  --json
```

Planning admits only cold `archived_sessions` files covered by a current,
deeply verified archive snapshot, the current projection, and a verified stable
imports mirror. It rejects shared or external hard links and records an
official Tokscale preview. Apply rechecks the plan digest, source metadata,
checksums, archive snapshots, stable coverage, and Tokscale parity before and
after deletion. If any prerequisite changes, generate a new plan; do not delete
the source manually.

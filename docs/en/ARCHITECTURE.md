# Architecture

This file owns system responsibilities, data flow, storage domains, and
invariants. It does not define configuration fields or operating procedures.

## Ownership

| Component | Owns | Does not own |
| --- | --- | --- |
| OPL Fleet | Approved node inventory, controller identity, routes, admission, job dispatch, artifact return | Projection semantics, Tokscale submission, archive contents |
| Agent Session Vault | Projection format and state, imported analytics history, managed Tokscale environment, stable recovery copies, Codex archive lifecycle | Fleet inventory, live client state, provider billing truth |
| Tokscale | Usage calculation, official preview, external submission | Cross-machine discovery, source collection, archive lifecycle |
| Codex, Gemini CLI, OpenClaw, Antigravity IDE, ZCode | Authoritative live session roots and APIs | Analytics projection and Vault archive state |

Each responsibility has one owner. Session Vault consumes Fleet and client
interfaces without copying their control state.

## Runtime Data Flow

```text
live client roots on controller
             |
             v
       local projection ----+
                            |
approved Fleet nodes        |       managed projection HOME
       |                     |               +
       v                     v               |
Fleet projection jobs -> imported projections + managed local extras
                                            |
                                            v
                                      one Tokscale run
                                      on controller
```

`sync fleet` is the only cross-machine path. Fleet selects approved candidates,
performs fresh admission, runs a self-contained projection job, and returns the
artifact. Session Vault verifies and imports that artifact below `import_root`
under the stable Fleet node identity.

The local controller projection and imported Fleet projections are kept in
per-machine `.raw/<client>` trees. The name `.raw` is a storage layout detail,
not a selectable view: the product exposes one managed Tokscale projection.

## Projection Contract

- Supported clients are Codex, Gemini CLI, OpenClaw, Antigravity IDE, and
  ZCode. Gemini history remains a separate client after the Antigravity rename.
- Antigravity IDE usage is refreshed through the official Tokscale
  `antigravity sync` RPC path using the same current package as submit. Vault
  projects the resulting usage-only cache and retains the previous cache when
  the IDE language server is unavailable.
- ZCode's live SQLite database is read through SQLite backup. Vault exports
  only model, timestamp, session identity, and token counters as JSONL, then
  combines it with any legacy `.zcode/projects` JSONL history.
- Tokscale receives `projection_home` as `HOME`; it never receives the real
  user HOME or `CODEX_HOME`.
- `TOKSCALE_EXTRA_DIRS` contains the local projection, imported Fleet
  projections, and explicit local Codex namespaces carrying `sync-state.json`.
- Workspace `.codex` roots and client live roots never enter Tokscale directly.
- Projection imports are history-preserving. A source deletion does not delete
  an already imported usage record.
- Fleet projection state supports full initialization followed by validated
  deltas. A base-snapshot mismatch is rejected instead of guessed.
- The controller performs the only aggregate submit. Remote nodes project and
  return artifacts; they do not submit.

Projection data is derived and rebuildable from available sources. It is not a
full conversation backup.

## Storage Domains

| Domain | Role | Routine Tokscale input | Destructive authority |
| --- | --- | --- | --- |
| Live client roots | Authoritative client state | Never | Client owner only |
| Projection imports | Compact, history-preserving analytics input | Yes | Rebuildable; never used to delete live sources by itself |
| Managed local extras | Explicit append-only Codex ingestion | Yes | Vault-managed namespace only |
| Stable analytics | Packed, verified recovery copy of analytics and control files | No | Restore to a separate destination |
| Optional stable migration profile | Explicit copy of live session roots for machine migration | No | Requires quiescent clients; no live merge |
| Full-fidelity Codex archive | Immutable objects, snapshots, catalog, verification, and receipts | No | Can authorize plan-driven pruning only with all other evidence |

Cloud-synced or NAS directories may host stable or archive data, but storage
location does not make them a live client root.

## Stable Analytics

The stable layer packages projection imports and managed extras into indexed
zstd shards and copies the Vault configuration and Tokscale custom pricing.
Every successful mirror has a verified manifest. Restore refuses missing or
unverified manifests and writes only below a separate destination.

This layer protects analytics continuity. Its optional live-session profile is
an explicit migration copy and remains separate from both routine Tokscale
inputs and the searchable Codex archive.

## Full-Fidelity Archive

The archive scans only configured Codex sources and allowed session/index
files. It identifies a source by stable machine identity and source root,
stores immutable content-addressed objects, publishes snapshot manifests, and
builds catalog segments for query and restore.

Restore plans are digest-protected and always use staging mode. Local pruning
is a separate digest-protected plan that requires all of the following at plan
and apply time:

- current source identity, metadata, and checksums;
- deep verification of the referenced archive snapshots;
- current projection coverage for every candidate;
- a verified stable mirror covering projection imports;
- no live-session or external hard-link sharing;
- unchanged official Tokscale preview before and after deletion.

No snapshot, stable mirror, projection, or successful test alone authorizes
deletion.

## Non-Goals

- Maintaining machine inventory, routes, or artifact transport
- Offering multiple selectable Tokscale views
- Patching Tokscale or client upstreams
- Enabling Antigravity CLI or DeepSeek Harness before their source format is
  available in the current official Tokscale package and approved here
- Treating provider billing as a Vault calculation
- Restoring directly into a live Codex home
- Keeping compatibility commands for retired workflows

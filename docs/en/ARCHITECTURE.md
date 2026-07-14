# Architecture Guide

## Design Goal

`agent-session-vault` is not trying to become a cloud sync platform.

Its job is narrower and more practical:

- discover session roots
- move or project them between machines
- build views that downstream tools such as Tokscale can read
- keep colder data archivable without making the hot layer grow forever

## Core Layers

The current model is easiest to reason about as four layers.

### 1. Live Roots

These are upstream-owned directories such as:

- `~/.codex`
- `~/.gemini`
- `~/.openclaw`
- project-level `.codex`

This repository does not treat itself as the owner of those trees.

### 2. Imported Raw Projection Layer

Local and imported-machine analytics projections land under:

```text
import_root/local-home/.raw/<client>
import_root/<machine>/.raw/<client>
```

For the default path, this is usually imported from a projection bundle, not a byte-for-byte raw mirror.

For Tokscale workflows, this layer accumulates imported history in an append-only way.

If the remote machine later deletes session files or an entire root, projection manifests and inventories still record that change, but the local `.raw` tree does not discard already imported history.

### 3. Canonical Layer

Canonical views are stricter local session layouts used for Tokscale or internal analysis.

Examples:

- `shadow_home`
- `import_root/<machine>/<client>`
- `local_workspace_extras/*/codex`

When canonical mode is selected, the current implementation requires `--omx-replay-dedupe strict`.

## 4. Archive Layer

Cold data can be packed into `tar.zst` bundles and moved under `archive_root`.

The backend is directory-native by design. A bundle can live on:

- a local disk
- a NAS mount
- a OneDrive-synced directory
- an iCloud-synced directory

## Projection-First Sync

`sync auto` is the default cross-machine path.

`sync local-home-projection` incrementally refreshes the current machine. It reads only the standard current-HOME roots, not legacy workspace runtimes or cold archives. The daily runner performs this step automatically before remote sync.

The high-level flow is:

1. discover roots for a configured machine
2. refresh persistent projection state under `remote_state_root`, reprojecting only new or changed files
3. build a full or delta bundle against the local base snapshot
4. choose `ssh` or `relay` transport by bundle size threshold
5. import the bundle locally into `import_root/<machine>/.raw/*`
6. optionally canonicalize after import

This is why large remote histories no longer require full raw mirroring for the Tokscale use case.

The key distinction is among compute incrementality, transport incrementality, and submission history:

- remote state reuses unchanged projection blobs by source path, size, mtime, and projector version
- bundles use true snapshot-based incremental transport; missing or incompatible state triggers a complete projection rebuild
- local `.raw` and canonical views keep the cumulative history Tokscale needs instead of treating upstream cleanup as local data loss

## Projection Behavior By Client

### Codex

Codex projection preserves the original order of every parseable JSON record because Tokscale's accounting parser is stateful. The slim records retain only parser-relevant fields, including:

- every `session_meta` identity, fork/source, provider, agent, and workspace field
- every `turn_context` model and turn identifier
- `task_started`, human-versus-injected `user_message`, `token_count`, and terminal event structure
- top-level headless model, timestamp, and usage records

Conversation bodies and unrelated payload fields are removed. Human prompts become a `user` sentinel, while known injected-context prefixes remain distinguishable. The projector version is stored in local state and projection manifests so a schema change forces a rebuild instead of reusing an incompatible delta baseline.

This keeps Tokscale totals unchanged while making large Codex sessions substantially smaller; it is not a full-fidelity conversation copy.

### Gemini CLI

Gemini projection keeps chat JSON files under `chats/`.

The current goal is to preserve the files Tokscale-relevant workflows actually need, not to mirror every transient file under the Gemini state tree.

### OpenClaw

OpenClaw projection keeps usage-relevant structure while stripping large message bodies.

It also normalizes non-standard `.jsonl` suffix variants into standard `.jsonl` paths when needed, because Tokscale only recognizes part of the raw OpenClaw file naming surface.

## Raw And Canonical Tokscale Views

### Raw

Raw mode points Tokscale at:

- an empty `projection_home`, preventing direct live-HOME discovery
- the current-machine projection under `import_root/local-home/.raw/*`
- imported machine `.raw` trees
- managed local extras

This is the default submission policy. Every usage input first lands in the mirrorable analytics layer; live client roots are not exposed directly to Tokscale.

### Canonical

Canonical mode points Tokscale at:

- `shadow_home`
- canonicalized imported machine trees
- canonicalized local project extras

Use it when you want a stricter internal accounting view and are willing to apply explicit replay dedupe.

## What This Architecture Does Not Do

- it does not claim billing truth for provider pricing
- it does not rewrite Tokscale upstream
- it does not rewrite upstream client source trees destructively
- it does not hardcode cloud vendors into the core logic

## Durability And Migration Boundary

Four states must remain distinct:

- `synced`: a remote projection was applied to local hot imports
- `submitted`: Tokscale returned a confirmed server response
- `analytics mirrored`: imports, managed extras, and control-plane config passed stable source coverage readback
- `full-fidelity migration ready`: the explicit optional analytics-plus-live-session profile passed migration readback

The default product contract is Tokscale analytics continuity. Once the local projection, remote projections, managed extras, and control-plane config pass stable readback, they can be restored on another computer for continued recomputation and submission. `full_fidelity_restore_ready=false` does not block that default goal.

Live roots such as `~/.codex/sessions` and `~/.codex/archived_sessions` enter the optional migration profile only when complete conversation text, search, or session resumption is required. Stop client writers before explicitly running `storage mirror-stable --include-live-sessions`.

Credentials are excluded from the stable mirror. Reauthenticate on the new machine, restore session data, and rebuild canonical/cache state.

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

Imported machine data lands under:

```text
import_root/<machine>/.raw/<client>
```

For the default path, this is usually imported from a projection bundle, not a byte-for-byte raw mirror.

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

The high-level flow is:

1. discover roots for a configured machine
2. build a full projection bundle on the remote machine
3. choose `ssh` or `relay` transport by bundle size threshold
4. import the bundle locally into `import_root/<machine>/.raw/*`
5. optionally canonicalize after import

This is why large remote histories no longer require full raw mirroring for the Tokscale use case.

## Projection Behavior By Client

### Codex

Codex projection keeps only the pieces needed for downstream usage accounting and session identity:

- leading `session_meta`
- the first `turn_context`
- `token_count` events
- terminal events such as `task_complete` or `turn_aborted`

This makes large Codex sessions much smaller while preserving usage-relevant structure.

### Gemini CLI

Gemini projection keeps chat JSON files under `chats/`.

The current goal is to preserve the files Tokscale-relevant workflows actually need, not to mirror every transient file under the Gemini state tree.

### OpenClaw

OpenClaw projection keeps usage-relevant structure while stripping large message bodies.

It also normalizes non-standard `.jsonl` suffix variants into standard `.jsonl` paths when needed, because Tokscale only recognizes part of the raw OpenClaw file naming surface.

## Raw And Canonical Tokscale Views

### Raw

Raw mode points Tokscale at:

- the local live home
- imported machine `.raw` trees
- local project-level `.codex` roots

Use it when you want a view close to actual upstream layout and Tokscale submission behavior.

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

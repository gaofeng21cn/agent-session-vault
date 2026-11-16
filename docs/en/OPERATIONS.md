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
agent-session-vault tokscale exec -- submit -c codex,gemini,openclaw,antigravity,zcode --dry-run
```

Everything after `--` is passed to Tokscale. By contrast, placing Vault's
`--dry-run` before the separator only prints the `npx` invocation:

```bash
agent-session-vault tokscale exec --dry-run -- submit -c codex,gemini,openclaw,antigravity,zcode
```

Except for Vault's command-printing dry run, `tokscale exec` refreshes the
current HOME projection before invoking Tokscale. Do not run a naked
`tokscale` command because that bypasses the managed HOME and extra roots.

When a manual run requires a specific package, pin it for that invocation:

```bash
AGENT_SESSION_VAULT_TOKSCALE_PACKAGE=tokscale@<version> \
  agent-session-vault tokscale exec -- submit -c codex,gemini,openclaw,antigravity,zcode --dry-run
```

## Daily Aggregate Submit

The scheduled job runs a NAS preflight before any write. It verifies the
`Personal-Drive` mount, archive identity, and configured `archive/stable/staging`
paths. SMB path checks run in an isolated worker with a 45-second hard timeout;
`status: blocked` or a timeout stops the job before submission and reports the
failing `phase` for file-access recovery. The real submit command is allowed
only after the preflight returns `status: ready`.

The operational entrypoint refreshes local and Fleet projections, validates
the resulting view, resolves the current Tokscale package, checks a new submit
contract with official help and preview, and submits once from the controller:

```bash
agent-session-vault ops daily-tokscale --mirror-stable --json
```

This is a real external submission. Run it only when submission is intended.
Before projection, the runner uses that current package to refresh Antigravity
IDE's official RPC cache on each available node. An unavailable Antigravity
language server is reported as `skipped_unavailable` and does not discard the
previous cache. When the official log explicitly reports `trajectory not found`,
the runner moves confirmed stale local candidates into a recoverable quarantine
with a manifest and retries the official sync once; recent or unproven sessions
are left in place. The receipt records the repair and retry evidence under
`source_sync.antigravity.repair` and `attempts`. ZCode SQLite is snapshotted and converted to usage-only JSONL;
conversation content is not copied from the database.
It uses a process lock and writes `current.json`, per-run logs, and a terminal
`receipt.json` below the configured file's `ops/daily-tokscale` directory unless
`--run-root` overrides it.

Treat only `status: confirmed` plus the current receipt's statistics as a
confirmed run. `already_running`, `failed`, `unconfirmed`, and
`incomplete_receipt` are not equivalent to submission success. A stable mirror
warning does not invalidate an already confirmed submit; inspect
`stable_mirror.status` separately.

When `--mirror-stable` is requested and the local projection succeeds, the
runner attempts the stable mirror on every terminal path, including Fleet
discovery or submit failure. The mirror covers projections available before the
failure. Mirror success does not confirm delivery to Tokscale, and mirror
failure does not replace the primary run result. A transport interruption after
sending a request leaves delivery unknown; the runner never retries a real
submit automatically.
For an unconfirmed submit, `tokscale.submit_error` retains the CLI's error and
transport detail (when present), also included in `error.message`. Pricing
warnings are not submission failures. The full `submit_log` remains the source
for further diagnosis; a server error does not establish whether data was stored.

The local scheduler should call one fixed launcher script rather than assemble
the business command in its prompt. That script owns the required working
directory, read-only HTTPS probe, unchanged NAS preflight, exact Python detached
launcher invocation, launch lock, and durable launch record. Only read-only
connection probes may retry. It reattaches to a live or unresolved original
launch and exposes a read-only status operation bound to that runner's receipt
and actual exit code. GET response 405 from the submit endpoint proves only that
the HTTPS connection works; it does not prove POST delivery. Deployment paths,
machine identities, logs, and scheduler configuration stay outside the repository.

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

`--due-only` first compares `archived_sessions` paths and content hashes with
the latest deep-verified snapshot for each source. An uncovered or changed
historical session triggers a cycle immediately without waiting for
`cadence_days`. The cadence is only the fallback interval when all historical
sessions are covered.

The cycle scans configured Codex sources, creates incremental immutable
objects, publishes snapshots, rebuilds catalog segments, performs deep
verification, and writes receipts. It never prunes local sources. `not_due`
means the fallback cadence has not elapsed and all historical sessions are
covered; it is a successful no-op. `partial` requires inspection and returns a
non-zero exit.

Before packing, files reuse members across the archive by `(byte length, SHA-256)`;
source, session ID and path do not define content identity. Moving a conversation
from `sessions` to `archived_sessions`, or bringing back an older file, updates its
logical path/source/session metadata while retaining the existing member reference.
Equal new files in one cycle are packed once and restore to their individual paths.
The in-memory index is rebuilt from all committed snapshots after checking snapshot/
manifest checksums and object presence/length. Staging, catalog entries, projections
and uncommitted snapshots are not reuse authority. Deep verification continues to
check actual object and member bytes. Legacy TAR hardlinks are resolved to their
content inside the package during restore, without following external targets.

For repeated fixed sources or path-only changes, `dedup.new_content_files`,
`new_content_bytes`, `new_objects` and `new_archive_bytes` are all zero.
`reused_files` and `reused_logical_bytes` count logical file reuse;
`historical_reused_files` and `same_cycle_reused_files` separate historical and
same-cycle reuse. Snapshot JSON, cycle stdout and receipts preserve these counters;
they are not reclaimed-space figures. New manifests include only referenced bundles,
so deep verification does not inherit historical bundles with no current files.
Old snapshots and objects remain intact; no automatic repacking or garbage collection runs.
Scheduled maintenance checks that the reuse counters are present, nonnegative and
consistent before creating any prune plan. Missing/invalid counters stop later stages.

### Explicit compaction of existing archives

After user authorization for existing-storage optimization, use the one-shot
migration rather than adding it to biweekly or daily schedules. Reconcile previous
runner evidence and pass the configured NAS preflight, then create a durable local
log directory under `ops/archive-compaction/<unique-ID>` in the configuration
directory. Launch each write command exactly once through `run-detached.py`, named
`compact-plan` and `compact-apply`, using the installed absolute CLI path. Wait for
terminal stdout JSON and the real exit_code in order:

```sh
agent-session-vault archive compact-plan --run-dir <run-log-dir> --json
agent-session-vault archive compact-apply --plan <run-log-dir>/plan.json --json
agent-session-vault archive compact-status --plan <run-log-dir>/plan.json --json
```

Only plan exit 0/planned admits apply. The plan binds current identity, exact source
inventory/signatures, metadata SHA-256, all logical-history digests and workspace
budget; source drift stops migration. Apply streams into same-NAS
`operations/archive-compaction/<unique-ID>/candidate`, storing each file content
once in bundles targeting 512 MiB of logical data. After one deep member check per
output bundle, complete history equivalence and an actual restore sample, it saves
verification evidence and original metadata before switching back to the original
archive path. Logical history, old restore plans, configuration and live files are
preserved. Post-switch coverage and another NAS preflight gate exact removal of
this run's retired containers. No measured storage saving stops with the original
archive retained. Report removed containers, new bundle costs, net reduction and
actual NAS-volume availability separately.

On macOS, `statvfs` can wrap large SMB counters every 4 TiB. Workspace admission
and cold-data metrics use native `df -kP` counters to avoid false insufficient-space
decisions, without exposing mount sources that may include temporary login IDs.
Keep original receipt values and annotate their measurement limits. Report directory
reduction, unlink and final server-side physical reclamation separately. Without a
reliable comparable baseline or server evidence, physical reclaimed bytes remain
unconfirmed.

The NAS run directory retains `plan.json`, `journal.jsonl`, `progress.json`,
`verification.json`, `original-metadata.tar.zst` and, on success, `result.json`.
Actual restore samples stay inside the local run log directory. `compact-status`
only reads existing evidence and detached-runner state; it never resumes work.
Interruption/failure retains the maintenance fence, candidate and remaining source.
Without the real exit_code, result/receipt files cannot prove command success;
never reissue apply or retry failed deletion. Only an unambiguously failed second
rename allows one rollback by the original runner. Scheduled cycles encountering
the maintenance fence must stop and report its original run, without bypassing it.
Auxiliary cold-data packages and stable analytics are outside this migration.

An unmanifested `._*` qualifies as metadata noise only after checking AppleDouble
magic/version, entry bounds, Finder/xattr entry types, an empty resource fork and
the corresponding indexed original. The prefix alone is insufficient. Exclusion
evidence retains paths, SHA-256 and logical bytes. Explicit file-list packing also
disables xattrs, ACLs, fflags, Mac metadata and implicit directory recursion to avoid
generating new noise.

If the original apply genuinely exited 2 at legacy-noise recognition, before any
switch and with the original archive unchanged, a corrected implementation may
run `compact-recovery-plan --plan <original-plan.json> --json` as a separate phase
to authenticate existing packs and the original terminal evidence. Only its real
exit 0/verified admits one `compact-recover --plan <original-plan.json> --json`.
Both stages launch detached once with their own matching log names, without
reissuing apply. Recovery binds source signatures, original stdout/exit, fence and
every verified candidate member, reusing completed packs and authenticated source
progress. Unfinished fragments remain in separate custody until successful full
coverage permits their exact cleanup. Unknown exits, other failures, an existing
switch or recovery intent prohibit this entrypoint and retain the evidence.

Appended revisions, different contents with the same session ID, and different raw
`.jsonl`/`.jsonl.zst` representations can still add data. Recoverable history is kept
without discarding revisions by ID or normalizing away original file bytes. If a
concurrent write invalidates shared-content grouping or converges to known content,
the build stops with diagnostic staging rather than publishing incorrect references
or known redundancy. It does not automatically relaunch an archive command.

Inspect archive-wide reuse and legacy duplication read-only:

```bash
agent-session-vault archive content-status --json
```

This checks committed metadata, object presence and sizes, and reports content
identities, distinct bundle/member locations and cross-object redundant logical
bytes. It does not read live databases, rebuild the catalog, deeply decode every
object, or delete history. Redundant logical bytes are not directly reclaimable
compressed NAS space and do not establish semantic conversation duplication.

Codex `.jsonl.zst` files under `archived_sessions` are included as opaque
full-fidelity members and are not added to the Tokscale projection. They can be
pruned after the same deep archive and restore checks when they are cold; the
Tokscale dry-run must remain unchanged before and after the operation.

During snapshot construction, discovered Codex files are pinned in a private local
capture before hashing and parsing. A local capture uses hardlinks, with an
open-descriptor copy for filesystems that cannot link to local staging; no
symlinks are created. Codex can move a captured session from `sessions` to
`archived_sessions` while the cycle continues. A move immediately before
capture is resolved only to the exact archived filename and recorded as a
`relocated-during-capture` warning. Captures are removed when construction ends;
manifest hashes still describe the actual packed bytes. A missing packed member
fails construction rather than being omitted.

Snapshot construction failures return terminal JSON with `status: "failed"`,
`reason: "snapshot_build_failed"`, `error`, and the exact `staging_root`, with a
non-zero exit. Existing cycle staging is never overwritten. An incomplete
bundle in retained staging is diagnostic material, not a published snapshot;
verify its coverage in a successful archive before removing that exact staging.
Publish, deep verification, state and receipt errors also return structured failure JSON
with the failing phase, known snapshot IDs and staging state. `receipt_path` identifies
the successfully written cycle receipt; only the detached runner records the real process exit code.

A `verified` cycle removes the exact cycle staging directory it created before
writing verified state and its receipt. The terminal JSON reports
`staging_cleanup: "removed"`. A cleanup failure returns `partial` with
`reason: "staging_cleanup_failed"`; any remaining staging is left for diagnosis.
Cycles that do not verify all sources do not attempt cleanup. If writing state
or receipts fails after cleanup succeeds, staging has already been removed;
inspect the published snapshots. Manual `archive publish` does not remove a
caller-provided staging root.

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

For a one-time historical import, use a separate configuration with explicit
source roots, then the same snapshot, publish, deep-verify, and staging-restore
commands. A `file_tree` source stores supporting files in the same archive as
`client = "files"`; query or plan their restore with `--client files`. Keep the
recurring configuration pointed at live session sources. Retire old containers
only after their contents are covered and a restore succeeds; imported
analytics projections must not be described as complete conversations.

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

Session pruning and the cold-data cleanup below are destructive workflows. Ensure
candidate files have no active writers, create and review a plan, then apply that exact plan:

```bash
agent-session-vault archive prune-plan \
  --plan-path ~/.config/agent-session-vault/prune-plans/current.json \
  --json
agent-session-vault archive prune-apply \
  --plan ~/.config/agent-session-vault/prune-plans/current.json \
  --json
```

Planning admits only cold `archived_sessions` files covered by a current,
deeply verified archive snapshot and a verified stable imports mirror. Normal
JSONL sessions still require their current projection file; opaque `.jsonl.zst`
members intentionally do not, because Tokscale does not read them. The plan
rejects shared or external hard links and records an official Tokscale preview.
Apply rechecks the plan digest, source metadata, checksums, archive snapshots,
stable coverage, and Tokscale parity before and after deletion. If any
prerequisite changes, generate a new plan; do not delete the source manually.

## Biweekly Maintenance And Cold Archives

Authorized local schedules use one entrypoint instead of reconstructing session stages:

```bash
agent-session-vault ops archive-maintenance --json
agent-session-vault ops archive-maintenance --status --json
```

The first command checks NAS readiness and launches the maintenance runner through
local `~/.config/agent-session-vault/run-detached.py`, maintained in
[`scripts/run_detached.py`](../../scripts/run_detached.py). Every stage reserves one
launch and records its own PID, stdout, stderr and actual `exit_code`. Startup and
supervisor errors have separate evidence and never fabricate a business exit code.
`--status`, optionally with `--run-dir <run-directory>`, is read-only; it does not
resume, relaunch or delete. An unknown outcome, live runner or unfinished cold stage
blocks a new maintenance run until the original evidence is reconciled.

The runner invokes `archive-cycle --no-due-only --deep` once, then prune-plan only
after verification. It exclusively captures the newly generated plan as the run's
`prune-plan.json`, checks creation time, ID and digest, and applies that exact copy
once. An empty plan skips only prune-apply. NAS preflight runs before each stage;
any failure or unverified outcome stops the workflow. It never submits Tokscale,
refreshes stable to bypass a guard, or replaces the external biweekly scheduler.

After the session runner exits `0/session_verified`, launch each following stage
once through the same detached launcher:

```bash
agent-session-vault ops cold-inventory --run-dir <run-directory> --json
agent-session-vault ops cold-plan --run-dir <run-directory> --decisions <run-directory>/decisions.json --json
agent-session-vault ops cold-pack --run-dir <run-directory> --json
agent-session-vault ops cold-cleanup --run-dir <run-directory> --json
```

Inventory measures whole deliverables, session directories and run batches using
the run time minus 30 days and the latest batch mtime, ctime or creation time. A
recent root is retained without traversing old dependencies inside it. Scope is
limited to visualizations, generated_images, independent recovery/backup history,
RedCube workspaces, automation history batches, old standalone releases and
marketplace `.staging` sub-batches. Databases, live sessions, analytics inputs and
current controls are excluded. Every category has a summary, including empty ones.
Scanning does not follow symlinks; cross-volume trees, special files and active-lock
material are retained.

AI reviews purpose, whole-batch completion, current pointers, processes and current
work, then writes decisions in the persistent run directory. See
[`config/codex-cold-decisions.example.json`](../../config/codex-cold-decisions.example.json).
Each selected batch requires completion and reference explanations plus persistent
evidence paths. Runtime batches additionally require `retired_runtime: true`.
Unselected or uncertain material stays local; an empty `archive` list is normal.
Code enforces actual scope, current control references (including extensionless
IDs), processes and open files. Asset search hits or input errors retain the asset
group. The actual CLI/current release and the two most recent other releases stay
local, together with any other referenced release.

The plan binds the run's inventory/decisions SHA-256 and records absolute sources,
whole-batch boundaries, coldness, counts, logical bytes, per-file hashes and metadata.
Pack and cleanup require the plan SHA-256 from this run's original runner output;
old plans are not reused. Sources and references are rechecked before/after packing
and before moving. TAR uses an explicit file list and `--no-recursion --no-xattrs
--no-mac-metadata --no-acls --no-fflags`, writing to an exclusive per-run named file
to avoid macOS compressed-stdout padding. One decode verifies paths, structural
members, content hashes and logical hardlink contents, followed by an actual sample
restore into independent run staging.

Immutable packages go beside the configured archive root under
`auxiliary/codex-home-archives/<run-id>/`; manifests and receipts go under
`operations/space-optimization/<run-id>/`. Empty categories produce no packages.
Cleanup checks NAS packages, source bytes, evidence and references again, moves to
the same-volume exclusive `.Trash/codex-cold-<run-id>/`, verifies all moved content,
then purges only the exact set. File deletion uses no-follow directory descriptors;
directories use `rmdir`, never arbitrary recursive removal. Changes, permission
errors or unknown outcomes stop immediately, retaining sources, packages, Trash and
per-item progress. Deletion is not retried and other user Trash is untouched.

`space-before.json` and the cleanup receipt record directory allocation, volume
available bytes and read-only database sizes separately. Logical purged bytes are
not physical SSD recovery: APFS snapshots and concurrent writes affect volume
changes. This workflow does not maintain databases or remove system snapshots.
Restore the relevant TAR into a separate directory and verify relative paths, bytes
and hashes against `cold-plan.json` before deciding how to retrieve the material.

## Read-only archive review

Use the same owner data in an Agent response or the shared OPL view:

```bash
agent-session-vault archive review --json
agent-session-vault archive review --plan /path/to/prune-plan.json --expected-revision <plan_digest> --json
agent-session-vault archive review --plan /path/to/prune-plan.json --receipt /path/to/receipt.json --json
```

`review` reads current stable coverage and an existing restore or prune plan. It
validates the plan digest and rejects a different requested revision. Prune
review checks current local source identity, stable imports coverage, and archive
object checksums; it shows the exact snapshot IDs. Deep member verification and
Tokscale parity still run in `prune-apply`. A review is not execution approval.

The shared `vault.archive.review` view uses `agent.session.vault.v1#review` with
optional absolute `config_path`, `plan_path`, `receipt_path` and
`expected_revision`. Its adapter calls the same Python owner, exposes no actions,
and does not create plans, receipts or runtime state. The descriptor is a
`development_only` source integration; it does not claim a published or installed
carrier. The source tree must contain its `src/` package alongside the adapter.

When a prune plan is selected, matching owner receipts are read automatically.
A partial/failed/unknown receipt stays distinct from the current source checks;
a missing file alone does not prove deletion by that operation. Compare the
receipt with the displayed remaining sources and recorded archive snapshots
before creating a new plan through the existing CLI. Refresh and reopen never
retry an operation. Restore plans remain bound to their staging destination and
collision policy.

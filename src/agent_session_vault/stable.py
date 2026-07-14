from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from .config import VaultConfig


@dataclass(frozen=True)
class StableMirrorItem:
    label: str
    kind: str
    role: str
    source: Path
    destination: Path


@dataclass(frozen=True)
class StableMirrorItemResult:
    label: str
    kind: str
    role: str
    source: Path
    destination: Path
    source_exists: bool
    source_bytes: int
    source_files: int
    status: str
    duration_seconds: float
    command: list[str] | None
    exit_code: int | None
    stderr: str | None
    coverage_status: str
    verified_files: int
    missing_files: int
    mismatched_files: int
    source_manifest_fingerprint: str | None
    transfer_status: str


@dataclass(frozen=True)
class StableMirrorResult:
    stable_root: Path
    manifest_path: Path | None
    attempt_path: Path | None
    dry_run: bool
    profile: str
    status: str
    mirrored_at: str
    items: list[StableMirrorItemResult]


@dataclass(frozen=True)
class _TreeSnapshot:
    is_file: bool
    entries: dict[str, tuple[int, int]]

    @property
    def total_bytes(self) -> int:
        return sum(size for size, _ in self.entries.values())

    @property
    def total_files(self) -> int:
        return len(self.entries)


def default_stable_root(config: VaultConfig) -> Path:
    return config.paths.archive_root.expanduser().parent / "stable"


def stable_mirror_items(
    config: VaultConfig,
    stable_root: Path | None = None,
    *,
    include_live_sessions: bool = False,
) -> list[StableMirrorItem]:
    root = (stable_root or default_stable_root(config)).expanduser()
    items = [
        StableMirrorItem(
            label="imports",
            kind="directory",
            role="durable_analytics_projection",
            source=config.paths.import_root.expanduser(),
            destination=root / "tokscale" / "imports",
        ),
        StableMirrorItem(
            label="local_workspace_extras",
            kind="directory",
            role="durable_local_ingest",
            source=config.paths.local_workspace_extras.expanduser(),
            destination=root / "tokscale" / "local-workspace-extras",
        ),
        StableMirrorItem(
            label="config",
            kind="file",
            role="control_plane",
            source=config.config_path.expanduser(),
            destination=root / "config" / "config.toml",
        ),
    ]
    if not include_live_sessions:
        return items

    home = config.paths.home.expanduser()
    items.extend(
        [
            StableMirrorItem(
                label="live_codex_sessions",
                kind="directory",
                role="authoritative_live_session",
                source=home / ".codex" / "sessions",
                destination=root / "live" / "codex" / "sessions",
            ),
            StableMirrorItem(
                label="live_codex_archived_sessions",
                kind="directory",
                role="authoritative_live_session",
                source=home / ".codex" / "archived_sessions",
                destination=root / "live" / "codex" / "archived_sessions",
            ),
            StableMirrorItem(
                label="live_codex_session_index",
                kind="file",
                role="live_session_index",
                source=home / ".codex" / "session_index.jsonl",
                destination=root / "live" / "codex" / "session_index.jsonl",
            ),
            StableMirrorItem(
                label="live_codex_history",
                kind="file",
                role="live_session_index",
                source=home / ".codex" / "history.jsonl",
                destination=root / "live" / "codex" / "history.jsonl",
            ),
            StableMirrorItem(
                label="live_gemini_chats",
                kind="directory",
                role="authoritative_live_session",
                source=home / ".gemini" / "tmp",
                destination=root / "live" / "gemini" / "tmp",
            ),
            StableMirrorItem(
                label="live_openclaw_agents",
                kind="directory",
                role="authoritative_live_session",
                source=home / ".openclaw" / "agents",
                destination=root / "live" / "openclaw" / "agents",
            ),
        ]
    )
    return items


def _tree_snapshot(path: Path) -> _TreeSnapshot:
    if path.is_file():
        stat = path.stat()
        return _TreeSnapshot(is_file=True, entries={"": (stat.st_size, stat.st_mtime_ns)})
    entries: dict[str, tuple[int, int]] = {}
    for child in path.rglob("*"):
        if child.is_file():
            stat = child.stat()
            entries[child.relative_to(path).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return _TreeSnapshot(is_file=False, entries=entries)


def _tree_stats(path: Path) -> tuple[int, int]:
    snapshot = _tree_snapshot(path)
    return snapshot.total_bytes, snapshot.total_files


def _snapshot_fingerprint(snapshot: _TreeSnapshot) -> str:
    digest = hashlib.sha256()
    digest.update(b"file\0" if snapshot.is_file else b"directory\0")
    for relative, (size, mtime_ns) in sorted(snapshot.entries.items()):
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(mtime_ns).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _coverage_from_snapshot(
    snapshot: _TreeSnapshot,
    destination: Path,
) -> tuple[str, int, int, int]:
    if not snapshot.is_file and not destination.is_dir():
        return "failed", 0, 1, 0
    verified = 0
    missing = 0
    mismatched = 0
    for relative, (source_size, _) in snapshot.entries.items():
        target = destination if snapshot.is_file else destination / Path(relative)
        if not target.is_file():
            missing += 1
        elif target.stat().st_size != source_size:
            mismatched += 1
        else:
            verified += 1
    status = "verified" if missing == 0 and mismatched == 0 else "failed"
    return status, verified, missing, mismatched


def _source_coverage(source: Path, destination: Path) -> tuple[str, int, int, int]:
    return _coverage_from_snapshot(_tree_snapshot(source), destination)


def _rsync_source(path: Path) -> str:
    return f"{path}/"


def _destination_matches_snapshot_entry(destination: Path, entry: tuple[int, int]) -> bool:
    try:
        destination_stat = destination.stat()
    except OSError:
        return False
    source_size, source_mtime_ns = entry
    return destination_stat.st_size == source_size and int(destination_stat.st_mtime) == int(
        source_mtime_ns / 1_000_000_000
    )


def _stage_changed_destinations(
    source: Path,
    destination: Path,
    backup_root: Path,
    source_snapshot: _TreeSnapshot,
) -> list[tuple[Path, Path]]:
    replaced: list[tuple[Path, Path]] = []
    try:
        for relative_text, entry in source_snapshot.entries.items():
            relative = Path(source.name) if source_snapshot.is_file else Path(relative_text)
            destination_file = destination if source_snapshot.is_file else destination / relative
            if not destination_file.exists() and not destination_file.is_symlink():
                continue
            if _destination_matches_snapshot_entry(destination_file, entry):
                continue
            backup_file = backup_root / relative
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            destination_file.replace(backup_file)
            replaced.append((backup_file, destination_file))
    except Exception:
        _restore_replaced_destinations(replaced)
        raise
    return replaced


def _restore_replaced_destinations(replaced: list[tuple[Path, Path]]) -> None:
    for backup_file, destination_file in reversed(replaced):
        if destination_file.exists() or destination_file.is_symlink():
            if destination_file.is_dir() and not destination_file.is_symlink():
                shutil.rmtree(destination_file)
            else:
                destination_file.unlink()
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        backup_file.replace(destination_file)


def _discard_replaced_destinations(backup_root: Path) -> None:
    if backup_root.exists():
        shutil.rmtree(backup_root)


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    previous = path.with_name(f".{path.name}.{os.getpid()}.previous")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if path.exists() or path.is_symlink():
        path.replace(previous)
    try:
        temporary.replace(path)
    except Exception:
        if previous.exists() or previous.is_symlink():
            previous.replace(path)
        raise
    if previous.exists() or previous.is_symlink():
        previous.unlink()


def _load_verified_manifest_items(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("status") != "verified":
        return {}
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return {}
    items: dict[str, dict[str, object]] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or raw_item.get("coverage_status") != "verified":
            continue
        label = raw_item.get("label")
        if isinstance(label, str):
            items[label] = raw_item
    return items


def mirror_stable_layer(
    config: VaultConfig,
    *,
    stable_root: Path | None = None,
    dry_run: bool = False,
    include_live_sessions: bool = False,
) -> StableMirrorResult:
    root = (stable_root or default_stable_root(config)).expanduser()
    mirrored_at = datetime.now(UTC).isoformat()
    replacement_run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    replacement_root = root / ".asv-replaced" / replacement_run_id
    profile = "migration" if include_live_sessions else "analytics"
    previous_items = _load_verified_manifest_items(root / "stable-layer-manifest.json")
    results: list[StableMirrorItemResult] = []
    failed = False

    for item in stable_mirror_items(config, root, include_live_sessions=include_live_sessions):
        started = time.monotonic()
        source = item.source.expanduser()
        destination = item.destination.expanduser()
        exists = source.exists()

        if not exists:
            results.append(
                StableMirrorItemResult(
                    label=item.label,
                    kind=item.kind,
                    role=item.role,
                    source=source,
                    destination=destination,
                    source_exists=False,
                    source_bytes=0,
                    source_files=0,
                    status="skipped_missing",
                    duration_seconds=0.0,
                    command=None,
                    exit_code=None,
                    stderr=None,
                    coverage_status="not_applicable",
                    verified_files=0,
                    missing_files=0,
                    mismatched_files=0,
                    source_manifest_fingerprint=None,
                    transfer_status="not_applicable",
                )
            )
            continue

        source_snapshot = _tree_snapshot(source)
        source_bytes = source_snapshot.total_bytes
        source_files = source_snapshot.total_files
        source_manifest_fingerprint = _snapshot_fingerprint(source_snapshot)
        command: list[str] | None = None
        exit_code: int | None = None
        stderr: str | None = None
        coverage_status = "planned" if dry_run else "unverified"
        verified_files = 0
        missing_files = 0
        mismatched_files = 0
        transfer_status = "planned" if dry_run else "pending"
        backup_root = replacement_root / item.label
        replaced: list[tuple[Path, Path]] = []
        previous_item = previous_items.get(item.label)
        reuse_candidate = bool(
            not dry_run
            and previous_item
            and previous_item.get("source") == str(source)
            and previous_item.get("destination") == str(destination)
            and previous_item.get("source_manifest_fingerprint") == source_manifest_fingerprint
        )

        if reuse_candidate:
            try:
                coverage_status, verified_files, missing_files, mismatched_files = _coverage_from_snapshot(
                    source_snapshot,
                    destination,
                )
                final_snapshot = _tree_snapshot(source)
            except OSError:
                coverage_status = "failed"
                final_snapshot = source_snapshot
            if coverage_status == "verified" and final_snapshot == source_snapshot:
                results.append(
                    StableMirrorItemResult(
                        label=item.label,
                        kind=item.kind,
                        role=item.role,
                        source=source,
                        destination=destination,
                        source_exists=True,
                        source_bytes=source_bytes,
                        source_files=source_files,
                        status="mirrored",
                        duration_seconds=round(time.monotonic() - started, 3),
                        command=None,
                        exit_code=0,
                        stderr=None,
                        coverage_status="verified",
                        verified_files=verified_files,
                        missing_files=0,
                        mismatched_files=0,
                        source_manifest_fingerprint=source_manifest_fingerprint,
                        transfer_status="reused_verified",
                    )
                )
                continue
            source_snapshot = final_snapshot
            source_bytes = source_snapshot.total_bytes
            source_files = source_snapshot.total_files
            source_manifest_fingerprint = _snapshot_fingerprint(source_snapshot)
            coverage_status = "unverified"
            verified_files = 0
            missing_files = 0
            mismatched_files = 0

        file_destination_current = (
            item.kind == "file"
            and (destination.exists() or destination.is_symlink())
            and _destination_matches_snapshot_entry(destination, source_snapshot.entries[""])
        )

        if not dry_run:
            try:
                replaced = _stage_changed_destinations(source, destination, backup_root, source_snapshot)
            except OSError as exc:
                status = "failed"
                exit_code = 1
                stderr = f"destination staging failed: {type(exc).__name__}: {exc}"
                results.append(
                    StableMirrorItemResult(
                        label=item.label,
                        kind=item.kind,
                        role=item.role,
                        source=source,
                        destination=destination,
                        source_exists=True,
                        source_bytes=source_bytes,
                        source_files=source_files,
                        status=status,
                        duration_seconds=round(time.monotonic() - started, 3),
                        command=None,
                        exit_code=exit_code,
                        stderr=stderr,
                        coverage_status="unverified",
                        verified_files=0,
                        missing_files=0,
                        mismatched_files=0,
                        source_manifest_fingerprint=source_manifest_fingerprint,
                        transfer_status="failed",
                    )
                )
                failed = True
                break

        if item.kind == "directory":
            command = ["rsync", "-a", _rsync_source(source), _rsync_source(destination)]
            if dry_run:
                status = "planned"
            else:
                transfer_status = "transferred"
                destination.mkdir(parents=True, exist_ok=True)
                try:
                    completed = subprocess.run(command, text=True, capture_output=True)
                    exit_code = completed.returncode
                    stderr = completed.stderr.strip() or None
                except OSError as exc:
                    exit_code = 127
                    stderr = f"{type(exc).__name__}: {exc}"
                status = "mirrored" if exit_code == 0 else "failed"
        elif item.kind == "file":
            if dry_run:
                status = "planned"
            elif file_destination_current:
                exit_code = 0
                status = "mirrored"
                transfer_status = "unchanged"
            else:
                transfer_status = "transferred"
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                    exit_code = 0
                    status = "mirrored"
                except OSError as exc:
                    exit_code = 1
                    stderr = f"{type(exc).__name__}: {exc}"
                    status = "failed"
        else:
            raise ValueError(f"unsupported stable mirror item kind: {item.kind}")

        if not dry_run and status == "mirrored":
            try:
                final_snapshot = _tree_snapshot(source)
                coverage_status, verified_files, missing_files, mismatched_files = _coverage_from_snapshot(
                    final_snapshot,
                    destination,
                )
            except OSError as exc:
                coverage_status = "failed"
                stderr = f"coverage readback failed: {type(exc).__name__}: {exc}"
            if coverage_status != "verified":
                status = "verification_failed"
            elif final_snapshot != source_snapshot:
                status = "verification_failed"
                coverage_status = "failed"
                stderr = "source changed during mirror; stop active writers before retrying"

        if not dry_run:
            try:
                if status == "mirrored" and coverage_status == "verified":
                    _discard_replaced_destinations(backup_root)
                else:
                    _restore_replaced_destinations(replaced)
            except OSError as exc:
                status = "verification_failed"
                coverage_status = "failed"
                stderr = f"destination rollback/cleanup failed: {type(exc).__name__}: {exc}"

        results.append(
            StableMirrorItemResult(
                label=item.label,
                kind=item.kind,
                role=item.role,
                source=source,
                destination=destination,
                source_exists=True,
                source_bytes=source_bytes,
                source_files=source_files,
                status=status,
                duration_seconds=round(time.monotonic() - started, 3),
                command=command,
                exit_code=exit_code,
                stderr=stderr,
                coverage_status=coverage_status,
                verified_files=verified_files,
                missing_files=missing_files,
                mismatched_files=mismatched_files,
                source_manifest_fingerprint=source_manifest_fingerprint,
                transfer_status=transfer_status,
            )
        )
        if status in {"failed", "verification_failed"}:
            failed = True
            break

    if not dry_run and not failed and replacement_root.exists():
        shutil.rmtree(replacement_root, ignore_errors=True)
    status = "planned" if dry_run else "failed" if failed else "verified"
    manifest_path = None if dry_run or failed else root / "stable-layer-manifest.json"
    attempt_path = None if dry_run else root / "stable-layer-attempt.json"
    result = StableMirrorResult(
        stable_root=root,
        manifest_path=manifest_path,
        attempt_path=attempt_path,
        dry_run=dry_run,
        profile=profile,
        status=status,
        mirrored_at=mirrored_at,
        items=results,
    )
    if attempt_path is not None:
        _write_json_atomic(attempt_path, stable_mirror_payload(result))
    if manifest_path is not None:
        _write_json_atomic(manifest_path, stable_mirror_payload(result))
    return result


def stable_mirror_payload(result: StableMirrorResult) -> dict[str, object]:
    return {
        "schema_version": 3,
        "stable_root": str(result.stable_root),
        "manifest_path": str(result.manifest_path) if result.manifest_path else None,
        "attempt_path": str(result.attempt_path) if result.attempt_path else None,
        "dry_run": result.dry_run,
        "profile": result.profile,
        "status": result.status,
        "mirror_semantics": "source-covered-no-delete",
        "mirrored_at": result.mirrored_at,
        "items": [
            {
                "label": item.label,
                "kind": item.kind,
                "role": item.role,
                "source": str(item.source),
                "destination": str(item.destination),
                "source_exists": item.source_exists,
                "source_bytes": item.source_bytes,
                "source_files": item.source_files,
                "status": item.status,
                "duration_seconds": item.duration_seconds,
                "command": item.command,
                "exit_code": item.exit_code,
                "stderr": item.stderr,
                "coverage_status": item.coverage_status,
                "verified_files": item.verified_files,
                "missing_files": item.missing_files,
                "mismatched_files": item.mismatched_files,
                "source_manifest_fingerprint": item.source_manifest_fingerprint,
                "transfer_status": item.transfer_status,
            }
            for item in result.items
        ],
    }


def migration_plan_payload(config: VaultConfig, *, stable_root: Path | None = None) -> dict[str, object]:
    root = (stable_root or default_stable_root(config)).expanduser()
    all_items = stable_mirror_items(config, root, include_live_sessions=True)
    analytics_labels = {item.label for item in stable_mirror_items(config, root)}
    item_payloads: list[dict[str, object]] = []
    analytics_bytes = 0
    analytics_files = 0
    live_bytes = 0
    live_files = 0

    for item in all_items:
        exists = item.source.exists()
        source_bytes, source_files = _tree_stats(item.source) if exists else (0, 0)
        profile = "analytics" if item.label in analytics_labels else "migration"
        item_payloads.append(
            {
                "label": item.label,
                "kind": item.kind,
                "role": item.role,
                "profile": profile,
                "source": str(item.source),
                "stable_destination": str(item.destination),
                "restore_destination": str(item.source),
                "source_exists": exists,
                "source_bytes": source_bytes,
                "source_files": source_files,
            }
        )
        if profile == "analytics":
            analytics_bytes += source_bytes
            analytics_files += source_files
        else:
            live_bytes += source_bytes
            live_files += source_files

    manifest_path = root / "stable-layer-manifest.json"
    manifest: dict[str, object] | None = None
    manifest_error: str | None = None
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            manifest_error = f"{type(exc).__name__}: {exc}"

    item_by_label = {str(item["label"]): item for item in item_payloads}
    covered_labels: set[str] = set()
    if manifest and manifest.get("status") == "verified":
        manifest_items = manifest.get("items")
        if isinstance(manifest_items, list):
            for raw_item in manifest_items:
                if not isinstance(raw_item, dict):
                    continue
                label = raw_item.get("label")
                if not isinstance(label, str) or label not in item_by_label:
                    continue
                current_item = item_by_label[label]
                if raw_item.get("source_exists") is False and current_item["source_exists"] is False:
                    covered_labels.add(label)
                    continue
                if raw_item.get("coverage_status") != "verified" or current_item["source_exists"] is not True:
                    continue
                if raw_item.get("source_bytes") != current_item["source_bytes"] or raw_item.get(
                    "source_files"
                ) != current_item["source_files"]:
                    continue
                coverage_status, _, _, _ = _source_coverage(
                    Path(str(current_item["source"])),
                    Path(str(current_item["stable_destination"])),
                )
                if coverage_status == "verified":
                    covered_labels.add(label)

    existing_analytics = {
        str(item["label"])
        for item in item_payloads
        if item["profile"] == "analytics" and item["source_exists"] is True
    }
    existing_live = {
        str(item["label"])
        for item in item_payloads
        if item["profile"] == "migration" and item["source_exists"] is True
    }
    analytics_ready = existing_analytics.issubset(covered_labels)
    full_fidelity_ready = analytics_ready and existing_live.issubset(covered_labels) and bool(existing_live)
    blockers: list[str] = []
    if not manifest_path.is_file():
        blockers.append("stable_manifest_missing")
    elif manifest_error:
        blockers.append("stable_manifest_unreadable")
    elif not analytics_ready:
        blockers.append("analytics_profile_not_verified")
    optional_migration_blockers = list(blockers)
    if not full_fidelity_ready:
        optional_migration_blockers.append("live_sessions_not_verified")

    home = config.paths.home.expanduser()
    return {
        "schema_version": 1,
        "stable_root": str(root),
        "profiles": {
            "analytics": {
                "purpose": "Tokscale continuity and rebuildable imported views",
                "source_bytes": analytics_bytes,
                "source_files": analytics_files,
            },
            "migration": {
                "purpose": "Optional full-fidelity client session history in addition to the default analytics state",
                "additional_source_bytes": live_bytes,
                "additional_source_files": live_files,
                "requires_clients_stopped": True,
            },
        },
        "items": item_payloads,
        "current_mirror": {
            "manifest_path": str(manifest_path),
            "manifest_readable": manifest is not None,
            "manifest_error": manifest_error,
            "profile": manifest.get("profile") if manifest else None,
            "status": manifest.get("status") if manifest else None,
            "mirrored_at": manifest.get("mirrored_at") if manifest else None,
        },
        "readiness": {
            "analytics_restore_ready": analytics_ready,
            "full_fidelity_restore_ready": full_fidelity_ready,
            "blockers": blockers,
            "optional_migration_blockers": optional_migration_blockers,
        },
        "rebuildable_or_discardable": [
            {"label": "projection_home", "path": str(config.paths.projection_home)},
            {"label": "canonical_shadow_home", "path": str(config.paths.shadow_home)},
            {"label": "tokscale_cache", "path": str(home / ".config" / "tokscale" / "cache")},
            {"label": "relay_transport", "path": str(config.paths.relay_root)},
        ],
        "excluded_sensitive": [
            {
                "label": "codex_credentials",
                "path": str(home / ".codex" / "auth.json"),
                "reason": "reauthenticate_on_new_machine",
            },
            {
                "label": "gemini_credentials",
                "path": str(home / ".gemini" / "oauth_creds.json"),
                "reason": "reauthenticate_on_new_machine",
            },
            {
                "label": "tokscale_credentials",
                "path": str(home / ".config" / "tokscale" / "credentials.json"),
                "reason": "reauthenticate_on_new_machine",
            },
        ],
    }

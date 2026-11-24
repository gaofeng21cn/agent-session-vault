"""Restic-backed stable analytics and optional migration mirror."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from .config import VaultConfig
from .restic import ResticError, ResticRepository, default_password_file, has_password_env


STABLE_ARCHIVE_FORMAT = "restic-v2"
LEGACY_STABLE_ARCHIVE_FORMAT = "tar-zstd-shards-v1"


@dataclass(frozen=True)
class PackedSourceSnapshot:
    entries: dict[str, tuple[int, int]]
    is_file: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(size for size, _ in self.entries.values())

    @property
    def total_files(self) -> int:
        return len(self.entries)

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(b"file\0" if self.is_file else b"directory\0")
        for relative, (size, mtime_ns) in sorted(self.entries.items()):
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(str(size).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(mtime_ns).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()


@dataclass(frozen=True)
class StableMirrorItem:
    label: str
    kind: str
    role: str
    source: Path
    destination: Path
    restore_relative_path: Path


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
    restore_relative_path: Path
    archive_format: str | None = None
    restic_snapshot_id: str | None = None
    archive_bytes: int = 0


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
    restic_password_file: Path | None = None


def default_stable_root(config: VaultConfig) -> Path:
    return config.paths.stable_root.expanduser()


def stable_repo_path(stable_root: Path) -> Path:
    return stable_root.expanduser() / "repo"


def _stable_repo(
    stable_root: Path,
    *,
    password_file: Path | None = None,
    cache_dir: Path | None = None,
) -> ResticRepository:
    return ResticRepository(
        stable_repo_path(stable_root),
        password_file=password_file,
        cache_dir=cache_dir,
    )


def snapshot_directory(source: Path) -> PackedSourceSnapshot:
    if source.is_file():
        stat = source.stat()
        return PackedSourceSnapshot(entries={"": (stat.st_size, stat.st_mtime_ns)}, is_file=True)
    entries: dict[str, tuple[int, int]] = {}
    for child in source.rglob("*"):
        if child.is_file() and not child.is_symlink():
            stat = child.stat()
            entries[child.relative_to(source).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return PackedSourceSnapshot(entries=entries, is_file=False)


def stable_mirror_items(
    config: VaultConfig,
    stable_root: Path | None = None,
    *,
    include_live_sessions: bool = False,
) -> list[StableMirrorItem]:
    root = (stable_root or default_stable_root(config)).expanduser()
    repo = stable_repo_path(root)
    items = [
        StableMirrorItem(
            label="imports",
            kind="directory",
            role="durable_analytics_projection",
            source=config.paths.import_root.expanduser(),
            destination=repo,
            restore_relative_path=Path("tokscale") / "imports",
        ),
        StableMirrorItem(
            label="local_workspace_extras",
            kind="directory",
            role="durable_local_ingest",
            source=config.paths.local_workspace_extras.expanduser(),
            destination=repo,
            restore_relative_path=Path("tokscale") / "local-workspace-extras",
        ),
        StableMirrorItem(
            label="config",
            kind="file",
            role="control_plane",
            source=config.config_path.expanduser(),
            destination=repo,
            restore_relative_path=Path("config") / "config.toml",
        ),
        StableMirrorItem(
            label="tokscale_custom_pricing",
            kind="file",
            role="control_plane",
            source=config.paths.projection_home.expanduser() / ".config" / "tokscale" / "custom-pricing.json",
            destination=repo,
            restore_relative_path=(
                Path("tokscale") / "projection-home" / ".config" / "tokscale" / "custom-pricing.json"
            ),
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
                destination=repo,
                restore_relative_path=Path("live") / "codex" / "sessions",
            ),
            StableMirrorItem(
                label="live_codex_archived_sessions",
                kind="directory",
                role="authoritative_live_session",
                source=home / ".codex" / "archived_sessions",
                destination=repo,
                restore_relative_path=Path("live") / "codex" / "archived_sessions",
            ),
            StableMirrorItem(
                label="live_codex_session_index",
                kind="file",
                role="live_session_index",
                source=home / ".codex" / "session_index.jsonl",
                destination=repo,
                restore_relative_path=Path("live") / "codex" / "session_index.jsonl",
            ),
            StableMirrorItem(
                label="live_codex_history",
                kind="file",
                role="live_session_index",
                source=home / ".codex" / "history.jsonl",
                destination=repo,
                restore_relative_path=Path("live") / "codex" / "history.jsonl",
            ),
            StableMirrorItem(
                label="live_gemini_chats",
                kind="directory",
                role="authoritative_live_session",
                source=home / ".gemini" / "tmp",
                destination=repo,
                restore_relative_path=Path("live") / "gemini" / "tmp",
            ),
            StableMirrorItem(
                label="live_openclaw_agents",
                kind="directory",
                role="authoritative_live_session",
                source=home / ".openclaw" / "agents",
                destination=repo,
                restore_relative_path=Path("live") / "openclaw" / "agents",
            ),
        ]
    )
    return items


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


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


def _existing_snapshot_ids(repo: ResticRepository) -> set[str]:
    if not repo.exists():
        return set()
    try:
        return {item.id for item in repo.snapshots(tags=("vault:stable",))}
    except ResticError:
        return set()


def mirror_stable_layer(
    config: VaultConfig,
    *,
    stable_root: Path | None = None,
    dry_run: bool = False,
    include_live_sessions: bool = False,
) -> StableMirrorResult:
    root = (stable_root or default_stable_root(config)).expanduser()
    mirrored_at = datetime.now(UTC).isoformat()
    profile = "migration" if include_live_sessions else "analytics"
    password_file = config.archive.password_file.expanduser()
    cache_dir = config.archive.staging_root.expanduser() / ".restic-cache"
    repo = _stable_repo(root, password_file=password_file, cache_dir=cache_dir)

    previous_items = _load_verified_manifest_items(root / "stable-layer-manifest.json")
    existing_snaps = _existing_snapshot_ids(repo)
    mirror_items = stable_mirror_items(config, root, include_live_sessions=include_live_sessions)
    results: list[StableMirrorItemResult] = []
    failed = False

    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        try:
            repo.init()
        except (OSError, ResticError) as exc:
            failed = True
            for item in mirror_items:
                results.append(
                    StableMirrorItemResult(
                        label=item.label,
                        kind=item.kind,
                        role=item.role,
                        source=item.source,
                        destination=item.destination,
                        source_exists=item.source.exists(),
                        source_bytes=0,
                        source_files=0,
                        status="failed",
                        duration_seconds=0.0,
                        command=None,
                        exit_code=1,
                        stderr=f"restic init failed: {type(exc).__name__}: {exc}",
                        coverage_status="failed",
                        verified_files=0,
                        missing_files=0,
                        mismatched_files=0,
                        source_manifest_fingerprint=None,
                        transfer_status="failed",
                        restore_relative_path=item.restore_relative_path,
                        archive_format=STABLE_ARCHIVE_FORMAT,
                    )
                )
            mirror_items = []

    for item in mirror_items:
        started = time.monotonic()
        source = item.source.expanduser()
        destination = item.destination.expanduser()
        if not source.exists():
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
                    restore_relative_path=item.restore_relative_path,
                )
            )
            continue

        snapshot = snapshot_directory(source)
        fingerprint = snapshot.fingerprint
        previous = previous_items.get(item.label)
        prev_snap_id = previous.get("restic_snapshot_id") if isinstance(previous, dict) else None
        reusable = bool(
            not dry_run
            and previous
            and previous.get("archive_format") == STABLE_ARCHIVE_FORMAT
            and previous.get("source") == str(source)
            and previous.get("source_manifest_fingerprint") == fingerprint
            and (
                (snapshot.total_files == 0 and prev_snap_id is None)
                or (isinstance(prev_snap_id, str) and prev_snap_id in existing_snaps)
            )
        )

        if dry_run:
            results.append(
                StableMirrorItemResult(
                    label=item.label,
                    kind=item.kind,
                    role=item.role,
                    source=source,
                    destination=destination,
                    source_exists=True,
                    source_bytes=snapshot.total_bytes,
                    source_files=snapshot.total_files,
                    status="planned",
                    duration_seconds=round(time.monotonic() - started, 3),
                    command=None,
                    exit_code=None,
                    stderr=None,
                    coverage_status="planned",
                    verified_files=0,
                    missing_files=0,
                    mismatched_files=0,
                    source_manifest_fingerprint=fingerprint,
                    transfer_status="planned",
                    restore_relative_path=item.restore_relative_path,
                    archive_format=STABLE_ARCHIVE_FORMAT,
                )
            )
            continue

        if reusable:
            results.append(
                StableMirrorItemResult(
                    label=item.label,
                    kind=item.kind,
                    role=item.role,
                    source=source,
                    destination=destination,
                    source_exists=True,
                    source_bytes=snapshot.total_bytes,
                    source_files=snapshot.total_files,
                    status="mirrored",
                    duration_seconds=round(time.monotonic() - started, 3),
                    command=None,
                    exit_code=0,
                    stderr=None,
                    coverage_status="verified",
                    verified_files=snapshot.total_files,
                    missing_files=0,
                    mismatched_files=0,
                    source_manifest_fingerprint=fingerprint,
                    transfer_status="reused_verified",
                    restore_relative_path=item.restore_relative_path,
                    archive_format=STABLE_ARCHIVE_FORMAT,
                    restic_snapshot_id=str(prev_snap_id) if isinstance(prev_snap_id, str) else None,
                    archive_bytes=int(previous.get("archive_bytes", 0) or 0) if isinstance(previous, dict) else 0,
                )
            )
            continue

        restic_snapshot_id: str | None = None
        archive_bytes = 0
        stderr: str | None = None
        exit_code = 0
        status = "mirrored"
        coverage_status = "verified"
        transfer_status = "backed_up"
        verified_files = snapshot.total_files

        try:
            if snapshot.total_files > 0:
                if item.kind == "file":
                    cwd = source.parent
                    relatives = [source.name]
                else:
                    cwd = source
                    relatives = sorted(snapshot.entries.keys())
                parent = str(prev_snap_id) if isinstance(prev_snap_id, str) and prev_snap_id in existing_snaps else None
                summary = repo.backup(
                    cwd,
                    relatives,
                    hostname="stable",
                    tags=("vault:stable", f"profile:{profile}", f"label:{item.label}"),
                    parent=parent,
                )
                restic_snapshot_id = summary.snapshot_id
                archive_bytes = summary.data_added_packed
                existing_snaps.add(restic_snapshot_id)
            else:
                transfer_status = "empty_directory"

            after = snapshot_directory(source)
            if after != snapshot:
                status = "verification_failed"
                coverage_status = "failed"
                exit_code = 1
                verified_files = 0
                stderr = "source changed during stable mirror; retry after writers are quiescent"
        except (OSError, ResticError, ValueError) as exc:
            status = "failed"
            coverage_status = "failed"
            transfer_status = "failed"
            exit_code = 1
            verified_files = 0
            stderr = f"restic stable mirror failed: {type(exc).__name__}: {exc}"

        results.append(
            StableMirrorItemResult(
                label=item.label,
                kind=item.kind,
                role=item.role,
                source=source,
                destination=destination,
                source_exists=True,
                source_bytes=snapshot.total_bytes,
                source_files=snapshot.total_files,
                status=status,
                duration_seconds=round(time.monotonic() - started, 3),
                command=None,
                exit_code=exit_code,
                stderr=stderr,
                coverage_status=coverage_status,
                verified_files=verified_files,
                missing_files=0,
                mismatched_files=0,
                source_manifest_fingerprint=fingerprint,
                transfer_status=transfer_status,
                restore_relative_path=item.restore_relative_path,
                archive_format=STABLE_ARCHIVE_FORMAT,
                restic_snapshot_id=restic_snapshot_id,
                archive_bytes=archive_bytes,
            )
        )
        if status in {"failed", "verification_failed"}:
            failed = True
            break

    status = "planned" if dry_run else "failed" if failed else "verified"
    manifest_path = None if dry_run or failed else root / "stable-layer-manifest.json"
    attempt_path = None if dry_run else root / "stable-layer-attempt.json"
    recorded_pw = None if has_password_env() else password_file
    result = StableMirrorResult(
        stable_root=root,
        manifest_path=manifest_path,
        attempt_path=attempt_path,
        dry_run=dry_run,
        profile=profile,
        status=status,
        mirrored_at=mirrored_at,
        items=results,
        restic_password_file=recorded_pw,
    )
    if attempt_path is not None:
        _write_json_atomic(attempt_path, stable_mirror_payload(result))
    if manifest_path is not None:
        _write_json_atomic(manifest_path, stable_mirror_payload(result))
    return result


def stable_mirror_payload(result: StableMirrorResult) -> dict[str, object]:
    return {
        "schema_version": 6,
        "stable_root": str(result.stable_root),
        "restic_repo": str(stable_repo_path(result.stable_root)),
        "restic_password_file": str(result.restic_password_file) if result.restic_password_file else None,
        "manifest_path": str(result.manifest_path) if result.manifest_path else None,
        "attempt_path": str(result.attempt_path) if result.attempt_path else None,
        "dry_run": result.dry_run,
        "profile": result.profile,
        "status": result.status,
        "mirror_semantics": "restic-source-covered",
        "mirrored_at": result.mirrored_at,
        "items": [
            {
                "label": item.label,
                "kind": item.kind,
                "role": item.role,
                "source": str(item.source),
                "destination": str(item.destination),
                "stable_relative_path": item.destination.relative_to(result.stable_root).as_posix(),
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
                "restore_relative_path": item.restore_relative_path.as_posix(),
                "archive_format": item.archive_format,
                "restic_snapshot_id": item.restic_snapshot_id,
                "archive_bytes": item.archive_bytes,
            }
            for item in result.items
        ],
    }


def _resolve_restore_password_file(manifest: dict[str, object], password_file: Path | None) -> Path | None:
    if has_password_env():
        return None
    if password_file is not None:
        return password_file.expanduser()
    recorded = manifest.get("restic_password_file")
    if isinstance(recorded, str) and recorded:
        candidate = Path(recorded).expanduser()
        if candidate.is_file():
            return candidate
    return default_password_file(Path.home())


def restore_stable_layer(
    stable_root: Path,
    destination_root: Path,
    *,
    labels: set[str] | None = None,
    password_file: Path | None = None,
) -> dict[str, object]:
    root = stable_root.expanduser()
    destination = destination_root.expanduser()
    manifest_path = root / "stable-layer-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"stable manifest is missing or unreadable: {manifest_path}") from exc
    if not isinstance(manifest, dict) or manifest.get("status") != "verified":
        raise ValueError(f"stable manifest is not verified: {manifest_path}")
    raw_items = manifest.get("items")
    if not isinstance(raw_items, list):
        raise ValueError(f"stable manifest items are missing: {manifest_path}")

    repo = _stable_repo(root, password_file=_resolve_restore_password_file(manifest, password_file))
    restored_items: list[dict[str, object]] = []
    destination.mkdir(parents=True, exist_ok=True)

    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or raw_item.get("source_exists") is not True:
            continue
        label = raw_item.get("label")
        if not isinstance(label, str) or (labels is not None and label not in labels):
            continue
        archive_format = raw_item.get("archive_format")
        if archive_format == LEGACY_STABLE_ARCHIVE_FORMAT:
            raise ValueError(
                f"legacy tar-zstd-shards-v1 stable item cannot be restored by restic backend: {label}; "
                "re-mirror stable analytics with restic or use the previous release to read legacy shards"
            )
        if archive_format != STABLE_ARCHIVE_FORMAT:
            raise ValueError(f"unsupported stable restore format for {label}: {archive_format!r}")

        relative_text = raw_item.get("restore_relative_path")
        if not isinstance(relative_text, str):
            raise ValueError(f"stable item has no restore path: {label}")
        relative_path = Path(relative_text)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"unsafe stable restore path for {label}: {relative_text}")
        restore_path = destination / relative_path
        if restore_path.exists() or restore_path.is_symlink():
            raise FileExistsError(f"stable restore destination already exists: {restore_path}")

        kind = raw_item.get("kind")
        snap_id = raw_item.get("restic_snapshot_id")
        expected_files = int(raw_item.get("source_files", 0) or 0)
        expected_bytes = int(raw_item.get("source_bytes", 0) or 0)

        if expected_files == 0 and kind == "directory":
            restore_path.mkdir(parents=True, exist_ok=True)
            restored_files, restored_bytes = 0, 0
        else:
            if not isinstance(snap_id, str) or not snap_id:
                raise ValueError(f"stable item has no restic snapshot id: {label}")
            if kind == "directory":
                repo.restore(snap_id, restore_path)
                restored_snapshot = snapshot_directory(restore_path)
                restored_files, restored_bytes = restored_snapshot.total_files, restored_snapshot.total_bytes
            elif kind == "file":
                source_name = Path(str(raw_item.get("source", restore_path.name))).name
                with tempfile.TemporaryDirectory(prefix="asv-stable-restore-") as staging_dir:
                    staging_path = Path(staging_dir)
                    repo.restore(snap_id, staging_path)
                    extracted = staging_path / source_name
                    if not extracted.is_file():
                        raise ValueError(f"restored stable file missing from snapshot: {source_name}")
                    restore_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(extracted, restore_path)
                restored_files = 1
                restored_bytes = restore_path.stat().st_size
            else:
                raise ValueError(f"unsupported stable restore item kind: {label}")

        if restored_files != expected_files or restored_bytes != expected_bytes:
            raise ValueError(
                f"restored stable coverage mismatch for {label}: "
                f"expected ({expected_files} files, {expected_bytes} bytes), "
                f"got ({restored_files} files, {restored_bytes} bytes)"
            )
        restored_items.append(
            {
                "label": label,
                "source": str(stable_repo_path(root)),
                "restic_snapshot_id": snap_id,
                "destination": str(restore_path),
                "restored_files": restored_files,
                "restored_bytes": restored_bytes,
                "status": "verified",
            }
        )

    expected_labels = {
        str(item.get("label"))
        for item in raw_items
        if isinstance(item, dict)
        and item.get("source_exists") is True
        and (labels is None or item.get("label") in labels)
    }
    restored_labels = {str(item["label"]) for item in restored_items}
    if expected_labels != restored_labels:
        missing = ",".join(sorted(expected_labels - restored_labels))
        raise ValueError(f"stable restore did not cover requested items: {missing}")

    return {
        "schema_version": 2,
        "status": "verified",
        "stable_root": str(root),
        "destination_root": str(destination),
        "manifest_path": str(manifest_path),
        "items": restored_items,
        "restored_files": sum(int(item["restored_files"]) for item in restored_items),
        "restored_bytes": sum(int(item["restored_bytes"]) for item in restored_items),
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
        snapshot = snapshot_directory(item.source) if exists else None
        source_bytes = snapshot.total_bytes if snapshot else 0
        source_files = snapshot.total_files if snapshot else 0
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
                "restore_relative_path": item.restore_relative_path.as_posix(),
                "source_exists": exists,
                "source_bytes": source_bytes,
                "source_files": source_files,
                "source_manifest_fingerprint": snapshot.fingerprint if snapshot else None,
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

    repo = _stable_repo(
        root,
        password_file=config.archive.password_file,
        cache_dir=config.archive.staging_root / ".restic-cache",
    )
    existing_snaps = _existing_snapshot_ids(repo) if manifest and manifest.get("status") == "verified" else set()

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
                if raw_item.get("archive_format") != STABLE_ARCHIVE_FORMAT:
                    continue
                if raw_item.get("source_manifest_fingerprint") != current_item["source_manifest_fingerprint"]:
                    continue
                snap_id = raw_item.get("restic_snapshot_id")
                if current_item["source_files"] == 0 and snap_id is None:
                    covered_labels.add(label)
                elif isinstance(snap_id, str) and snap_id in existing_snaps:
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
            {"label": "tokscale_cache", "path": str(home / ".config" / "tokscale" / "cache")},
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

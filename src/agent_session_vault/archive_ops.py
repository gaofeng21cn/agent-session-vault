from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from .archive import sha256_file
from .archive_backend import (
    PublishedSnapshot,
    ResticArchiveBackend,
    _manifest_from_payload,
    _member_from_record,
    _snapshot_from_payload,
)
from .archive_catalog import write_catalog_segment
from .archive_model import (
    ArchiveManifest,
    ArchiveReceipt,
    ArchiveSnapshot,
    utc_now,
)
from .archive_sources import ArchiveScanResult, scan_codex_sources
from .config import VaultConfig


@dataclass(frozen=True)
class SnapshotBuildResult:
    machine_id: str
    cycle_id: str
    staging_root: Path
    snapshots: tuple[ArchiveSnapshot, ...]
    manifests: tuple[ArchiveManifest, ...]
    scan: ArchiveScanResult
    dedup: dict[str, int] = field(default_factory=dict)

    def payload(self) -> dict[str, object]:
        return {
            "status": "staged",
            "machine_id": self.machine_id,
            "cycle_id": self.cycle_id,
            "staging_root": str(self.staging_root),
            "snapshots": [
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "source_id": snapshot.source.source_id,
                    "status": snapshot.status,
                    "consistency": snapshot.consistency,
                    "file_count": len(next(manifest.files for manifest in self.manifests if manifest.snapshot_id == snapshot.snapshot_id)),
                    "manifest_sha256": next(manifest.sha256 for manifest in self.manifests if manifest.snapshot_id == snapshot.snapshot_id),
                    "warnings": list(snapshot.warnings),
                }
                for snapshot in self.snapshots
            ],
            "missing_sources": list(self.scan.missing_sources),
            "dedup": self.dedup,
        }


@dataclass(frozen=True)
class ArchiveCycleResult:
    status: str
    machine_id: str
    cycle_id: str | None
    snapshot_ids: tuple[str, ...]
    verifications: tuple[dict[str, object], ...]
    reason: str | None = None
    staging_cleanup: str | None = None
    staging_root: str | None = None
    error: str | None = None
    receipt_path: str | None = None
    dedup: dict[str, int] | None = None

    def payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "machine_id": self.machine_id,
            "cycle_id": self.cycle_id,
            "snapshot_ids": list(self.snapshot_ids),
            "verifications": list(self.verifications),
            "reason": self.reason,
            "staging_cleanup": self.staging_cleanup,
            "staging_root": self.staging_root,
            "error": self.error,
            "receipt_path": self.receipt_path,
            "dedup": self.dedup,
        }


def archive_backend(config: VaultConfig) -> ResticArchiveBackend:
    return ResticArchiveBackend(
        config.archive.root,
        password_file=config.archive.password_file,
        cache_dir=config.archive.staging_root / ".restic-cache",
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _snapshot_id(source_id: str) -> str:
    return f"snap-{source_id.removeprefix('source-')}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"


def _cycle_id() -> str:
    return f"cycle-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"


def _committed_history(
    backend: ResticArchiveBackend,
) -> tuple[set[tuple[int, str]], dict[tuple[str, str], PublishedSnapshot]]:
    """Read authenticated metadata of committed snapshots for parent and dedup accounting."""
    known_contents: set[tuple[int, str]] = set()
    latest: dict[tuple[str, str], PublishedSnapshot] = {}
    for snapshot_dir in backend.iter_snapshot_dirs():
        published = backend.load_verified_metadata(snapshot_dir)
        for manifest in published.manifests:
            for record in manifest.files:
                known_contents.add((record.bytes, record.sha256))
            key = (published.snapshot.machine_id, manifest.source.source_id)
            previous = latest.get(key)
            if previous is None or (published.snapshot.captured_at, published.snapshot.snapshot_id) > (
                previous.snapshot.captured_at,
                previous.snapshot.snapshot_id,
            ):
                latest[key] = published
    return known_contents, latest


def _latest_published_manifest(
    backend: ResticArchiveBackend,
    machine_id: str,
    source_id: str,
) -> tuple[ArchiveSnapshot, ArchiveManifest] | None:
    candidates: list[tuple[str, ArchiveSnapshot, ArchiveManifest]] = []
    for snapshot_dir in backend.iter_snapshot_dirs(machine_id):
        try:
            published = backend.load_snapshot(snapshot_dir)
        except Exception:
            continue
        for manifest in published.manifests:
            if manifest.source.source_id == source_id:
                candidates.append((published.snapshot.captured_at, published.snapshot, manifest))
    if not candidates:
        return None
    _, snapshot, manifest = max(candidates, key=lambda item: item[0])
    return snapshot, manifest


def _compute_dedup_counters(
    manifests: list[ArchiveManifest],
    historical_contents: set[tuple[int, str]],
    *,
    restic_data_blobs: int | None = None,
    restic_tree_blobs: int = 0,
    restic_data_added: int = 0,
    restic_data_added_packed: int = 0,
) -> dict[str, int]:
    all_files = [record for manifest in manifests for record in manifest.files]
    seen_in_cycle: set[tuple[int, str]] = set()
    new_content_files = 0
    new_content_bytes = 0
    historical_reused = 0
    same_cycle_reused = 0
    reused_bytes = 0
    for record in all_files:
        key = (record.bytes, record.sha256)
        if key in historical_contents:
            historical_reused += 1
            reused_bytes += record.bytes
        elif key in seen_in_cycle:
            same_cycle_reused += 1
            reused_bytes += record.bytes
        else:
            seen_in_cycle.add(key)
            new_content_files += 1
            new_content_bytes += record.bytes

    reused_files = historical_reused + same_cycle_reused
    if new_content_files == 0:
        new_objects = 0
        new_archive_bytes = 0
    elif restic_data_blobs is None:
        new_objects = new_content_files
        new_archive_bytes = max(new_content_bytes, 1)
    else:
        new_objects = max(restic_data_blobs, 1)
        new_archive_bytes = max(restic_data_added_packed, 1)

    counters: dict[str, int] = {
        "files_seen": len(all_files),
        "file_logical_bytes": sum(record.bytes for record in all_files),
        "new_content_files": new_content_files,
        "new_content_bytes": new_content_bytes,
        "new_objects": new_objects,
        "new_archive_bytes": new_archive_bytes,
        "reused_files": reused_files,
        "reused_logical_bytes": reused_bytes,
        "historical_reused_files": historical_reused,
        "same_cycle_reused_files": same_cycle_reused,
    }
    if restic_data_blobs is not None:
        counters.update(
            {
                "restic_data_blobs": restic_data_blobs,
                "restic_tree_blobs": restic_tree_blobs,
                "restic_data_added": restic_data_added,
                "restic_data_added_packed": restic_data_added_packed,
            }
        )
    return counters


def _materialize_staged_payload(
    capture_root: Path,
    payload_root: Path,
    members: list[str],
) -> None:
    payload_root.mkdir(parents=True, exist_ok=True)
    for member in members:
        source_file = capture_root / Path(member)
        destination_file = payload_root / Path(member)
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination_file)


def build_snapshot(
    config: VaultConfig,
    *,
    machine_id: str | None = None,
    cycle_id: str | None = None,
    staging_root: Path | None = None,
) -> SnapshotBuildResult:
    backend = archive_backend(config)
    backend.ensure_writable()
    backend.ensure_ready()
    historical_contents, latest_by_source = _committed_history(backend)

    cycle = cycle_id or _cycle_id()
    cycle_staging_root = (staging_root or config.archive.staging_root).expanduser() / cycle
    if cycle_staging_root.exists():
        raise FileExistsError(f"archive cycle staging already exists: {cycle_staging_root}")
    cycle_staging_root.mkdir(parents=True, exist_ok=False)

    snapshots: list[ArchiveSnapshot] = []
    manifests: list[ArchiveManifest] = []
    with tempfile.TemporaryDirectory(prefix="asv-capture-") as temp_capture:
        scan = scan_codex_sources(config, machine_id=machine_id, capture_root=Path(temp_capture))

        for scanned in scan.sources:
            previous_published = latest_by_source.get((scan.machine_id, scanned.source.source_id))
            previous = (
                (
                    previous_published.snapshot,
                    next(m for m in previous_published.manifests if m.source.source_id == scanned.source.source_id),
                )
                if previous_published
                else None
            )
            snapshot_id = _snapshot_id(scanned.source.source_id)
            source_dir = cycle_staging_root / scanned.source.source_id
            source_dir.mkdir(parents=True, exist_ok=True)
            records = tuple(
                sorted(
                    (
                        replace(
                            item.record,
                            member=item.record.member or _member_from_record(scanned.source, item.record),
                        )
                        for item in scanned.files
                    ),
                    key=lambda item: item.path,
                )
            )
            members = [record.member for record in records if record.member]
            if scanned.capture_root is not None:
                payload_root = source_dir / "payload"
                _materialize_staged_payload(scanned.capture_root, payload_root, members)
            else:
                payload_root = Path(scanned.source.root_path)

            warnings = tuple(sorted(set(scanned.warnings)))
            consistency = "quiesced" if not warnings else "best_effort"
            snapshot = ArchiveSnapshot(
                snapshot_id=snapshot_id,
                cycle_id=cycle,
                machine_id=scan.machine_id,
                source=scanned.source,
                captured_at=utc_now(),
                consistency=consistency,
                status="staged",
                parent_snapshot_id=previous[0].snapshot_id if previous else None,
                warnings=warnings,
            )
            manifest = ArchiveManifest(
                snapshot_id=snapshot_id,
                source=scanned.source,
                files=records,
                deleted_paths=(
                    tuple(sorted({record.path for record in previous[1].files} - {record.path for record in records}))
                    if previous
                    else ()
                ),
            )
            _write_json(source_dir / "snapshot.json", snapshot.to_payload())
            _write_json(source_dir / "manifest.json", manifest.to_payload())
            _write_json(
                source_dir / "stage.json",
                {
                    "payload_root": str(payload_root),
                    "parent_restic_snapshot_id": previous[0].restic_snapshot_id if previous else None,
                    "members": members,
                },
            )
            snapshots.append(snapshot)
            manifests.append(manifest)

    dedup = _compute_dedup_counters(manifests, historical_contents)
    _write_json(
        cycle_staging_root / "cycle.json",
        {
            "schema_version": "archive.cycle.v2",
            "cycle_id": cycle,
            "machine_id": scan.machine_id,
            "snapshot_ids": [snapshot.snapshot_id for snapshot in snapshots],
            "missing_sources": list(scan.missing_sources),
        },
    )
    _write_json(cycle_staging_root / "dedup.json", dedup)
    return SnapshotBuildResult(
        machine_id=scan.machine_id,
        cycle_id=cycle,
        staging_root=cycle_staging_root,
        snapshots=tuple(snapshots),
        manifests=tuple(manifests),
        scan=scan,
        dedup=dedup,
    )


def _verify_staged_files(manifest: ArchiveManifest, payload_root: Path) -> None:
    for record in manifest.files:
        member = record.member or _member_from_record(manifest.source, record)
        candidate = payload_root / Path(member)
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError(f"staged file is missing or not a regular file: {member}")
        if candidate.stat().st_size != record.bytes or sha256_file(candidate) != record.sha256:
            raise ValueError(f"staged file checksum mismatch: {member}")


def publish_snapshot(
    config: VaultConfig,
    staging_root: Path,
    *,
    verify_staged: bool = False,
) -> list[PublishedSnapshot]:
    backend = archive_backend(config)
    backend.ensure_writable()
    backend.ensure_ready()
    historical_contents, _ = _committed_history(backend)

    prepared: list[tuple[ArchiveSnapshot, ArchiveManifest, Path, list[str], str | None]] = []
    for source_dir in sorted(path for path in staging_root.iterdir() if path.is_dir() and not path.name.startswith("_")):
        snapshot_path = source_dir / "snapshot.json"
        manifest_path = source_dir / "manifest.json"
        stage_path = source_dir / "stage.json"
        if not snapshot_path.is_file() or not manifest_path.is_file():
            continue
        snapshot = _snapshot_from_payload(json.loads(snapshot_path.read_text(encoding="utf-8")))
        if snapshot.status != "staged":
            raise ValueError(f"cannot publish an unstable snapshot: {snapshot.snapshot_id}")
        manifest = _manifest_from_payload(json.loads(manifest_path.read_text(encoding="utf-8")))
        stage_meta = json.loads(stage_path.read_text(encoding="utf-8")) if stage_path.is_file() else {}
        payload_root = Path(str(stage_meta.get("payload_root") or manifest.source.root_path))
        members = [
            record.member or _member_from_record(manifest.source, record)
            for record in manifest.files
        ]
        parent_restic = stage_meta.get("parent_restic_snapshot_id")
        if verify_staged:
            _verify_staged_files(manifest, payload_root)
        prepared.append(
            (
                snapshot,
                manifest,
                payload_root,
                members,
                str(parent_restic) if isinstance(parent_restic, str) and parent_restic else None,
            )
        )

    result: list[PublishedSnapshot] = []
    published_manifests: list[ArchiveManifest] = []
    total_data_blobs = 0
    total_tree_blobs = 0
    total_data_added = 0
    total_data_added_packed = 0

    for snapshot, manifest, payload_root, members, parent_restic in prepared:
        restic_snapshot_id: str | None = None
        if members:
            tags = (
                "vault:archive",
                f"client:{manifest.source.client}",
                f"machine:{snapshot.machine_id}",
                f"source:{manifest.source.source_id}",
                f"snapshot:{snapshot.snapshot_id}",
                f"cycle:{snapshot.cycle_id}",
            )
            summary = backend.backup_source(
                payload_root,
                members,
                hostname=snapshot.machine_id,
                tags=tags,
                parent=parent_restic,
            )
            restic_snapshot_id = summary.snapshot_id
            total_data_blobs += summary.data_blobs
            total_tree_blobs += summary.tree_blobs
            total_data_added += summary.data_added
            total_data_added_packed += summary.data_added_packed

        bound_snapshot = replace(snapshot, restic_snapshot_id=restic_snapshot_id)
        bound_manifest = replace(manifest, restic_snapshot_id=restic_snapshot_id)
        published = backend.publish_snapshot(bound_snapshot, (bound_manifest,))
        write_catalog_segment(backend, published)
        result.append(published)
        published_manifests.append(bound_manifest)

    dedup = _compute_dedup_counters(
        published_manifests,
        historical_contents,
        restic_data_blobs=total_data_blobs,
        restic_tree_blobs=total_tree_blobs,
        restic_data_added=total_data_added,
        restic_data_added_packed=total_data_added_packed,
    )
    _write_json(staging_root / "dedup.json", dedup)
    return result


def verify_snapshot(
    config: VaultConfig,
    snapshot_id: str,
    *,
    deep: bool = False,
    run_repo_check: bool = True,
) -> dict[str, object]:
    backend = archive_backend(config)
    for snapshot_dir in backend.iter_snapshot_dirs():
        if snapshot_dir.name == snapshot_id:
            return backend.verify_snapshot(snapshot_dir, deep=deep, run_repo_check=run_repo_check)
    raise FileNotFoundError(f"snapshot not found: {snapshot_id}")


def init_backend(config: VaultConfig) -> dict[str, object]:
    return archive_backend(config).initialize()


def receipt(
    config: VaultConfig,
    operation: str,
    status: str,
    *,
    machine_id: str,
    source_id: str | None = None,
    snapshot_id: str | None = None,
    details: dict[str, object] | None = None,
) -> ArchiveReceipt:
    archive_backend(config).ensure_writable()
    now = utc_now()
    item = ArchiveReceipt(
        operation_id=f"op-{uuid.uuid4().hex}",
        operation=operation,
        status=status,
        started_at=now,
        finished_at=now,
        machine_id=machine_id,
        source_id=source_id,
        snapshot_id=snapshot_id,
        details=details or {},
    )
    root = archive_backend(config).root / "receipts" / machine_id
    _write_json(root / f"{item.operation_id}.json", item.to_payload())
    return item


def archive_cycle(
    config: VaultConfig,
    *,
    machine_id: str | None = None,
    due_only: bool = True,
    deep: bool = True,
) -> ArchiveCycleResult:
    if archive_backend(config).maintenance_path.exists():
        return ArchiveCycleResult("busy", machine_id or "unknown", None, (), (), "archive_maintenance_in_progress")
    resolved_machine_id = machine_id or "unknown"
    state_path = config.paths.home.expanduser() / ".config" / "agent-session-vault" / "archive-cycle-state.json"
    lock_path = state_path.with_name("archive-cycle.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ArchiveCycleResult("busy", resolved_machine_id, None, (), (), "archive_cycle_lock_held")
        phase = "archive_admission"
        cycle_id = None
        cycle_staging = None
        snapshot_ids: tuple[str, ...] = ()
        verifications: tuple[dict[str, object], ...] = ()
        staging_cleanup = None
        try:
            resolved_machine_id = machine_id or _read_machine_id_for_cycle(config)
            state = _read_state(state_path)
            if (
                due_only
                and not _is_due(state, config.archive.cadence_days)
                and not _has_uncovered_archived_sessions(config, resolved_machine_id)
            ):
                return ArchiveCycleResult(
                    "not_due",
                    resolved_machine_id,
                    None,
                    (),
                    (),
                    "cadence_not_reached_and_archived_sessions_covered",
                )
            cycle_id = _cycle_id()
            cycle_staging = config.archive.staging_root.expanduser() / cycle_id
            try:
                built = build_snapshot(config, machine_id=resolved_machine_id, cycle_id=cycle_id)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                return ArchiveCycleResult(
                    "failed",
                    resolved_machine_id,
                    cycle_id,
                    (),
                    (),
                    "snapshot_build_failed",
                    "retained" if cycle_staging.exists() else "not_created",
                    str(cycle_staging),
                    f"{type(exc).__name__}: {exc}",
                )
            if not built.snapshots:
                return ArchiveCycleResult(
                    "pending",
                    resolved_machine_id,
                    built.cycle_id,
                    (),
                    (),
                    "no_codex_sources",
                    "retained",
                )
            if any(snapshot.status != "staged" for snapshot in built.snapshots):
                return ArchiveCycleResult(
                    "pending",
                    resolved_machine_id,
                    built.cycle_id,
                    (),
                    (),
                    "source_changed_during_scan",
                    "retained",
                )
            phase = "snapshot_publish"
            published = publish_snapshot(config, built.staging_root)
            dedup = _read_state(built.staging_root / "dedup.json") or built.dedup
            snapshot_ids = tuple(item.snapshot.snapshot_id for item in published)
            phase = "snapshot_verify"
            last_index = len(published) - 1
            verifications = tuple(
                verify_snapshot(
                    config,
                    item.snapshot.snapshot_id,
                    deep=deep,
                    run_repo_check=(deep and idx == last_index),
                )
                for idx, item in enumerate(published)
            )
            verified = bool(published) and all(item.get("status") == "verified" for item in verifications)
            status = "verified" if verified and not built.scan.missing_sources else "partial"
            reason = None
            staging_cleanup = "retained"
            if status == "verified":
                try:
                    _cleanup_cycle_staging(config, built.staging_root, built.cycle_id)
                except (OSError, ValueError):
                    status = "partial"
                    reason = "staging_cleanup_failed"
                    staging_cleanup = "failed"
                else:
                    staging_cleanup = "removed"
            if status == "verified":
                phase = "cycle_state_write"
                _write_json(
                    state_path,
                    {
                        "schema_version": "archive-cycle-state.v1",
                        "machine_id": resolved_machine_id,
                        "last_verified_at": utc_now(),
                        "snapshot_ids": list(snapshot_ids),
                    },
                )
            phase = "cycle_receipt_write"
            recorded = receipt(
                config,
                "archive-cycle",
                status,
                machine_id=resolved_machine_id,
                details={
                    "cycle_id": built.cycle_id,
                    "snapshot_ids": list(snapshot_ids),
                    "missing_sources": list(built.scan.missing_sources),
                    "verifications": list(verifications),
                    "staging_root": str(built.staging_root),
                    "staging_cleanup": staging_cleanup,
                    "dedup": dedup,
                },
            )
            return ArchiveCycleResult(
                status,
                resolved_machine_id,
                built.cycle_id,
                snapshot_ids,
                verifications,
                reason,
                staging_cleanup,
                str(built.staging_root),
                receipt_path=str(config.archive.root / "receipts" / resolved_machine_id / f"{recorded.operation_id}.json"),
                dedup={k: int(v) for k, v in dedup.items() if isinstance(v, int)},
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            return ArchiveCycleResult(
                "failed",
                resolved_machine_id,
                cycle_id,
                snapshot_ids,
                verifications,
                f"{phase}_failed",
                staging_cleanup
                or ("retained" if cycle_staging is not None and cycle_staging.exists() else "not_created"),
                str(cycle_staging) if cycle_staging is not None else None,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_machine_id_for_cycle(config: VaultConfig) -> str:
    from .archive_sources import load_or_create_machine_id

    return load_or_create_machine_id(config)


def _read_state(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_due(state: dict[str, object], cadence_days: int) -> bool:
    raw = state.get("last_verified_at")
    if not isinstance(raw, str):
        return True
    try:
        last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (datetime.now(UTC) - last.astimezone(UTC)).total_seconds() >= cadence_days * 86400


def _cleanup_cycle_staging(config: VaultConfig, staging_root: Path, cycle_id: str) -> None:
    configured_root = config.archive.staging_root.expanduser()
    candidate = staging_root.expanduser()
    if configured_root.is_symlink() or candidate.is_symlink():
        raise ValueError("archive cycle staging paths must not be symlinks")
    if candidate.name != cycle_id or candidate.parent.resolve() != configured_root.resolve():
        raise ValueError(f"archive cycle staging path is outside the configured root: {candidate}")
    shutil.rmtree(candidate)


def _has_uncovered_archived_sessions(config: VaultConfig, machine_id: str) -> bool:
    scan = scan_codex_sources(config, machine_id=machine_id)
    backend: ResticArchiveBackend | None = None

    for scanned in scan.sources:
        root = Path(scanned.source.root_path)
        archived_records = tuple(
            item.record
            for item in scanned.files
            if _is_archived_session_path(root, item.source_path)
        )
        if not archived_records:
            continue

        backend = backend or archive_backend(config)
        latest = _latest_published_manifest(backend, machine_id, scanned.source.source_id)
        if latest is None:
            return True
        snapshot, manifest = latest
        snapshot_dir = next(
            (path for path in backend.iter_snapshot_dirs(machine_id) if path.name == snapshot.snapshot_id),
            None,
        )
        if snapshot_dir is None:
            return True
        verification = backend.verify_snapshot(snapshot_dir, deep=True)
        if verification.get("status") != "verified":
            raise ValueError(f"latest archive snapshot is not deeply verified: {snapshot.snapshot_id}")

        covered = {record.path: record.sha256 for record in manifest.files}
        if any(covered.get(record.path) != record.sha256 for record in archived_records):
            return True

    return False


def _is_archived_session_path(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] == "archived_sessions"

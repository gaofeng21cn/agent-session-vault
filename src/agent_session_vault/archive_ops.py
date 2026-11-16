from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
import fcntl

from .archive import bundle_member_digests, pack_paths, sha256_file, verify_bundle_members
from .archive_backend import FilesystemArchiveBackend, PublishedSnapshot, _manifest_from_payload, _snapshot_from_payload
from .archive_catalog import write_catalog_segment
from .archive_content import ArchiveContentIndex
from .archive_model import (
    ArchiveBundle,
    ArchiveFileRecord,
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
    staged_objects: dict[str, Path]
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


def archive_backend(config: VaultConfig) -> FilesystemArchiveBackend:
    return FilesystemArchiveBackend(config.archive.root)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _snapshot_id(source_id: str) -> str:
    return f"snap-{source_id.removeprefix('source-')}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"


def _cycle_id() -> str:
    return f"cycle-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"


def _latest_published_manifest(
    backend: FilesystemArchiveBackend,
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


def _stage_changed_files(
    scanned,
    previous: ArchiveManifest | None,
    root: Path,
    *,
    content_index: ArchiveContentIndex | None = None,
) -> tuple[list[ArchiveFileRecord], list[ArchiveBundle], dict[str, Path], tuple[str, ...]]:
    index = content_index if content_index is not None else ArchiveContentIndex()
    if content_index is None and previous:
        previous_bundles = {bundle.bundle_id: bundle for bundle in previous.bundles}
        for record in previous.files:
            index.add(record, previous_bundles[record.bundle_id])
    records: dict[str, ArchiveFileRecord] = {}
    bundles: dict[str, ArchiveBundle] = {}
    pending: dict[tuple[int, str], list] = {}
    for item in scanned.files:
        location = index.find(item.record)
        if location is not None:
            records[item.record.path] = replace(
                item.record, bundle_id=location.bundle.bundle_id, member=location.record.member,
            )
            bundles[location.bundle.bundle_id] = location.bundle
        else:
            pending.setdefault((item.record.bytes, item.record.sha256), []).append(item)

    staged_objects: dict[str, Path] = {}
    warnings: list[str] = []
    if pending:
        representatives = [items[0] for items in pending.values()]
        members = {
            item.record.path: item.source_path.relative_to(Path(scanned.source.root_path)).as_posix()
            for item in representatives
        }
        raw_bundle = root / "bundle.tar.zst"
        pack_paths(scanned.capture_root or Path(scanned.source.root_path), list(members.values()), raw_bundle)
        actual_members = bundle_member_digests(raw_bundle)
        missing = set(members.values()) - set(actual_members)
        if missing:
            raise ValueError(f"missing packed archive member: {min(missing)}")
        extra = set(actual_members) - set(members.values())
        if extra:
            raise ValueError(f"unexpected packed archive member: {min(extra)}")
        packed: dict[str, ArchiveFileRecord] = {}
        actual_keys: set[tuple[int, str]] = set()
        for items in pending.values():
            item = items[0]
            record = item.record
            member = members[record.path]
            actual = actual_members[member]
            if actual != (record.bytes, record.sha256):
                warnings.append(f"changed-during-pack:{record.path}")
                if len(items) > 1:
                    raise ValueError(f"shared source content changed while packing: {record.path}")
            actual_record = replace(record, bytes=actual[0], sha256=actual[1], member=member)
            # A concurrently rewritten source can converge to a known file, or
            # two writers can converge to the same bytes. Retain diagnostic
            # staging and stop instead of publishing a redundant payload.
            if actual in actual_keys or index.find(actual_record) is not None:
                raise ValueError(f"source converged to existing content while packing: {record.path}")
            actual_keys.add(actual)
            packed[record.path] = actual_record
        digest = sha256_file(raw_bundle)
        bundle_id = f"bundle-{digest}"
        final_bundle = root / "bundles" / f"{bundle_id}.tar.zst"
        final_bundle.parent.mkdir(parents=True, exist_ok=True)
        raw_bundle.replace(final_bundle)
        staged_objects[bundle_id] = final_bundle
        bundle = ArchiveBundle(
            bundle_id=bundle_id, object_path=f"objects/{digest}.tar.zst", sha256=digest,
            bytes=final_bundle.stat().st_size,
            source_bytes=sum(record.bytes for record in packed.values()), file_count=len(packed),
        )
        bundles[bundle_id] = bundle
        for items in pending.values():
            representative = items[0]
            stored = replace(packed[representative.record.path], bundle_id=bundle_id)
            index.add(stored, bundle)
            records[stored.path] = stored
            for alias in items[1:]:
                records[alias.record.path] = replace(alias.record, bundle_id=bundle_id, member=stored.member)
    # Old snapshots retain their own objects. A new manifest references only
    # the bundles needed by its files, rather than inheriting all old bundles.
    return [records[item.record.path] for item in scanned.files], list(bundles.values()), staged_objects, tuple(sorted(set(warnings)))


def build_snapshot(
    config: VaultConfig,
    *,
    machine_id: str | None = None,
    cycle_id: str | None = None,
    staging_root: Path | None = None,
) -> SnapshotBuildResult:
    archive_backend(config).ensure_writable()
    # Local hardlinks pin discovered files without copying their payload or
    # requiring SMB hardlink support. Keep them only for this snapshot build.
    with tempfile.TemporaryDirectory(prefix="asv-capture-") as captured:
        return _build_captured_snapshot(
            config,
            machine_id=machine_id,
            cycle_id=cycle_id,
            staging_root=staging_root,
            capture_root=Path(captured),
        )


def _build_captured_snapshot(
    config: VaultConfig,
    *,
    machine_id: str | None,
    cycle_id: str | None,
    staging_root: Path | None,
    capture_root: Path,
) -> SnapshotBuildResult:
    backend = archive_backend(config)
    backend.ensure_ready()
    content_index = ArchiveContentIndex.load(backend)
    scan = scan_codex_sources(config, machine_id=machine_id, capture_root=capture_root)
    cycle = cycle_id or _cycle_id()
    cycle_staging_root = (staging_root or config.archive.staging_root).expanduser() / cycle
    if cycle_staging_root.exists():
        raise FileExistsError(f"archive cycle staging already exists: {cycle_staging_root}")
    cycle_staging_root.mkdir(parents=True, exist_ok=False)
    snapshots: list[ArchiveSnapshot] = []
    manifests: list[ArchiveManifest] = []
    staged_objects: dict[str, Path] = {}
    for scanned in scan.sources:
        published = content_index.latest.get((scan.machine_id, scanned.source.source_id))
        previous = (published.snapshot, next(manifest for manifest in published.manifests
                                             if manifest.source.source_id == scanned.source.source_id)) if published else None
        snapshot_id = _snapshot_id(scanned.source.source_id)
        source_root = cycle_staging_root / scanned.source.source_id
        source_root.mkdir(parents=True, exist_ok=True)
        records, bundles, objects, pack_warnings = _stage_changed_files(
            scanned,
            previous[1] if previous else None,
            source_root,
            content_index=content_index,
        )
        staged_objects.update(objects)
        warnings = tuple(sorted(set((*scanned.warnings, *pack_warnings))))
        consistency = "quiesced" if not warnings else "best_effort"
        status = "staged"
        snapshot = ArchiveSnapshot(
            snapshot_id=snapshot_id,
            cycle_id=cycle,
            machine_id=scan.machine_id,
            source=scanned.source,
            captured_at=utc_now(),
            consistency=consistency,
            status=status,
            parent_snapshot_id=previous[0].snapshot_id if previous else None,
            warnings=warnings,
        )
        manifest = ArchiveManifest(
            snapshot_id=snapshot_id,
            source=scanned.source,
            files=tuple(sorted(records, key=lambda item: item.path)),
            bundles=tuple(sorted(bundles, key=lambda item: item.bundle_id)),
            deleted_paths=(
                tuple(sorted({record.path for record in previous[1].files} - {record.path for record in records}))
                if previous
                else ()
            ),
        )
        snapshot_dir = source_root
        _write_json(snapshot_dir / "snapshot.json", snapshot.to_payload())
        _write_json(snapshot_dir / "manifest.json", manifest.to_payload())
        snapshots.append(snapshot)
        manifests.append(manifest)
    _write_json(
        cycle_staging_root / "cycle.json",
        {
            "schema_version": "archive.cycle.v1",
            "cycle_id": cycle,
            "machine_id": scan.machine_id,
            "snapshot_ids": [snapshot.snapshot_id for snapshot in snapshots],
            "missing_sources": list(scan.missing_sources),
        },
    )
    new_bundles = {bundle.bundle_id: bundle for manifest in manifests for bundle in manifest.bundles
                   if bundle.bundle_id in staged_objects}
    all_files = [record for manifest in manifests for record in manifest.files]
    packed_files = sum(bundle.file_count for bundle in new_bundles.values())
    packed_bytes = sum(bundle.source_bytes for bundle in new_bundles.values())
    historical_files = [record for record in all_files if record.bundle_id in content_index.published_bundle_ids]
    dedup = {
        "files_seen": len(all_files), "file_logical_bytes": sum(record.bytes for record in all_files),
        "new_content_files": packed_files, "new_content_bytes": packed_bytes,
        "new_objects": len(staged_objects), "new_archive_bytes": sum(bundle.bytes for bundle in new_bundles.values()),
        "reused_files": len(all_files) - packed_files,
        "reused_logical_bytes": sum(record.bytes for record in all_files) - packed_bytes,
        "historical_reused_files": len(historical_files),
        "same_cycle_reused_files": len(all_files) - len(historical_files) - packed_files,
    }
    _write_json(cycle_staging_root / "dedup.json", dedup)
    return SnapshotBuildResult(
        machine_id=scan.machine_id,
        cycle_id=cycle,
        staging_root=cycle_staging_root,
        snapshots=tuple(snapshots),
        manifests=tuple(manifests),
        staged_objects=staged_objects,
        scan=scan,
        dedup=dedup,
    )


def publish_snapshot(
    config: VaultConfig,
    staging_root: Path,
    *,
    verify_staged: bool = False,
) -> list[PublishedSnapshot]:
    backend = archive_backend(config)
    backend.ensure_writable()
    backend.ensure_ready()
    result: list[PublishedSnapshot] = []
    prepared = []
    cycle_objects: dict[str, Path] = {}
    for source_dir in sorted(path for path in staging_root.iterdir() if path.is_dir()):
        snapshot_path = source_dir / "snapshot.json"
        manifest_path = source_dir / "manifest.json"
        if not snapshot_path.is_file() or not manifest_path.is_file():
            continue
        snapshot = _snapshot_from_payload(json.loads(snapshot_path.read_text(encoding="utf-8")))
        if snapshot.status != "staged":
            raise ValueError(f"cannot publish an unstable snapshot: {snapshot.snapshot_id}")
        manifest = _manifest_from_payload(json.loads(manifest_path.read_text(encoding="utf-8")))
        staged_objects = {
            bundle.bundle_id: source_dir / "bundles" / f"{bundle.bundle_id}.tar.zst"
            for bundle in manifest.bundles
            if (source_dir / "bundles" / f"{bundle.bundle_id}.tar.zst").is_file()
        }
        if verify_staged:
            _verify_staged_manifest(manifest, staged_objects)
        prepared.append((snapshot, manifest))
        cycle_objects.update(staged_objects)
    # A later source may reference content first packed by an earlier source.
    # Publish every new object before committing any of the source snapshots,
    # independent of source-ID/directory order.
    for bundle_id, local_path in cycle_objects.items():
        backend.publish_object(local_path, bundle_id.removeprefix("bundle-"))
    for snapshot, manifest in prepared:
        published = backend.publish_snapshot(snapshot, (manifest,), {})
        write_catalog_segment(backend, published)
        result.append(published)
    return result


def _verify_staged_manifest(
    manifest: ArchiveManifest,
    staged_objects: dict[str, Path],
) -> None:
    for bundle_id, bundle_path in staged_objects.items():
        expected = {
            record.member: (record.bytes, record.sha256)
            for record in manifest.files
            if record.bundle_id == bundle_id and record.member
        }
        _, failures = verify_bundle_members(bundle_path, expected)
        if failures:
            raise ValueError(f"staged bundle does not match manifest: {bundle_id}: {failures[0]}")


def verify_snapshot(config: VaultConfig, snapshot_id: str, *, deep: bool = False) -> dict[str, object]:
    backend = archive_backend(config)
    for snapshot_dir in backend.iter_snapshot_dirs():
        if snapshot_dir.name == snapshot_id:
            return backend.verify_snapshot(snapshot_dir, deep=deep)
    raise FileNotFoundError(f"snapshot not found: {snapshot_id}")


def init_backend(config: VaultConfig) -> dict[str, object]:
    return archive_backend(config).initialize()


def receipt(config: VaultConfig, operation: str, status: str, *, machine_id: str, source_id: str | None = None, snapshot_id: str | None = None, details: dict[str, object] | None = None) -> ArchiveReceipt:
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
        snapshot_ids = ()
        verifications = ()
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
            snapshot_ids = tuple(item.snapshot.snapshot_id for item in published)
            phase = "snapshot_verify"
            verifications = tuple(
                verify_snapshot(config, item.snapshot.snapshot_id, deep=deep)
                for item in published
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
                    "dedup": built.dedup,
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
                dedup=built.dedup,
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            return ArchiveCycleResult(
                "failed", resolved_machine_id, cycle_id, snapshot_ids, verifications,
                f"{phase}_failed", staging_cleanup or (
                    "retained" if cycle_staging is not None and cycle_staging.exists() else "not_created"),
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
    backend: FilesystemArchiveBackend | None = None

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

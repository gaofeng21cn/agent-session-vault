from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import uuid
import fcntl

from .archive import bundle_member_digests, pack_paths, sha256_file, verify_bundle_members
from .archive_backend import FilesystemArchiveBackend, PublishedSnapshot, _manifest_from_payload, _snapshot_from_payload
from .archive_catalog import write_catalog_segment
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
        }


@dataclass(frozen=True)
class ArchiveCycleResult:
    status: str
    machine_id: str
    cycle_id: str | None
    snapshot_ids: tuple[str, ...]
    verifications: tuple[dict[str, object], ...]
    reason: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "machine_id": self.machine_id,
            "cycle_id": self.cycle_id,
            "snapshot_ids": list(self.snapshot_ids),
            "verifications": list(self.verifications),
            "reason": self.reason,
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
) -> tuple[list[ArchiveFileRecord], list[ArchiveBundle], dict[str, Path], tuple[str, ...]]:
    previous_by_path = {record.path: record for record in previous.files} if previous else {}
    previous_bundles = {bundle.bundle_id: bundle for bundle in previous.bundles} if previous else {}
    changed = [item for item in scanned.files if previous_by_path.get(item.record.path, None) is None or previous_by_path[item.record.path].sha256 != item.record.sha256]
    member_paths: list[str] = []
    records: dict[str, ArchiveFileRecord] = {}
    for item in changed:
        source_relative = item.source_path.relative_to(Path(scanned.source.root_path)).as_posix()
        member = source_relative
        member_paths.append(member)
        records[item.record.path] = ArchiveFileRecord(
            path=item.record.path,
            session_id=item.record.session_id,
            bytes=item.record.bytes,
            sha256=item.record.sha256,
            start_at=item.record.start_at,
            end_at=item.record.end_at,
            time_source=item.record.time_source,
            parse_status=item.record.parse_status,
            member=member,
        )

    staged_objects: dict[str, Path] = {}
    new_bundles: list[ArchiveBundle] = []
    warnings: list[str] = []
    if member_paths:
        raw_bundle = root / "bundle.tar.zst"
        pack_paths(Path(scanned.source.root_path), member_paths, raw_bundle)
        digest = sha256_file(raw_bundle)
        bundle_id = f"bundle-{digest}"
        final_bundle = root / "bundles" / f"{bundle_id}.tar.zst"
        final_bundle.parent.mkdir(parents=True, exist_ok=True)
        raw_bundle.replace(final_bundle)
        staged_objects[bundle_id] = final_bundle
        actual_members = bundle_member_digests(
            final_bundle,
            [record.member for record in records.values() if record.member],
        )
        for path, record in tuple(records.items()):
            actual = actual_members.get(record.member or "")
            if actual is None:
                warnings.append(f"missing-packed-member:{record.path}")
                continue
            actual_bytes, actual_sha256 = actual
            if (actual_bytes, actual_sha256) != (record.bytes, record.sha256):
                warnings.append(f"changed-during-pack:{record.path}")
                records[path] = ArchiveFileRecord(
                    path=record.path,
                    session_id=record.session_id,
                    bytes=actual_bytes,
                    sha256=actual_sha256,
                    start_at=record.start_at,
                    end_at=record.end_at,
                    time_source=record.time_source,
                    parse_status=record.parse_status,
                    member=record.member,
                )
        new_bundles.append(
            ArchiveBundle(
                bundle_id=bundle_id,
                object_path=f"objects/{digest}.tar.zst",
                sha256=digest,
                bytes=final_bundle.stat().st_size,
                source_bytes=sum(record.bytes for record in records.values()),
                file_count=len(records),
            )
        )
        records = {
            path: ArchiveFileRecord(
                path=record.path,
                session_id=record.session_id,
                bytes=record.bytes,
                sha256=record.sha256,
                start_at=record.start_at,
                end_at=record.end_at,
                time_source=record.time_source,
                parse_status=record.parse_status,
                bundle_id=bundle_id,
                member=record.member,
            )
            for path, record in records.items()
        }

    current_records = [records.get(item.record.path, previous_by_path.get(item.record.path, item.record)) for item in scanned.files]
    bundles = tuple({bundle.bundle_id: bundle for bundle in (*previous_bundles.values(), *new_bundles)}.values())
    return current_records, list(bundles), staged_objects, tuple(sorted(set(warnings)))


def build_snapshot(
    config: VaultConfig,
    *,
    machine_id: str | None = None,
    cycle_id: str | None = None,
    staging_root: Path | None = None,
) -> SnapshotBuildResult:
    backend = archive_backend(config)
    backend.ensure_ready()
    scan = scan_codex_sources(config, machine_id=machine_id)
    cycle = cycle_id or _cycle_id()
    cycle_staging_root = (staging_root or config.archive.staging_root).expanduser() / cycle
    if cycle_staging_root.exists():
        shutil.rmtree(cycle_staging_root)
    cycle_staging_root.mkdir(parents=True, exist_ok=False)
    snapshots: list[ArchiveSnapshot] = []
    manifests: list[ArchiveManifest] = []
    staged_objects: dict[str, Path] = {}
    for scanned in scan.sources:
        previous = _latest_published_manifest(backend, scan.machine_id, scanned.source.source_id)
        snapshot_id = _snapshot_id(scanned.source.source_id)
        source_root = cycle_staging_root / scanned.source.source_id
        source_root.mkdir(parents=True, exist_ok=True)
        records, bundles, objects, pack_warnings = _stage_changed_files(
            scanned,
            previous[1] if previous else None,
            source_root,
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
    return SnapshotBuildResult(
        machine_id=scan.machine_id,
        cycle_id=cycle,
        staging_root=cycle_staging_root,
        snapshots=tuple(snapshots),
        manifests=tuple(manifests),
        staged_objects=staged_objects,
        scan=scan,
    )


def publish_snapshot(
    config: VaultConfig,
    staging_root: Path,
    *,
    verify_staged: bool = False,
) -> list[PublishedSnapshot]:
    backend = archive_backend(config)
    backend.ensure_ready()
    result: list[PublishedSnapshot] = []
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
        published = backend.publish_snapshot(snapshot, (manifest,), staged_objects)
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
    resolved_machine_id = machine_id or "unknown"
    state_path = config.paths.home.expanduser() / ".config" / "agent-session-vault" / "archive-cycle-state.json"
    lock_path = state_path.with_name("archive-cycle.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ArchiveCycleResult("busy", resolved_machine_id, None, (), (), "archive_cycle_lock_held")
        try:
            resolved_machine_id = machine_id or _read_machine_id_for_cycle(config)
            state = _read_state(state_path)
            if due_only and not _is_due(state, config.archive.cadence_days):
                return ArchiveCycleResult("not_due", resolved_machine_id, None, (), (), "cadence_not_reached")
            built = build_snapshot(config, machine_id=resolved_machine_id)
            if not built.snapshots:
                return ArchiveCycleResult("pending", resolved_machine_id, built.cycle_id, (), (), "no_codex_sources")
            if any(snapshot.status != "staged" for snapshot in built.snapshots):
                return ArchiveCycleResult("pending", resolved_machine_id, built.cycle_id, (), (), "source_changed_during_scan")
            published = publish_snapshot(config, built.staging_root)
            verifications = tuple(
                verify_snapshot(config, item.snapshot.snapshot_id, deep=deep)
                for item in published
            )
            snapshot_ids = tuple(item.snapshot.snapshot_id for item in published)
            verified = bool(published) and all(item.get("status") == "verified" for item in verifications)
            status = "verified" if verified and not built.scan.missing_sources else "partial"
            if status == "verified":
                _write_json(
                    state_path,
                    {
                        "schema_version": "archive-cycle-state.v1",
                        "machine_id": resolved_machine_id,
                        "last_verified_at": utc_now(),
                        "snapshot_ids": list(snapshot_ids),
                    },
                )
            receipt(
                config,
                "archive-cycle",
                status,
                machine_id=resolved_machine_id,
                details={
                    "cycle_id": built.cycle_id,
                    "snapshot_ids": list(snapshot_ids),
                    "missing_sources": list(built.scan.missing_sources),
                    "verifications": list(verifications),
                },
            )
            return ArchiveCycleResult(status, resolved_machine_id, built.cycle_id, snapshot_ids, verifications)
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

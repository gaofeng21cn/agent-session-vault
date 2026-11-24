from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import uuid

from .archive import sha256_file
from .archive_model import ArchiveFileRecord, ArchiveManifest, ArchiveSnapshot, ArchiveSource, payload_sha256
from .restic import ResticBackupSummary, ResticError, ResticRepository


ARCHIVE_FORMAT = "agent-session-vault-restic-v2"
LEGACY_ARCHIVE_FORMAT = "agent-session-vault-archive-v1"
ROOT_MARKER_NAME = "root-marker.json"
COMMIT_MARKER_NAME = "COMMITTED"


class ArchiveBackendError(RuntimeError):
    pass


class LegacyArchiveFormatError(ArchiveBackendError):
    """Raised fail-closed when an existing legacy archive-v1 root is targeted."""


@dataclass(frozen=True)
class PublishedSnapshot:
    snapshot_dir: Path
    snapshot: ArchiveSnapshot
    manifests: tuple[ArchiveManifest, ...]


class ResticArchiveBackend:
    """Restic-backed archive storage for local directories and mounted NAS roots.

    All file payloads, CDC chunk deduplication, encryption, compression,
    verification, and compaction live in ``<root>/repo`` (managed by the
    ``restic`` binary). Committed Vault snapshot manifests bind exact
    ``restic_snapshot_id`` values alongside per-file SHA-256 metadata and
    catalog segments. Existing legacy ``archive-v1`` roots are detected
    read-only and never mutated in place.
    """

    def __init__(
        self,
        root: Path,
        *,
        password_file: Path | None = None,
        cache_dir: Path | None = None,
        restic_binary: str | None = None,
    ) -> None:
        self.root = root.expanduser()
        self.password_file = password_file.expanduser() if password_file is not None else None
        self.cache_dir = cache_dir.expanduser() if cache_dir is not None else None
        self.restic_binary = restic_binary

    @property
    def marker_path(self) -> Path:
        return self.root / ROOT_MARKER_NAME

    @property
    def repo_path(self) -> Path:
        return self.root / "repo"

    @property
    def maintenance_path(self) -> Path:
        return self.root.with_name(f".{self.root.name}.maintenance.json")

    def restic(self) -> ResticRepository:
        return ResticRepository(
            self.repo_path,
            password_file=self.password_file,
            cache_dir=self.cache_dir,
            binary=self.restic_binary,
        )

    def ensure_writable(self) -> None:
        if self.maintenance_path.exists() or self.maintenance_path.is_symlink():
            raise ArchiveBackendError(
                f"archive maintenance in progress; reconcile its recorded runner: {self.maintenance_path}"
            )

    def detect_format(self) -> str | None:
        """Read-only format detection without touching or locking the archive root."""
        if not self.marker_path.is_file():
            return None
        marker = self._read_json(self.marker_path)
        value = marker.get("format")
        return str(value) if isinstance(value, str) else None

    def _raise_if_legacy(self, marker: dict[str, object]) -> None:
        if marker.get("format") == LEGACY_ARCHIVE_FORMAT:
            raise LegacyArchiveFormatError(
                f"legacy archive-v1 repository detected at {self.root}; "
                "refusing in-place writes to preserve existing NAS data. "
                "Inspect read-only with `agent-session-vault archive legacy-status --json`, "
                "point [archive].root at a new restic archive root, run `archive init` and "
                "`ops archive-cycle --no-due-only --deep`, and keep the legacy v1 directory "
                "read-only until verified."
            )

    def inspect_legacy(self) -> dict[str, object]:
        """Pure read-only inspection for legacy archive-v1 detection and migration readiness."""
        fence_present = self.maintenance_path.exists() or self.maintenance_path.is_symlink()
        if not self.root.exists():
            return {
                "status": "uninitialized",
                "format": None,
                "root": str(self.root),
                "read_only": True,
                "migration_required": False,
                "maintenance_fence_present": fence_present,
            }
        if not self.marker_path.is_file():
            return {
                "status": "uninitialized",
                "format": None,
                "root": str(self.root),
                "read_only": True,
                "migration_required": False,
                "maintenance_fence_present": fence_present,
            }
        marker = self._read_json(self.marker_path)
        fmt = marker.get("format")
        snapshot_dirs = self._iter_committed_snapshot_dirs()
        catalog_root = self.root / "catalog" / "segments"
        catalog_segments = sum(1 for _ in catalog_root.rglob("*.json")) if catalog_root.is_dir() else 0
        if fmt == LEGACY_ARCHIVE_FORMAT:
            objects_root = self.root / "objects"
            object_files = list(objects_root.glob("*.tar.zst")) if objects_root.is_dir() else []
            object_bytes = sum(path.stat().st_size for path in object_files if path.is_file())
            return {
                "status": "legacy_v1_detected",
                "format": LEGACY_ARCHIVE_FORMAT,
                "vault_id": marker.get("vault_id"),
                "created_at": marker.get("created_at"),
                "root": str(self.root),
                "read_only": True,
                "migration_required": True,
                "maintenance_fence_present": fence_present,
                "snapshot_count": len(snapshot_dirs),
                "object_count": len(object_files),
                "object_bytes": object_bytes,
                "catalog_segment_count": catalog_segments,
                "guidance": (
                    "Retain this legacy archive-v1 directory read-only. Configure a new [archive].root "
                    "for restic-v2, run `archive init` and `ops archive-cycle --no-due-only --deep`, "
                    "and verify coverage before retiring legacy v1 archives."
                ),
            }
        if fmt == ARCHIVE_FORMAT:
            return {
                "status": "current_restic_v2",
                "format": ARCHIVE_FORMAT,
                "vault_id": marker.get("vault_id"),
                "restic_repo_id": marker.get("restic_repo_id"),
                "created_at": marker.get("created_at"),
                "root": str(self.root),
                "repo_path": str(self.repo_path),
                "repo_initialized": self.restic().exists(),
                "read_only": True,
                "migration_required": False,
                "maintenance_fence_present": fence_present,
                "snapshot_count": len(snapshot_dirs),
                "catalog_segment_count": catalog_segments,
            }
        return {
            "status": "unknown_format",
            "format": fmt,
            "root": str(self.root),
            "read_only": True,
            "migration_required": False,
            "maintenance_fence_present": fence_present,
        }

    def initialize(self, vault_id: str | None = None) -> dict[str, object]:
        self.ensure_writable()
        if self.root.exists() and self.root.is_symlink():
            raise ArchiveBackendError(f"archive root must not be a symlink: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.marker_path.exists():
            payload = self._read_json(self.marker_path)
            self._raise_if_legacy(payload)
            if payload.get("format") != ARCHIVE_FORMAT:
                raise ArchiveBackendError(f"archive root marker has an unexpected format: {self.marker_path}")
            if not self.restic().exists():
                raise ArchiveBackendError(f"restic repository is missing under initialized archive root: {self.repo_path}")
            return payload

        repo_info = self.restic().init()
        payload: dict[str, object] = {
            "format": ARCHIVE_FORMAT,
            "schema_version": 2,
            "vault_id": vault_id or f"vault-{uuid.uuid4().hex}",
            "restic_repo_id": str(repo_info.get("id", "")),
            "created_at": datetime.now(UTC).isoformat(),
        }
        for name in ("snapshots", "catalog", "receipts", "staging"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(self.marker_path, payload)
        return payload

    def ensure_ready(self) -> dict[str, object]:
        if not self.root.exists() or not self.root.is_dir() or self.root.is_symlink():
            raise ArchiveBackendError(f"archive root is not an ordinary directory: {self.root}")
        if not self.marker_path.is_file():
            raise ArchiveBackendError(f"archive root marker is missing: {self.marker_path}")
        marker = self._read_json(self.marker_path)
        self._raise_if_legacy(marker)
        if marker.get("format") != ARCHIVE_FORMAT:
            raise ArchiveBackendError(f"archive root marker has an unexpected format: {self.marker_path}")
        if not self.restic().exists():
            raise ArchiveBackendError(f"restic repository is missing: {self.repo_path}")
        return marker

    def backup_source(
        self,
        cwd: Path,
        relative_paths: list[str],
        *,
        hostname: str,
        tags: tuple[str, ...],
        parent: str | None = None,
    ) -> ResticBackupSummary:
        self.ensure_writable()
        self.ensure_ready()
        return self.restic().backup(cwd, relative_paths, hostname=hostname, tags=tags, parent=parent)

    def publish_snapshot(
        self,
        snapshot: ArchiveSnapshot,
        manifests: tuple[ArchiveManifest, ...],
    ) -> PublishedSnapshot:
        self.ensure_writable()
        self.ensure_ready()
        snapshot_dir = self.root / "snapshots" / snapshot.machine_id / snapshot.snapshot_id
        if snapshot_dir.exists():
            if (snapshot_dir / COMMIT_MARKER_NAME).is_file():
                return self.load_snapshot(snapshot_dir)
            shutil.rmtree(snapshot_dir)
        temporary_root = self.root / "staging" / f"publish-{snapshot.snapshot_id}-{uuid.uuid4().hex}"
        temporary_root.mkdir(parents=True, exist_ok=False)
        try:
            snapshot_payload = snapshot.to_payload()
            manifest_payloads = [manifest.to_payload() for manifest in manifests]
            manifest_hash = payload_sha256(manifest_payloads)
            snapshot_payload["manifest_sha256"] = manifest_hash
            committed_snapshot = _snapshot_from_payload(snapshot_payload)
            self._write_json_atomic(temporary_root / "snapshot.json", snapshot_payload)
            self._write_json_atomic(temporary_root / "manifest.json", {"manifests": manifest_payloads})
            checksums = {
                "snapshot.json": sha256_file(temporary_root / "snapshot.json"),
                "manifest.json": sha256_file(temporary_root / "manifest.json"),
                "restic_snapshot_id": committed_snapshot.restic_snapshot_id,
            }
            self._write_json_atomic(temporary_root / "checksums.json", checksums)
            (temporary_root / COMMIT_MARKER_NAME).write_text(datetime.now(UTC).isoformat() + "\n", encoding="utf-8")
            snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
            temporary_root.replace(snapshot_dir)
        except Exception:
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise
        return PublishedSnapshot(snapshot_dir=snapshot_dir, snapshot=committed_snapshot, manifests=manifests)

    def _iter_committed_snapshot_dirs(self, machine_id: str | None = None) -> list[Path]:
        root = self.root / "snapshots"
        if machine_id:
            roots = [root / machine_id]
        else:
            roots = sorted(path for path in root.iterdir() if path.is_dir()) if root.exists() else []
        result: list[Path] = []
        for machine_root in roots:
            if not machine_root.is_dir():
                continue
            result.extend(
                path
                for path in sorted(machine_root.iterdir(), key=str)
                if path.is_dir() and (path / COMMIT_MARKER_NAME).is_file()
            )
        return result

    def iter_snapshot_dirs(self, machine_id: str | None = None) -> list[Path]:
        self.ensure_ready()
        return self._iter_committed_snapshot_dirs(machine_id)

    def load_snapshot(self, snapshot_dir: Path) -> PublishedSnapshot:
        if not (snapshot_dir / COMMIT_MARKER_NAME).is_file():
            raise ArchiveBackendError(f"snapshot is not committed: {snapshot_dir}")
        snapshot_payload = self._read_json(snapshot_dir / "snapshot.json")
        raw_manifest = self._read_json(snapshot_dir / "manifest.json")
        manifests_raw = raw_manifest.get("manifests")
        if not isinstance(manifests_raw, list):
            raise ArchiveBackendError(f"snapshot manifests are missing: {snapshot_dir}")
        snapshot = _snapshot_from_payload(snapshot_payload)
        manifests = tuple(_manifest_from_payload(item) for item in manifests_raw if isinstance(item, dict))
        return PublishedSnapshot(snapshot_dir=snapshot_dir, snapshot=snapshot, manifests=manifests)

    def _metadata_failures(self, snapshot_dir: Path, published: PublishedSnapshot) -> list[str]:
        failures: list[str] = []
        try:
            snapshot_payload = self._read_json(snapshot_dir / "snapshot.json")
            manifest_payload = self._read_json(snapshot_dir / "manifest.json")
            checksums_payload = self._read_json(snapshot_dir / "checksums.json")
            expected_manifest_hash = payload_sha256(manifest_payload.get("manifests", []))
            if published.snapshot.manifest_sha256 != expected_manifest_hash:
                failures.append("manifest_hash_mismatch")
            if checksums_payload.get("snapshot.json") != sha256_file(snapshot_dir / "snapshot.json"):
                failures.append("snapshot_checksum_mismatch")
            if checksums_payload.get("manifest.json") != sha256_file(snapshot_dir / "manifest.json"):
                failures.append("manifest_checksum_mismatch")
            if snapshot_payload.get("snapshot_id") != published.snapshot.snapshot_id:
                failures.append("snapshot_identity_mismatch")
            if checksums_payload.get("restic_snapshot_id") != published.snapshot.restic_snapshot_id:
                failures.append("restic_snapshot_checksum_mismatch")
            for manifest in published.manifests:
                if manifest.snapshot_id != published.snapshot.snapshot_id:
                    failures.append("manifest_snapshot_identity_mismatch")
                if manifest.restic_snapshot_id != published.snapshot.restic_snapshot_id:
                    failures.append("manifest_restic_snapshot_mismatch")
        except (OSError, KeyError, TypeError, ValueError) as exc:
            failures.append(f"metadata_verification_error:{type(exc).__name__}")
        return failures

    def load_verified_metadata(self, snapshot_dir: Path) -> PublishedSnapshot:
        """Authenticate committed manifests without scanning restic data packs."""
        published = self.load_snapshot(snapshot_dir)
        failures = self._metadata_failures(snapshot_dir, published)
        if failures:
            raise ArchiveBackendError(f"archive snapshot metadata failed: {snapshot_dir}: {failures}")
        return published

    def verify_snapshot(
        self,
        snapshot_dir: Path,
        *,
        deep: bool = False,
        run_repo_check: bool = True,
    ) -> dict[str, object]:
        published = self.load_snapshot(snapshot_dir)
        failures = self._metadata_failures(snapshot_dir, published)
        restic_id = published.snapshot.restic_snapshot_id
        checked_files = 0
        checked_restic_nodes = 0
        repo_check_payload: dict[str, object] | None = None
        if failures:
            return {
                "status": "failed",
                "snapshot_id": published.snapshot.snapshot_id,
                "restic_snapshot_id": restic_id,
                "snapshot_dir": str(snapshot_dir),
                "checked_files": 0,
                "checked_restic_nodes": 0,
                "repo_check": None,
                "failures": failures,
                "deep": deep,
            }

        total_manifest_files = sum(len(manifest.files) for manifest in published.manifests)
        if total_manifest_files > 0 and not restic_id:
            failures.append("missing_restic_snapshot_id")

        if restic_id:
            repo = self.restic()
            try:
                found = repo.snapshots(snapshot_ids=(restic_id,))
            except ResticError as exc:
                failures.append(f"restic_snapshot_lookup_failed:{exc}")
                found = []
            if not any(item.id == restic_id for item in found):
                failures.append(f"missing_restic_snapshot:{restic_id}")
            elif deep:
                try:
                    nodes = repo.ls(restic_id)
                except ResticError as exc:
                    failures.append(f"restic_ls_failed:{exc}")
                    nodes = []
                file_nodes = {
                    node.path.lstrip("/"): node.size
                    for node in nodes
                    if node.type == "file"
                }
                checked_restic_nodes = len(file_nodes)
                for manifest in published.manifests:
                    for record in manifest.files:
                        member = record.member or _member_from_record(manifest.source, record)
                        actual_size = file_nodes.get(member)
                        if actual_size is None:
                            failures.append(f"missing_restic_member:{member}")
                        elif actual_size != record.bytes:
                            failures.append(f"restic_member_size_mismatch:{member}")
                        else:
                            checked_files += 1
                if run_repo_check:
                    try:
                        check_result = repo.check(read_data=True)
                    except ResticError as exc:
                        failures.append(f"restic_check_failed:{exc}")
                    else:
                        repo_check_payload = check_result.to_payload()
                        if not check_result.ok:
                            failures.append(f"restic_check_errors:{check_result.num_errors}")

        status = "verified" if not failures else "failed"
        return {
            "status": status,
            "snapshot_id": published.snapshot.snapshot_id,
            "restic_snapshot_id": restic_id,
            "snapshot_dir": str(snapshot_dir),
            "checked_files": checked_files,
            "checked_restic_nodes": checked_restic_nodes,
            "repo_check": repo_check_payload,
            "failures": failures,
            "deep": deep,
        }

    def restore_entries(self, restic_snapshot_id: str, members: tuple[str, ...], target: Path) -> dict[str, object]:
        self.ensure_ready()
        return self.restic().restore(restic_snapshot_id, target, includes=members)

    def content_status(self) -> dict[str, object]:
        self.ensure_ready()
        snapshots = [self.load_verified_metadata(path) for path in self.iter_snapshot_dirs()]
        logical_files = 0
        logical_bytes = 0
        unique_contents: dict[tuple[int, str], int] = {}
        for item in snapshots:
            for manifest in item.manifests:
                for record in manifest.files:
                    logical_files += 1
                    logical_bytes += record.bytes
                    unique_contents.setdefault((record.bytes, record.sha256), record.bytes)
        repo = self.restic()
        restic_snaps = repo.snapshots()
        raw_stats = repo.stats(mode="raw-data")
        restore_stats = repo.stats(mode="restore-size")
        return {
            "status": "measured",
            "format": ARCHIVE_FORMAT,
            "backend_root": str(self.root),
            "repo_path": str(self.repo_path),
            "vault_snapshot_count": len(snapshots),
            "restic_snapshot_count": len(restic_snaps),
            "logical_file_references": logical_files,
            "logical_referenced_bytes": logical_bytes,
            "unique_content_files": len(unique_contents),
            "unique_content_bytes": sum(unique_contents.values()),
            "restic_raw_data": raw_stats.to_payload(),
            "restic_restore_size": restore_stats.to_payload(),
        }

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArchiveBackendError(f"cannot read archive metadata: {path}") from exc
        if not isinstance(value, dict):
            raise ArchiveBackendError(f"archive metadata is not an object: {path}")
        return value

    @staticmethod
    def _write_json_atomic(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)


def _member_from_record(source: ArchiveSource, record: ArchiveFileRecord) -> str:
    prefix = source.relative_root.rstrip("/") + "/"
    if record.path.startswith(prefix):
        return record.path[len(prefix) :]
    return record.path


def _source_from_payload(payload: object) -> ArchiveSource:
    if not isinstance(payload, dict):
        raise ArchiveBackendError("archive source payload is invalid")
    return ArchiveSource(
        source_id=str(payload["source_id"]),
        machine_id=str(payload["machine_id"]),
        client=str(payload["client"]),
        kind=str(payload["kind"]),
        root_path=str(payload["root_path"]),
        relative_root=str(payload["relative_root"]),
        label=payload.get("label") if isinstance(payload.get("label"), str) else None,
    )


def _snapshot_from_payload(payload: dict[str, object]) -> ArchiveSnapshot:
    return ArchiveSnapshot(
        snapshot_id=str(payload["snapshot_id"]),
        cycle_id=str(payload["cycle_id"]),
        machine_id=str(payload["machine_id"]),
        source=_source_from_payload(payload["source"]),
        captured_at=str(payload["captured_at"]),
        consistency=str(payload["consistency"]),
        status=str(payload["status"]),
        parent_snapshot_id=payload.get("parent_snapshot_id") if isinstance(payload.get("parent_snapshot_id"), str) else None,
        manifest_sha256=payload.get("manifest_sha256") if isinstance(payload.get("manifest_sha256"), str) else None,
        restic_snapshot_id=payload.get("restic_snapshot_id") if isinstance(payload.get("restic_snapshot_id"), str) else None,
        warnings=tuple(item for item in payload.get("warnings", []) if isinstance(item, str)),
    )


def _file_from_payload(payload: object) -> ArchiveFileRecord:
    if not isinstance(payload, dict):
        raise ArchiveBackendError("archive file payload is invalid")
    return ArchiveFileRecord(
        path=str(payload["path"]),
        session_id=str(payload["session_id"]),
        bytes=int(payload["bytes"]),
        sha256=str(payload["sha256"]),
        start_at=payload.get("start_at") if isinstance(payload.get("start_at"), str) else None,
        end_at=payload.get("end_at") if isinstance(payload.get("end_at"), str) else None,
        time_source=str(payload.get("time_source", "none")),
        parse_status=str(payload.get("parse_status", "opaque")),
        member=payload.get("member") if isinstance(payload.get("member"), str) else None,
    )


def _manifest_from_payload(payload: object) -> ArchiveManifest:
    if not isinstance(payload, dict):
        raise ArchiveBackendError("archive manifest payload is invalid")
    files = tuple(_file_from_payload(item) for item in payload.get("files", []) if isinstance(item, dict))
    return ArchiveManifest(
        snapshot_id=str(payload["snapshot_id"]),
        source=_source_from_payload(payload["source"]),
        files=files,
        restic_snapshot_id=payload.get("restic_snapshot_id") if isinstance(payload.get("restic_snapshot_id"), str) else None,
        deleted_paths=tuple(item for item in payload.get("deleted_paths", []) if isinstance(item, str)),
        generated_at=str(payload.get("generated_at", "")),
    )

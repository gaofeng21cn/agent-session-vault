from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import uuid

from .archive import sha256_file, verify_bundle_members
from .archive_model import ArchiveBundle, ArchiveManifest, ArchiveSnapshot, payload_sha256


ARCHIVE_FORMAT = "agent-session-vault-archive-v1"
ROOT_MARKER_NAME = "root-marker.json"
COMMIT_MARKER_NAME = "COMMITTED"


class ArchiveBackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublishedSnapshot:
    snapshot_dir: Path
    snapshot: ArchiveSnapshot
    manifests: tuple[ArchiveManifest, ...]


class FilesystemArchiveBackend:
    """Filesystem backend used for local paths, NAS mounts, and OneDrive paths.

    The backend deliberately treats every target as an explicit directory. It
    never follows a symlink for the vault root and never overwrites a committed
    snapshot or an existing content-addressed object.
    """

    def __init__(self, root: Path):
        self.root = root.expanduser()

    @property
    def marker_path(self) -> Path:
        return self.root / ROOT_MARKER_NAME

    def initialize(self, vault_id: str | None = None) -> dict[str, object]:
        if self.root.exists() and self.root.is_symlink():
            raise ArchiveBackendError(f"archive root must not be a symlink: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.marker_path.exists():
            payload = self._read_json(self.marker_path)
            if payload.get("format") != ARCHIVE_FORMAT:
                raise ArchiveBackendError(f"archive root marker has an unexpected format: {self.marker_path}")
            return payload
        payload = {
            "format": ARCHIVE_FORMAT,
            "schema_version": 1,
            "vault_id": vault_id or f"vault-{uuid.uuid4().hex}",
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._write_json_atomic(self.marker_path, payload)
        for name in ("objects", "snapshots", "receipts", "staging"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        return payload

    def ensure_ready(self) -> dict[str, object]:
        if not self.root.exists() or not self.root.is_dir() or self.root.is_symlink():
            raise ArchiveBackendError(f"archive root is not an ordinary directory: {self.root}")
        if not self.marker_path.is_file():
            raise ArchiveBackendError(f"archive root marker is missing: {self.marker_path}")
        marker = self._read_json(self.marker_path)
        if marker.get("format") != ARCHIVE_FORMAT:
            raise ArchiveBackendError(f"archive root marker has an unexpected format: {self.marker_path}")
        return marker

    def object_path(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ValueError(f"invalid object sha256: {sha256}")
        return self.root / "objects" / f"{sha256}.tar.zst"

    def publish_object(self, local_path: Path, sha256: str) -> Path:
        self.ensure_ready()
        source = local_path.expanduser()
        if not source.is_file():
            raise FileNotFoundError(source)
        destination = self.object_path(sha256)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.stat().st_size != source.stat().st_size or sha256_file(destination) != sha256:
                raise ArchiveBackendError(f"object collision with a different payload: {destination}")
            return destination
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.upload")
        try:
            shutil.copyfile(source, temporary)
            if temporary.stat().st_size != source.stat().st_size or sha256_file(temporary) != sha256:
                raise ArchiveBackendError(f"object verification failed before publish: {destination}")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def publish_snapshot(
        self,
        snapshot: ArchiveSnapshot,
        manifests: tuple[ArchiveManifest, ...],
        staged_objects: dict[str, Path],
    ) -> PublishedSnapshot:
        self.ensure_ready()
        for bundle_id, local_path in staged_objects.items():
            sha256 = bundle_id.removeprefix("bundle-")
            self.publish_object(local_path, sha256)

        snapshot_dir = self.root / "snapshots" / snapshot.machine_id / snapshot.snapshot_id
        if snapshot_dir.exists():
            if (snapshot_dir / COMMIT_MARKER_NAME).is_file():
                existing = self.load_snapshot(snapshot_dir)
                return existing
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
                "objects": {
                    bundle_id: sha256 for bundle_id in staged_objects for sha256 in [bundle_id.removeprefix("bundle-")]
                },
            }
            self._write_json_atomic(temporary_root / "checksums.json", checksums)
            (temporary_root / COMMIT_MARKER_NAME).write_text(datetime.now(UTC).isoformat() + "\n", encoding="utf-8")
            snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
            temporary_root.replace(snapshot_dir)
        except Exception:
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise
        return PublishedSnapshot(snapshot_dir=snapshot_dir, snapshot=committed_snapshot, manifests=manifests)

    def iter_snapshot_dirs(self, machine_id: str | None = None) -> list[Path]:
        self.ensure_ready()
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

    def verify_snapshot(self, snapshot_dir: Path, *, deep: bool = False) -> dict[str, object]:
        published = self.load_snapshot(snapshot_dir)
        checked_objects = 0
        checked_files = 0
        failures: list[str] = []
        metadata_ok = True
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
        except (OSError, KeyError, TypeError, ValueError) as exc:
            failures.append(f"metadata_verification_error:{type(exc).__name__}")
            metadata_ok = False
        if not metadata_ok:
            return {
                "status": "failed",
                "snapshot_id": published.snapshot.snapshot_id,
                "snapshot_dir": str(snapshot_dir),
                "checked_objects": 0,
                "checked_files": 0,
                "failures": failures,
                "deep": deep,
            }
        for manifest in published.manifests:
            for bundle in manifest.bundles:
                object_path = self.root / bundle.object_path
                if not object_path.is_file():
                    failures.append(f"missing_object:{bundle.bundle_id}")
                    continue
                checked_objects += 1
                if object_path.stat().st_size != bundle.bytes or sha256_file(object_path) != bundle.sha256:
                    failures.append(f"object_hash_mismatch:{bundle.bundle_id}")
                    continue
                if not deep:
                    continue
                expected = {
                    record.member: (record.bytes, record.sha256)
                    for record in manifest.files
                    if record.bundle_id == bundle.bundle_id and record.member
                }
                checked, bundle_failures = verify_bundle_members(object_path, expected)
                checked_files += checked
                failures.extend(bundle_failures)
        status = "verified" if not failures else "failed"
        return {
            "status": status,
            "snapshot_id": published.snapshot.snapshot_id,
            "snapshot_dir": str(snapshot_dir),
            "checked_objects": checked_objects,
            "checked_files": checked_files,
            "failures": failures,
            "deep": deep,
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


def _source_from_payload(payload: object):
    from .archive_model import ArchiveSource

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
        warnings=tuple(item for item in payload.get("warnings", []) if isinstance(item, str)),
    )


def _file_from_payload(payload: object):
    from .archive_model import ArchiveFileRecord

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
        bundle_id=payload.get("bundle_id") if isinstance(payload.get("bundle_id"), str) else None,
        member=payload.get("member") if isinstance(payload.get("member"), str) else None,
    )


def _bundle_from_payload(payload: object):
    if not isinstance(payload, dict):
        raise ArchiveBackendError("archive bundle payload is invalid")
    return ArchiveBundle(
        bundle_id=str(payload["bundle_id"]),
        object_path=str(payload["object_path"]),
        sha256=str(payload["sha256"]),
        bytes=int(payload["bytes"]),
        source_bytes=int(payload.get("source_bytes", 0)),
        file_count=int(payload.get("file_count", 0)),
    )


def _manifest_from_payload(payload: object) -> ArchiveManifest:
    if not isinstance(payload, dict):
        raise ArchiveBackendError("archive manifest payload is invalid")
    files = tuple(_file_from_payload(item) for item in payload.get("files", []) if isinstance(item, dict))
    bundles = tuple(_bundle_from_payload(item) for item in payload.get("bundles", []) if isinstance(item, dict))
    return ArchiveManifest(
        snapshot_id=str(payload["snapshot_id"]),
        source=_source_from_payload(payload["source"]),
        files=files,
        bundles=bundles,
        deleted_paths=tuple(item for item in payload.get("deleted_paths", []) if isinstance(item, str)),
        generated_at=str(payload.get("generated_at", "")),
    )

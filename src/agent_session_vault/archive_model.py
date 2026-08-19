from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any


ARCHIVE_SCHEMA_VERSION = "archive.snapshot.v1"
RECEIPT_SCHEMA_VERSION = "archive.receipt.v1"


def canonical_json(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def payload_sha256(payload: object) -> str:
    return sha256_bytes(canonical_json(payload))


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe archive relative path: {value!r}")
    return path.as_posix()


@dataclass(frozen=True)
class ArchiveSource:
    source_id: str
    machine_id: str
    client: str
    kind: str
    root_path: str
    relative_root: str
    label: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "machine_id": self.machine_id,
            "client": self.client,
            "kind": self.kind,
            "root_path": self.root_path,
            "relative_root": self.relative_root,
            "label": self.label,
        }


@dataclass(frozen=True)
class ArchiveFileRecord:
    path: str
    session_id: str
    bytes: int
    sha256: str
    start_at: str | None
    end_at: str | None
    time_source: str
    parse_status: str
    bundle_id: str | None = None
    member: str | None = None

    def __post_init__(self) -> None:
        validate_relative_path(self.path)
        if self.bytes < 0:
            raise ValueError("archive file bytes must be non-negative")

    def to_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "session_id": self.session_id,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "time_source": self.time_source,
            "parse_status": self.parse_status,
            "bundle_id": self.bundle_id,
            "member": self.member,
        }


@dataclass(frozen=True)
class ArchiveBundle:
    bundle_id: str
    object_path: str
    sha256: str
    bytes: int
    source_bytes: int
    file_count: int

    def to_payload(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "object_path": self.object_path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "source_bytes": self.source_bytes,
            "file_count": self.file_count,
        }


@dataclass(frozen=True)
class ArchiveSnapshot:
    snapshot_id: str
    cycle_id: str
    machine_id: str
    source: ArchiveSource
    captured_at: str
    consistency: str
    status: str
    parent_snapshot_id: str | None
    manifest_sha256: str | None = None
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "cycle_id": self.cycle_id,
            "machine_id": self.machine_id,
            "source": self.source.to_payload(),
            "captured_at": self.captured_at,
            "consistency": self.consistency,
            "status": self.status,
            "parent_snapshot_id": self.parent_snapshot_id,
            "manifest_sha256": self.manifest_sha256,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ArchiveManifest:
    snapshot_id: str
    source: ArchiveSource
    files: tuple[ArchiveFileRecord, ...]
    bundles: tuple[ArchiveBundle, ...]
    deleted_paths: tuple[str, ...] = ()
    generated_at: str = field(default_factory=utc_now)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "source": self.source.to_payload(),
            "generated_at": self.generated_at,
            "files": [item.to_payload() for item in self.files],
            "bundles": [item.to_payload() for item in self.bundles],
            "deleted_paths": [validate_relative_path(item) for item in self.deleted_paths],
        }

    @property
    def sha256(self) -> str:
        return payload_sha256(self.to_payload())


@dataclass(frozen=True)
class CatalogEntry:
    machine_id: str
    source_id: str
    client: str
    session_id: str
    snapshot_id: str
    path: str
    start_at: str | None
    end_at: str | None
    bytes: int
    sha256: str
    parse_status: str

    def to_payload(self) -> dict[str, object]:
        return {
            "machine_id": self.machine_id,
            "source_id": self.source_id,
            "client": self.client,
            "session_id": self.session_id,
            "snapshot_id": self.snapshot_id,
            "path": self.path,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "parse_status": self.parse_status,
        }


@dataclass(frozen=True)
class RestorePlan:
    plan_id: str
    created_at: str
    mode: str
    as_of_snapshot_id: str | None
    from_at: str | None
    to_at: str | None
    destination: str
    collision_policy: str
    entries: tuple[CatalogEntry, ...]
    plan_digest: str

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": "archive.restore-plan.v1",
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "mode": self.mode,
            "as_of_snapshot_id": self.as_of_snapshot_id,
            "from_at": self.from_at,
            "to_at": self.to_at,
            "destination": self.destination,
            "collision_policy": self.collision_policy,
            "entries": [entry.to_payload() for entry in self.entries],
            "plan_digest": self.plan_digest,
        }


@dataclass(frozen=True)
class ArchiveReceipt:
    operation_id: str
    operation: str
    status: str
    started_at: str
    finished_at: str
    machine_id: str
    source_id: str | None
    snapshot_id: str | None
    details: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "operation": self.operation,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "machine_id": self.machine_id,
            "source_id": self.source_id,
            "snapshot_id": self.snapshot_id,
            "details": self.details,
        }

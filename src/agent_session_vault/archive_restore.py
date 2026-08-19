from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import uuid

from .archive import restore_bundle_member
from .archive_backend import FilesystemArchiveBackend
from .archive_catalog import query_catalog
from .archive_model import ArchiveBundle, ArchiveFileRecord, CatalogEntry, RestorePlan, payload_sha256, validate_relative_path
from .config import VaultConfig


def _plan_id() -> str:
    return f"plan-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"


def build_restore_plan(
    config: VaultConfig,
    *,
    destination: Path,
    from_at: str | None = None,
    to_at: str | None = None,
    machine_id: str | None = None,
    client: str | None = "codex",
    session_id: str | None = None,
    source_id: str | None = None,
    collision_policy: str = "error",
) -> RestorePlan:
    if collision_policy not in {"error", "overwrite"}:
        raise ValueError(f"unsupported collision policy: {collision_policy}")
    entries = tuple(
        query_catalog(
            config,
            from_at=from_at,
            to_at=to_at,
            machine_id=machine_id,
            client=client,
            session_id=session_id,
            source_id=source_id,
        )
    )
    plan = RestorePlan(
        plan_id=_plan_id(),
        created_at=datetime.now(UTC).isoformat(),
        mode="staging",
        as_of_snapshot_id=None,
        from_at=from_at,
        to_at=to_at,
        destination=str(destination.expanduser()),
        collision_policy=collision_policy,
        entries=entries,
        plan_digest="",
    )
    plan_digest = payload_sha256({key: value for key, value in plan.to_payload().items() if key != "plan_digest"})
    return replace(plan, plan_digest=plan_digest)


def write_restore_plan(plan: RestorePlan, path: Path) -> Path:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(plan.to_payload(), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def load_restore_plan(path: Path) -> RestorePlan:
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "archive.restore-plan.v1":
        raise ValueError(f"invalid restore plan: {path}")
    entries = tuple(_entry_from_payload(item) for item in payload.get("entries", []) if isinstance(item, dict))
    plan = RestorePlan(
        plan_id=str(payload["plan_id"]),
        created_at=str(payload["created_at"]),
        mode=str(payload["mode"]),
        as_of_snapshot_id=payload.get("as_of_snapshot_id") if isinstance(payload.get("as_of_snapshot_id"), str) else None,
        from_at=payload.get("from_at") if isinstance(payload.get("from_at"), str) else None,
        to_at=payload.get("to_at") if isinstance(payload.get("to_at"), str) else None,
        destination=str(payload["destination"]),
        collision_policy=str(payload["collision_policy"]),
        entries=entries,
        plan_digest=str(payload["plan_digest"]),
    )
    if plan.mode != "staging":
        raise ValueError("restore plan mode must be staging")
    expected = payload_sha256({key: value for key, value in plan.to_payload().items() if key != "plan_digest"})
    if expected != plan.plan_digest:
        raise ValueError(f"restore plan digest mismatch: {path}")
    return plan


def restore_plan(config: VaultConfig, plan: RestorePlan) -> dict[str, object]:
    destination = Path(plan.destination).expanduser()
    backend = FilesystemArchiveBackend(config.archive.root)
    backend.ensure_ready()
    snapshots = {path.name: backend.load_snapshot(path) for path in backend.iter_snapshot_dirs()}
    records_by_entry: dict[tuple[str, str], tuple[CatalogEntry, ArchiveFileRecord, ArchiveBundle]] = {}
    for entry in plan.entries:
        published = snapshots.get(entry.snapshot_id)
        if published is None:
            raise ValueError(f"snapshot referenced by restore plan is missing: {entry.snapshot_id}")
        record = next(
            (
                record
                for manifest in published.manifests
                for record in manifest.files
                if record.path == entry.path
            ),
            None,
        )
        if record is None or not record.bundle_id or not record.member:
            raise ValueError(f"restore entry has no bundle member: {entry.path}")
        bundle = next(
            (
                bundle
                for manifest in published.manifests
                for bundle in manifest.bundles
                if bundle.bundle_id == record.bundle_id
            ),
            None,
        )
        if bundle is None:
            raise ValueError(f"restore bundle is missing from manifest: {record.bundle_id}")
        records_by_entry[(entry.snapshot_id, entry.path)] = (entry, record, bundle)
    restored: list[dict[str, object]] = []
    destination.mkdir(parents=True, exist_ok=True)
    restored_by_digest: dict[tuple[int, str], Path] = {}
    ordered = sorted(
        records_by_entry.values(),
        key=lambda item: (0 if str(item[1].member).startswith("sessions/") else 1, item[0].path),
    )
    for entry, record, bundle in ordered:
        member = Path(str(record.member))
        if member.is_absolute() or ".." in member.parts:
            raise ValueError(f"unsafe restore member: {record.member}")
        relative = Path(validate_relative_path(entry.path))
        target = destination / relative
        if target.exists() and plan.collision_policy == "error":
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest_key = (entry.bytes, entry.sha256)
        source = restored_by_digest.get(digest_key)
        if source is not None:
            shutil.copy2(source, target)
        else:
            actual_bytes, actual_sha256 = restore_bundle_member(
                backend.root / bundle.object_path,
                str(record.member),
                target,
            )
            if (actual_bytes, actual_sha256) != digest_key:
                target.unlink(missing_ok=True)
                raise ValueError(f"restored member checksum mismatch: {entry.path}")
            restored_by_digest[digest_key] = target
        restored.append({"path": entry.path, "destination": str(target), "sha256": entry.sha256})
    return {
        "status": "verified",
        "plan_id": plan.plan_id,
        "destination": str(destination),
        "restored_files": len(restored),
        "restored": restored,
    }


def _entry_from_payload(payload: object) -> CatalogEntry:
    if not isinstance(payload, dict):
        raise ValueError("restore plan entry is invalid")
    return CatalogEntry(
        machine_id=str(payload["machine_id"]),
        source_id=str(payload["source_id"]),
        client=str(payload["client"]),
        session_id=str(payload["session_id"]),
        snapshot_id=str(payload["snapshot_id"]),
        path=str(payload["path"]),
        start_at=payload.get("start_at") if isinstance(payload.get("start_at"), str) else None,
        end_at=payload.get("end_at") if isinstance(payload.get("end_at"), str) else None,
        bytes=int(payload["bytes"]),
        sha256=str(payload["sha256"]),
        parse_status=str(payload.get("parse_status", "opaque")),
    )

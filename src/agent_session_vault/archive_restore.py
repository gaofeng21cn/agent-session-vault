from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from .archive import sha256_file
from .archive_backend import _member_from_record
from .archive_catalog import query_catalog
from .archive_model import ArchiveFileRecord, CatalogEntry, RestorePlan, payload_sha256, validate_relative_path
from .archive_ops import archive_backend
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
    backend = archive_backend(config)
    backend.ensure_writable()
    backend.ensure_ready()
    destination = Path(plan.destination).expanduser()
    snapshots = {path.name: backend.load_verified_metadata(path) for path in backend.iter_snapshot_dirs()}

    grouped: dict[str, list[tuple[CatalogEntry, ArchiveFileRecord, str, Path]]] = defaultdict(list)
    for entry in plan.entries:
        published = snapshots.get(entry.snapshot_id)
        if published is None:
            raise ValueError(f"snapshot referenced by restore plan is missing: {entry.snapshot_id}")
        restic_id = published.snapshot.restic_snapshot_id
        if not restic_id:
            raise ValueError(f"snapshot has no restic snapshot binding: {entry.snapshot_id}")
        matched = next(
            (
                (manifest, record)
                for manifest in published.manifests
                for record in manifest.files
                if record.path == entry.path
            ),
            None,
        )
        if matched is None:
            raise ValueError(f"restore entry is missing from snapshot manifest: {entry.path}")
        manifest, record = matched
        member = validate_relative_path(record.member or _member_from_record(manifest.source, record))
        relative = Path(validate_relative_path(entry.path))
        target = destination / relative
        if target.exists() and plan.collision_policy == "error":
            raise FileExistsError(target)
        grouped[restic_id].append((entry, record, member, target))

    restored: list[dict[str, object]] = []
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="asv-restic-restore-") as staging_dir:
        staging_root = Path(staging_dir)
        for index, (restic_id, items) in enumerate(sorted(grouped.items())):
            batch_root = staging_root / f"batch-{index}"
            members = tuple(sorted({member for _, _, member, _ in items}))
            backend.restore_entries(restic_id, members, batch_root)
            for entry, _, member, target in items:
                extracted = batch_root / Path(member)
                if not extracted.is_file() or extracted.is_symlink():
                    raise ValueError(f"restored restic member is missing or not a regular file: {member}")
                actual = (extracted.stat().st_size, sha256_file(extracted))
                if actual != (entry.bytes, entry.sha256):
                    raise ValueError(f"restored member checksum mismatch: {entry.path}")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{os.getpid()}.restore")
                try:
                    shutil.copy2(extracted, temporary)
                    temporary.replace(target)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
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

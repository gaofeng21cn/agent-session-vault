from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
from typing import Callable
import uuid

from .archive import sha256_file
from .archive_backend import FilesystemArchiveBackend
from .archive_model import ArchiveManifest, ArchiveSnapshot, payload_sha256
from .archive_ops import archive_backend, receipt
from .archive_sources import scan_codex_sources
from .config import VaultConfig
from .daily_ops import _parse_tokscale_stats
from .stable import default_stable_root
from .stable_pack import snapshot_directory
from .tokscale import build_tokscale_invocation


PRUNE_PLAN_SCHEMA = "archive.prune-plan.v1"
_LOCAL_HOME_PROJECTION_STATE = ".local-home-projection-state.json"


@dataclass(frozen=True)
class PruneEntry:
    source_path: str
    archive_path: str
    snapshot_id: str
    sha256: str
    bytes: int
    mtime_ns: int
    device: int
    inode: int
    nlink: int
    age_basis: str
    age_at: str
    projection_path: str

    def to_payload(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "archive_path": self.archive_path,
            "snapshot_id": self.snapshot_id,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "mtime_ns": self.mtime_ns,
            "device": self.device,
            "inode": self.inode,
            "nlink": self.nlink,
            "age_basis": self.age_basis,
            "age_at": self.age_at,
            "projection_path": self.projection_path,
        }


@dataclass(frozen=True)
class PrunePlan:
    plan_id: str
    created_at: str
    machine_id: str
    cold_age_days: int
    stable_root: str
    imports_fingerprint: str
    tokscale_preview: dict[str, object]
    entries: tuple[PruneEntry, ...]
    skipped: dict[str, int]
    plan_digest: str

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": PRUNE_PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "machine_id": self.machine_id,
            "cold_age_days": self.cold_age_days,
            "stable_root": self.stable_root,
            "imports_fingerprint": self.imports_fingerprint,
            "tokscale_preview": self.tokscale_preview,
            "entries": [entry.to_payload() for entry in self.entries],
            "skipped": self.skipped,
            "plan_digest": self.plan_digest,
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _latest_manifest(
    backend: FilesystemArchiveBackend,
    source_id: str,
) -> tuple[ArchiveSnapshot, ArchiveManifest] | None:
    candidates: list[tuple[str, ArchiveSnapshot, ArchiveManifest]] = []
    for snapshot_dir in backend.iter_snapshot_dirs():
        published = backend.load_snapshot(snapshot_dir)
        for manifest in published.manifests:
            if manifest.source.source_id == source_id:
                candidates.append((published.snapshot.captured_at, published.snapshot, manifest))
    if not candidates:
        return None
    _, snapshot, manifest = max(candidates, key=lambda item: item[0])
    return snapshot, manifest


def _archive_relative(root: Path, source_path: Path) -> str | None:
    try:
        relative = source_path.relative_to(root)
    except ValueError:
        return None
    if not relative.parts or relative.parts[0] != "archived_sessions":
        return None
    return relative.as_posix()


def _projection_state_path(config: VaultConfig) -> Path:
    return config.paths.import_root / "local-home" / _LOCAL_HOME_PROJECTION_STATE


def _load_projection_files(config: VaultConfig) -> dict[str, dict[str, object]]:
    path = _projection_state_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("status") != "valid":
        return {}
    files = payload.get("files")
    if not isinstance(files, dict):
        return {}
    return {key: value for key, value in files.items() if isinstance(key, str) and isinstance(value, dict)}


def _stable_imports_fingerprint(config: VaultConfig) -> tuple[Path, str]:
    root = default_stable_root(config).expanduser()
    manifest_path = root / "stable-layer-manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"stable analytics manifest is unavailable: {manifest_path}") from exc
    if not isinstance(payload, dict) or payload.get("status") != "verified":
        raise ValueError("stable analytics mirror is not verified")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("stable analytics manifest has no items")
    imports = next((item for item in items if isinstance(item, dict) and item.get("label") == "imports"), None)
    if not isinstance(imports, dict) or imports.get("coverage_status") != "verified":
        raise ValueError("stable analytics imports coverage is not verified")
    expected = imports.get("source_manifest_fingerprint")
    if not isinstance(expected, str) or not expected:
        raise ValueError("stable analytics imports fingerprint is missing")
    actual = snapshot_directory(config.paths.import_root).fingerprint
    if actual != expected:
        raise ValueError("stable analytics imports are stale; mirror the current projection before pruning")
    return root, actual


def _tokscale_contract_path(config: VaultConfig) -> Path:
    return config.config_path.parent / "ops" / "daily-tokscale" / "submit-contract.json"


def _tokscale_preview(config: VaultConfig) -> dict[str, object]:
    contract_path = _tokscale_contract_path(config)
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Tokscale submit contract is unavailable: {contract_path}") from exc
    if not isinstance(contract, dict) or contract.get("dry_run") is not True:
        raise ValueError("Tokscale submit contract is not verified for dry-run")
    version = contract.get("tokscale_version")
    client_args = contract.get("client_args")
    clients = contract.get("clients")
    if (
        not isinstance(version, str)
        or not isinstance(client_args, list)
        or not all(isinstance(item, str) for item in client_args)
        or not isinstance(clients, list)
        or not all(isinstance(item, str) for item in clients)
    ):
        raise ValueError("Tokscale submit contract is invalid")
    package = f"tokscale@{version}"
    invocation = build_tokscale_invocation(
        config,
        args=["submit", *client_args, "--dry-run"],
        package_override=package,
    )
    completed = subprocess.run(
        invocation.command,
        env=invocation.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=3600,
        check=False,
    )
    output = completed.stdout or ""
    statistics = _parse_tokscale_stats(output)
    if completed.returncode != 0 or statistics is None or "Dry run - not submitting data." not in output:
        raise ValueError(f"Tokscale dry-run did not return a complete receipt (exit={completed.returncode})")
    return {
        "package": package,
        "clients": clients,
        "client_args": client_args,
        "statistics": statistics,
    }


def _preview_matches(expected: dict[str, object], actual: dict[str, object]) -> bool:
    return expected == actual


def _plan_digest(plan: PrunePlan) -> str:
    return payload_sha256({key: value for key, value in plan.to_payload().items() if key != "plan_digest"})


def _entry_from_payload(payload: object) -> PruneEntry:
    if not isinstance(payload, dict):
        raise ValueError("prune plan entry is invalid")
    return PruneEntry(
        source_path=str(payload["source_path"]),
        archive_path=str(payload["archive_path"]),
        snapshot_id=str(payload["snapshot_id"]),
        sha256=str(payload["sha256"]),
        bytes=int(payload["bytes"]),
        mtime_ns=int(payload["mtime_ns"]),
        device=int(payload["device"]),
        inode=int(payload["inode"]),
        nlink=int(payload["nlink"]),
        age_basis=str(payload["age_basis"]),
        age_at=str(payload["age_at"]),
        projection_path=str(payload["projection_path"]),
    )


def write_prune_plan(plan: PrunePlan, path: Path) -> Path:
    target = path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(plan.to_payload(), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def load_prune_plan(path: Path) -> PrunePlan:
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != PRUNE_PLAN_SCHEMA:
        raise ValueError(f"invalid prune plan: {path}")
    entries = tuple(_entry_from_payload(item) for item in payload.get("entries", []) if isinstance(item, dict))
    skipped = payload.get("skipped")
    if not isinstance(skipped, dict) or not all(isinstance(key, str) and isinstance(value, int) for key, value in skipped.items()):
        raise ValueError(f"invalid prune plan skips: {path}")
    preview = payload.get("tokscale_preview")
    if not isinstance(preview, dict):
        raise ValueError(f"invalid prune plan Tokscale preview: {path}")
    plan = PrunePlan(
        plan_id=str(payload["plan_id"]),
        created_at=str(payload["created_at"]),
        machine_id=str(payload["machine_id"]),
        cold_age_days=int(payload["cold_age_days"]),
        stable_root=str(payload["stable_root"]),
        imports_fingerprint=str(payload["imports_fingerprint"]),
        tokscale_preview=preview,
        entries=entries,
        skipped={key: value for key, value in skipped.items()},
        plan_digest=str(payload["plan_digest"]),
    )
    if plan.plan_digest != _plan_digest(plan):
        raise ValueError(f"prune plan digest mismatch: {path}")
    return plan


def build_prune_plan(
    config: VaultConfig,
    *,
    now: datetime | None = None,
    preview_runner: Callable[[VaultConfig], dict[str, object]] = _tokscale_preview,
) -> PrunePlan:
    now = (now or _utc_now()).astimezone(UTC)
    backend = archive_backend(config)
    backend.ensure_ready()
    stable_root, imports_fingerprint = _stable_imports_fingerprint(config)
    projection_files = _load_projection_files(config)
    if not projection_files:
        raise ValueError("local Tokscale projection state is unavailable")
    scan = scan_codex_sources(config)
    if config.archive.require_quiescent_for_prune and any(source.warnings for source in scan.sources):
        raise ValueError("Codex sources changed while scanning; wait for writers to become quiescent")
    cutoff = now - timedelta(days=config.archive.cold_age_days)
    skipped: Counter[str] = Counter()
    entries: list[PruneEntry] = []

    for source in scan.sources:
        latest = _latest_manifest(backend, source.source.source_id)
        if latest is None:
            skipped["snapshot_missing"] += len(source.files)
            continue
        snapshot, manifest = latest
        verification = backend.verify_snapshot(
            next(path for path in backend.iter_snapshot_dirs() if path.name == snapshot.snapshot_id),
            deep=True,
        )
        if verification.get("status") != "verified":
            raise ValueError(f"latest archive snapshot is not deeply verified: {snapshot.snapshot_id}")
        manifest_files = {record.path: record for record in manifest.files}
        root = Path(source.source.root_path)
        archived_stats: dict[Path, os.stat_result] = {}
        archive_inode_counts: Counter[tuple[int, int]] = Counter()
        session_inodes: set[tuple[int, int]] = set()
        for item in source.files:
            relative = _archive_relative(root, item.source_path)
            try:
                stat = item.source_path.stat()
            except OSError:
                skipped["source_missing"] += 1
                continue
            inode = (stat.st_dev, stat.st_ino)
            if relative is None:
                session_inodes.add(inode)
            else:
                archived_stats[item.source_path] = stat
                archive_inode_counts[inode] += 1

        for item in source.files:
            relative = _archive_relative(root, item.source_path)
            if relative is None:
                continue
            stat = archived_stats.get(item.source_path)
            if stat is None:
                continue
            archived_record = manifest_files.get(item.record.path)
            if archived_record is None or archived_record.sha256 != item.record.sha256:
                skipped["snapshot_coverage_missing"] += 1
                continue
            event_time = _parse_time(item.record.end_at)
            age_at = event_time or datetime.fromtimestamp(stat.st_mtime, UTC)
            age_basis = "session_end" if event_time else "filesystem_mtime"
            if age_at > cutoff:
                skipped["not_cold"] += 1
                continue
            inode = (stat.st_dev, stat.st_ino)
            if inode in session_inodes:
                skipped["shared_with_sessions"] += 1
                continue
            if stat.st_nlink != archive_inode_counts[inode]:
                skipped["external_hardlink"] += 1
                continue
            projection = projection_files.get(str(item.source_path.resolve()))
            projection_path = projection.get("destination") if projection else None
            if not isinstance(projection_path, str) or not Path(projection_path).is_file():
                skipped["projection_missing"] += 1
                continue
            entries.append(
                PruneEntry(
                    source_path=str(item.source_path),
                    archive_path=item.record.path,
                    snapshot_id=snapshot.snapshot_id,
                    sha256=item.record.sha256,
                    bytes=item.record.bytes,
                    mtime_ns=stat.st_mtime_ns,
                    device=stat.st_dev,
                    inode=stat.st_ino,
                    nlink=stat.st_nlink,
                    age_basis=age_basis,
                    age_at=_iso(age_at),
                    projection_path=projection_path,
                )
            )

    preview = preview_runner(config)
    draft = PrunePlan(
        plan_id=f"prune-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}",
        created_at=_iso(now),
        machine_id=scan.machine_id,
        cold_age_days=config.archive.cold_age_days,
        stable_root=str(stable_root),
        imports_fingerprint=imports_fingerprint,
        tokscale_preview=preview,
        entries=tuple(sorted(entries, key=lambda entry: entry.source_path)),
        skipped=dict(sorted(skipped.items())),
        plan_digest="",
    )
    return replace(draft, plan_digest=_plan_digest(draft))


def prune_plan_payload(plan: PrunePlan, *, plan_path: Path | None = None) -> dict[str, object]:
    bytes_total = sum(entry.bytes for entry in plan.entries)
    return {
        **plan.to_payload(),
        "plan_path": str(plan_path) if plan_path else None,
        "eligible_file_count": len(plan.entries),
        "eligible_bytes": bytes_total,
        "eligible_gib": round(bytes_total / 1024 / 1024 / 1024, 3),
    }


def _verify_entry(entry: PruneEntry) -> None:
    path = Path(entry.source_path)
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"prune source is missing: {path}") from exc
    expected = (entry.bytes, entry.mtime_ns, entry.device, entry.inode, entry.nlink)
    actual = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino, stat.st_nlink)
    if actual != expected:
        raise ValueError(f"prune source metadata changed: {path}")
    if sha256_file(path) != entry.sha256:
        raise ValueError(f"prune source checksum changed: {path}")
    projection = Path(entry.projection_path)
    if not projection.is_file():
        raise ValueError(f"Tokscale projection is missing: {projection}")


def _verify_plan_snapshots(config: VaultConfig, plan: PrunePlan) -> None:
    backend = archive_backend(config)
    snapshot_dirs = {path.name: path for path in backend.iter_snapshot_dirs()}
    for snapshot_id in sorted({entry.snapshot_id for entry in plan.entries}):
        snapshot_dir = snapshot_dirs.get(snapshot_id)
        if snapshot_dir is None:
            raise ValueError(f"prune snapshot is missing: {snapshot_id}")
        verification = backend.verify_snapshot(snapshot_dir, deep=True)
        if verification.get("status") != "verified":
            raise ValueError(f"prune snapshot is no longer deeply verified: {snapshot_id}")


def apply_prune_plan(
    config: VaultConfig,
    plan: PrunePlan,
    *,
    preview_runner: Callable[[VaultConfig], dict[str, object]] = _tokscale_preview,
) -> dict[str, object]:
    if plan.plan_digest != _plan_digest(plan):
        raise ValueError("prune plan digest mismatch")
    stable_root, imports_fingerprint = _stable_imports_fingerprint(config)
    if str(stable_root) != plan.stable_root or imports_fingerprint != plan.imports_fingerprint:
        raise ValueError("stable analytics coverage changed; generate a new prune plan")
    _verify_plan_snapshots(config, plan)
    before_preview = preview_runner(config)
    if not _preview_matches(plan.tokscale_preview, before_preview):
        raise ValueError("Tokscale dry-run changed; generate a new prune plan")
    for entry in plan.entries:
        _verify_entry(entry)
    deleted: list[str] = []
    deleted_bytes = 0
    try:
        for entry in plan.entries:
            Path(entry.source_path).unlink()
            deleted.append(entry.source_path)
            deleted_bytes += entry.bytes
        after_preview = preview_runner(config)
        if not _preview_matches(before_preview, after_preview):
            raise ValueError("Tokscale parity failed after prune; restore the deleted sessions from the recorded snapshot")
    except Exception as exc:
        receipt(
            config,
            "archive-prune",
            "partial" if deleted else "failed",
            machine_id=plan.machine_id,
            details={
                "plan_id": plan.plan_id,
                "plan_digest": plan.plan_digest,
                "deleted_file_count": len(deleted),
                "deleted_bytes": deleted_bytes,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise
    item = receipt(
        config,
        "archive-prune",
        "verified",
        machine_id=plan.machine_id,
        details={
            "plan_id": plan.plan_id,
            "plan_digest": plan.plan_digest,
            "deleted_file_count": len(deleted),
            "deleted_bytes": deleted_bytes,
            "tokscale_preview": after_preview,
        },
    )
    return {
        "status": "verified",
        "plan_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
        "deleted_file_count": len(deleted),
        "deleted_bytes": deleted_bytes,
        "deleted_gib": round(deleted_bytes / 1024 / 1024 / 1024, 3),
        "tokscale_preview": after_preview,
        "receipt_path": str(archive_backend(config).root / "receipts" / plan.machine_id / f"{item.operation_id}.json"),
    }

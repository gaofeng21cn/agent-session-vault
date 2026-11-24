"""Deterministic filesystem-backed fake ResticRepository for fast unit tests."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import uuid
from typing import Any

from agent_session_vault.restic import (
    ResticBackupSummary,
    ResticCheckResult,
    ResticError,
    ResticNode,
    ResticSnapshotInfo,
    ResticStats,
    ensure_password_file,
    has_password_env,
)


class FakeResticRepository:
    """Faithful local-disk substitute for ResticRepository used by unit tests.

    Stores content-addressed blobs and snapshot metadata strictly inside
    ``repo_path`` so ``shutil.copytree`` and read-only tree checks behave
    identically to a real restic repository without scrypt KDF latency.
    """

    def __init__(
        self,
        repo_path: Path,
        *,
        password_file: Path | None = None,
        cache_dir: Path | None = None,
        binary: str | None = None,
    ) -> None:
        self.repo_path = repo_path.expanduser()
        self.password_file = password_file.expanduser() if password_file is not None else None
        self.cache_dir = cache_dir.expanduser() if cache_dir is not None else None
        self.binary = binary

    def exists(self) -> bool:
        return (self.repo_path / "config").is_file()

    def _require_credentials(self, *, create: bool = False) -> None:
        if has_password_env():
            return
        if self.password_file is None:
            raise ResticError("restic credentials are not configured")
        if create:
            ensure_password_file(self.password_file)
        elif not self.password_file.is_file():
            raise ResticError(f"restic password file is missing: {self.password_file}")

    def init(self) -> dict[str, object]:
        self._require_credentials(create=True)
        if self.exists():
            cfg = self.cat_config()
            return {"status": "existing", "repository": str(self.repo_path), "id": str(cfg["id"])}
        self.repo_path.mkdir(parents=True, exist_ok=True)
        (self.repo_path / "data").mkdir(exist_ok=True)
        (self.repo_path / "snapshots").mkdir(exist_ok=True)
        repo_id = hashlib.sha256(f"fake-{uuid.uuid4().hex}".encode()).hexdigest()
        (self.repo_path / "config").write_text(
            json.dumps({"version": 2, "id": repo_id}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {"status": "initialized", "repository": str(self.repo_path), "id": repo_id}

    def cat_config(self) -> dict[str, Any]:
        self._require_credentials()
        return json.loads((self.repo_path / "config").read_text(encoding="utf-8"))

    def backup(
        self,
        cwd: Path,
        relative_paths: list[str],
        *,
        hostname: str,
        tags: tuple[str, ...] = (),
        parent: str | None = None,
    ) -> ResticBackupSummary:
        self._require_credentials()
        if not self.exists():
            raise ResticError(f"repository does not exist: {self.repo_path}")
        if not relative_paths:
            raise ValueError("restic backup requires at least one relative path")

        data_dir = self.repo_path / "data"
        data_dir.mkdir(exist_ok=True)
        snaps_dir = self.repo_path / "snapshots"
        snaps_dir.mkdir(exist_ok=True)

        entries: dict[str, dict[str, object]] = {}
        data_blobs = 0
        data_added = 0
        total_bytes = 0
        for rel in relative_paths:
            source = cwd / Path(rel)
            if not source.is_file() or source.is_symlink():
                raise ResticError(f"backup source missing or not a regular file: {source}")
            payload = source.read_bytes()
            blob_id = hashlib.sha256(payload).hexdigest()
            blob_path = data_dir / blob_id
            if not blob_path.exists():
                blob_path.write_bytes(payload)
                data_blobs += 1
                data_added += len(payload)
            total_bytes += len(payload)
            entries[rel.lstrip("/")] = {"size": len(payload), "blob": blob_id}

        snap_id = hashlib.sha256(f"{hostname}:{uuid.uuid4().hex}".encode()).hexdigest()
        now = datetime.now(UTC).isoformat()
        snap_payload = {
            "id": snap_id,
            "short_id": snap_id[:8],
            "time": now,
            "hostname": hostname,
            "tags": list(tags),
            "paths": [str((cwd / Path(rel))) for rel in relative_paths],
            "parent": parent,
            "tree": hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest(),
            "entries": entries,
            "summary": {
                "data_blobs": data_blobs,
                "data_added": data_added,
                "data_added_packed": data_added,
                "total_files_processed": len(entries),
                "total_bytes_processed": total_bytes,
            },
        }
        (snaps_dir / f"{snap_id}.json").write_text(
            json.dumps(snap_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return ResticBackupSummary(
            snapshot_id=snap_id,
            files_new=data_blobs,
            files_changed=0,
            files_unmodified=len(entries) - data_blobs,
            data_blobs=data_blobs,
            tree_blobs=1 if data_blobs else 0,
            data_added=data_added,
            data_added_packed=data_added,
            total_files_processed=len(entries),
            total_bytes_processed=total_bytes,
        )

    def _load_all_snapshots(self) -> list[dict[str, Any]]:
        snaps_dir = self.repo_path / "snapshots"
        if not snaps_dir.is_dir():
            return []
        loaded: list[dict[str, Any]] = []
        for path in sorted(snaps_dir.glob("*.json")):
            loaded.append(json.loads(path.read_text(encoding="utf-8")))
        loaded.sort(key=lambda item: (str(item.get("time", "")), str(item.get("id", ""))))
        return loaded

    def snapshots(
        self,
        *,
        tags: tuple[str, ...] = (),
        snapshot_ids: tuple[str, ...] = (),
    ) -> list[ResticSnapshotInfo]:
        self._require_credentials()
        if not self.exists():
            raise ResticError(f"repository does not exist: {self.repo_path}")
        wanted_tags = set(tags)
        wanted_ids = set(snapshot_ids)
        result: list[ResticSnapshotInfo] = []
        for item in self._load_all_snapshots():
            snap_tags = set(item.get("tags") or ())
            if wanted_tags and not wanted_tags.issubset(snap_tags):
                continue
            if wanted_ids and item["id"] not in wanted_ids and item.get("short_id") not in wanted_ids:
                continue
            result.append(
                ResticSnapshotInfo(
                    id=str(item["id"]),
                    short_id=str(item.get("short_id", item["id"][:8])),
                    time=str(item.get("time", "")),
                    hostname=str(item.get("hostname", "")),
                    tags=tuple(str(t) for t in item.get("tags") or ()),
                    paths=tuple(str(p) for p in item.get("paths") or ()),
                    parent=item.get("parent"),
                    tree=item.get("tree"),
                    summary=item.get("summary"),
                )
            )
        return result

    def ls(self, snapshot_id: str) -> list[ResticNode]:
        self._require_credentials()
        path = self.repo_path / "snapshots" / f"{snapshot_id}.json"
        if not path.is_file():
            raise ResticError(f"snapshot not found: {snapshot_id}")
        item = json.loads(path.read_text(encoding="utf-8"))
        return [
            ResticNode(path="/" + rel.lstrip("/"), type="file", size=int(meta["size"]))
            for rel, meta in sorted((item.get("entries") or {}).items())
        ]

    def restore(
        self,
        snapshot_id: str,
        target: Path,
        *,
        includes: tuple[str, ...] = (),
    ) -> dict[str, object]:
        self._require_credentials()
        path = self.repo_path / "snapshots" / f"{snapshot_id}.json"
        if not path.is_file():
            raise ResticError(f"snapshot not found: {snapshot_id}")
        item = json.loads(path.read_text(encoding="utf-8"))
        entries = item.get("entries") or {}
        wanted = {inc.lstrip("/") for inc in includes} if includes else None
        target_dir = target.expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
        restored_files = 0
        restored_bytes = 0
        for rel, meta in sorted(entries.items()):
            if wanted is not None and rel not in wanted:
                continue
            blob_path = self.repo_path / "data" / str(meta["blob"])
            if not blob_path.is_file():
                raise ResticError(f"missing blob: {meta['blob']}")
            dest = target_dir / Path(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(blob_path, dest)
            restored_files += 1
            restored_bytes += int(meta["size"])
        return {
            "message_type": "summary",
            "total_files": restored_files,
            "files_restored": restored_files,
            "total_bytes": restored_bytes,
            "bytes_restored": restored_bytes,
        }

    def check(self, *, read_data: bool = False) -> ResticCheckResult:
        self._require_credentials()
        if not self.exists():
            return ResticCheckResult(False, 1, (), False, False, {"error": "missing_config"})
        broken: list[str] = []
        data_dir = self.repo_path / "data"
        for snap in self._load_all_snapshots():
            for meta in (snap.get("entries") or {}).values():
                blob_id = str(meta["blob"])
                blob_path = data_dir / blob_id
                if not blob_path.is_file():
                    broken.append(blob_id)
                elif read_data and hashlib.sha256(blob_path.read_bytes()).hexdigest() != blob_id:
                    broken.append(blob_id)
        unique_broken = tuple(sorted(set(broken)))
        return ResticCheckResult(
            ok=not unique_broken,
            num_errors=len(unique_broken),
            broken_packs=unique_broken,
            suggest_repair_index=False,
            suggest_prune=False,
            raw={"num_errors": len(unique_broken), "broken_packs": list(unique_broken)},
        )

    def prune(self, *, dry_run: bool = False, repack_small: bool = False) -> dict[str, Any]:
        self._require_credentials()
        referenced: set[str] = set()
        for snap in self._load_all_snapshots():
            for meta in (snap.get("entries") or {}).values():
                referenced.add(str(meta["blob"]))
        data_dir = self.repo_path / "data"
        removed = 0
        removed_bytes = 0
        if data_dir.is_dir():
            for blob in sorted(data_dir.iterdir()):
                if blob.is_file() and blob.name not in referenced:
                    removed += 1
                    removed_bytes += blob.stat().st_size
                    if not dry_run:
                        blob.unlink()
        return {"blobs_removed": removed, "bytes_removed": removed_bytes, "dry_run": dry_run}

    def forget(
        self,
        *,
        keep_last: int | None = None,
        keep_daily: int | None = None,
        keep_weekly: int | None = None,
        keep_monthly: int | None = None,
        prune: bool = True,
        dry_run: bool = False,
        tags: tuple[str, ...] = (),
        group_by: str | None = "host",
        repack_small: bool = False,
    ) -> dict[str, Any]:
        self._require_credentials()
        all_snaps = self._load_all_snapshots()
        wanted_tags = set(tags)
        matching = [item for item in all_snaps if not wanted_tags or wanted_tags.issubset(set(item.get("tags") or ()))]
        kept_ids: set[str] = set()
        if keep_last is not None and keep_last > 0:
            kept_ids.update(item["id"] for item in matching[-keep_last:])
        elif keep_last is None:
            kept_ids.update(item["id"] for item in matching)
        removed_ids = [item["id"] for item in matching if item["id"] not in kept_ids]
        if not dry_run:
            for snap_id in removed_ids:
                (self.repo_path / "snapshots" / f"{snap_id}.json").unlink(missing_ok=True)
        prune_result = self.prune(dry_run=dry_run, repack_small=repack_small) if prune else None
        return {
            "kept": sorted(kept_ids),
            "removed": removed_ids,
            "prune": prune_result,
            "dry_run": dry_run,
        }

    def stats(self, *, mode: str = "raw-data") -> ResticStats:
        self._require_credentials()
        snaps = self._load_all_snapshots()
        data_dir = self.repo_path / "data"
        blobs = [p for p in data_dir.iterdir() if p.is_file()] if data_dir.is_dir() else []
        raw_bytes = sum(p.stat().st_size for p in blobs)
        restore_bytes = sum(
            int(meta["size"]) for snap in snaps for meta in (snap.get("entries") or {}).values()
        )
        file_count = sum(len(snap.get("entries") or {}) for snap in snaps)
        if mode == "restore-size":
            return ResticStats(
                total_size=restore_bytes,
                total_uncompressed_size=restore_bytes,
                compression_ratio=1.0,
                compression_space_saving=0.0,
                total_blob_count=len(blobs),
                snapshots_count=len(snaps),
                total_file_count=file_count,
                raw={"total_size": restore_bytes, "total_file_count": file_count, "snapshots_count": len(snaps)},
            )
        return ResticStats(
            total_size=raw_bytes,
            total_uncompressed_size=raw_bytes,
            compression_ratio=1.0,
            compression_space_saving=0.0,
            total_blob_count=len(blobs),
            snapshots_count=len(snaps),
            total_file_count=None,
            raw={"total_size": raw_bytes, "total_blob_count": len(blobs), "snapshots_count": len(snaps)},
        )

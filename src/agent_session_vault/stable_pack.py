from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil

from .archive import pack_paths, restore_bundle, sha256_file


PACKED_STABLE_FORMAT = "tar-zstd-shards-v1"
DEFAULT_SHARD_TARGET_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class PackedSourceSnapshot:
    entries: dict[str, tuple[int, int]]

    @property
    def total_bytes(self) -> int:
        return sum(size for size, _ in self.entries.values())

    @property
    def total_files(self) -> int:
        return len(self.entries)

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(b"directory\0")
        for relative, (size, mtime_ns) in sorted(self.entries.items()):
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(str(size).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(mtime_ns).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()


@dataclass(frozen=True)
class PackedDirectoryResult:
    source_snapshot: PackedSourceSnapshot
    index_path: Path | None
    status: str
    transfer_status: str
    archive_count: int
    archive_bytes: int
    transferred_archives: int
    reused_archives: int


def snapshot_directory(source: Path) -> PackedSourceSnapshot:
    entries: dict[str, tuple[int, int]] = {}
    for child in source.rglob("*"):
        if child.is_file():
            stat = child.stat()
            entries[child.relative_to(source).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return PackedSourceSnapshot(entries=entries)


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    previous = path.with_name(f".{path.name}.{os.getpid()}.previous")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if path.exists() or path.is_symlink():
        path.replace(previous)
    try:
        temporary.replace(path)
    except Exception:
        if previous.exists() or previous.is_symlink():
            previous.replace(path)
        raise
    previous.unlink(missing_ok=True)


def _load_index(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("archive_format") != PACKED_STABLE_FORMAT:
        return None
    if not isinstance(payload.get("files"), dict) or not isinstance(payload.get("shards"), dict):
        return None
    return payload


def _valid_previous_file(raw: object) -> tuple[int, int, str] | None:
    if not isinstance(raw, dict):
        return None
    size = raw.get("size")
    mtime_ns = raw.get("mtime_ns")
    shard = raw.get("shard")
    if not isinstance(size, int) or not isinstance(mtime_ns, int) or not isinstance(shard, str):
        return None
    return size, mtime_ns, shard


def _next_shard_id(existing: set[str]) -> str:
    numeric = [int(item) for item in existing if item.isdigit()]
    return f"{(max(numeric, default=0) + 1):06d}"


def _assign_shards(
    snapshot: PackedSourceSnapshot,
    previous_files: dict[str, object],
    shard_target_bytes: int,
) -> dict[str, str]:
    assignments: dict[str, str] = {}
    shard_sizes: dict[str, int] = {}
    known_shards: set[str] = set()

    for relative, (size, _) in sorted(snapshot.entries.items()):
        previous = _valid_previous_file(previous_files.get(relative))
        if previous is None:
            continue
        shard = previous[2]
        assignments[relative] = shard
        known_shards.add(shard)
        shard_sizes[shard] = shard_sizes.get(shard, 0) + size

    active_shard = max(known_shards, key=lambda item: int(item) if item.isdigit() else -1, default=None)
    for relative, (size, _) in sorted(snapshot.entries.items()):
        if relative in assignments:
            continue
        if (
            active_shard is None
            or shard_sizes.get(active_shard, 0) > 0
            and shard_sizes[active_shard] + size > shard_target_bytes
        ):
            active_shard = _next_shard_id(known_shards)
            known_shards.add(active_shard)
            shard_sizes[active_shard] = 0
        assignments[relative] = active_shard
        shard_sizes[active_shard] += size
    return assignments


def _archive_is_present(destination: Path, raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    archive_path = raw.get("archive_path")
    archive_bytes = raw.get("archive_bytes")
    if not isinstance(archive_path, str) or not isinstance(archive_bytes, int):
        return False
    candidate = destination / archive_path
    try:
        return candidate.is_file() and candidate.stat().st_size == archive_bytes
    except OSError:
        return False


def mirror_packed_directory(
    source: Path,
    destination: Path,
    *,
    dry_run: bool = False,
    shard_target_bytes: int = DEFAULT_SHARD_TARGET_BYTES,
) -> PackedDirectoryResult:
    if shard_target_bytes <= 0:
        raise ValueError("shard_target_bytes must be positive")

    source_snapshot = snapshot_directory(source)
    index_path = destination / "index.json"
    previous_index = _load_index(index_path) or {}
    previous_files = previous_index.get("files")
    previous_shards = previous_index.get("shards")
    previous_files = previous_files if isinstance(previous_files, dict) else {}
    previous_shards = previous_shards if isinstance(previous_shards, dict) else {}
    assignments = _assign_shards(source_snapshot, previous_files, shard_target_bytes)

    groups: dict[str, list[str]] = {}
    for relative, shard in assignments.items():
        groups.setdefault(shard, []).append(relative)
    for relatives in groups.values():
        relatives.sort()

    dirty_shards: set[str] = set()
    for relative, (size, mtime_ns) in source_snapshot.entries.items():
        previous = _valid_previous_file(previous_files.get(relative))
        if previous != (size, mtime_ns, assignments[relative]):
            dirty_shards.add(assignments[relative])
    for relative, raw in previous_files.items():
        previous = _valid_previous_file(raw)
        if relative not in source_snapshot.entries and previous is not None:
            dirty_shards.add(previous[2])
    for shard in groups:
        if not _archive_is_present(destination, previous_shards.get(shard)):
            dirty_shards.add(shard)

    reusable_shards = set(groups) - dirty_shards
    reusable_bytes = sum(
        int(previous_shards[shard]["archive_bytes"])
        for shard in reusable_shards
        if isinstance(previous_shards.get(shard), dict)
        and isinstance(previous_shards[shard].get("archive_bytes"), int)
    )
    if dry_run:
        return PackedDirectoryResult(
            source_snapshot=source_snapshot,
            index_path=None,
            status="planned",
            transfer_status="planned",
            archive_count=len(groups),
            archive_bytes=reusable_bytes,
            transferred_archives=len(dirty_shards),
            reused_archives=len(reusable_shards),
        )

    destination.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    staging_root = destination / ".staging" / run_id
    staged: dict[str, tuple[Path, dict[str, object]]] = {}
    moved_new: list[Path] = []
    try:
        for shard in sorted(dirty_shards):
            relatives = groups.get(shard, [])
            if not relatives:
                continue
            staged_path = staging_root / f"pack-{shard}.tar.zst"
            pack_paths(source, relatives, staged_path)
            archive_sha256 = sha256_file(staged_path)
            archive_name = f"pack-{shard}-{archive_sha256[:16]}.tar.zst"
            source_bytes = sum(source_snapshot.entries[relative][0] for relative in relatives)
            staged[shard] = (
                staged_path,
                {
                    "archive_path": archive_name,
                    "archive_bytes": staged_path.stat().st_size,
                    "sha256": archive_sha256,
                    "source_bytes": source_bytes,
                    "file_count": len(relatives),
                },
            )

        final_snapshot = snapshot_directory(source)
        if final_snapshot != source_snapshot:
            raise RuntimeError("source changed during packed mirror; retry after writers are quiescent")

        shard_payloads: dict[str, dict[str, object]] = {}
        for shard in sorted(groups):
            if shard in staged:
                staged_path, shard_payload = staged[shard]
                final_path = destination / str(shard_payload["archive_path"])
                if final_path.exists():
                    if final_path.stat().st_size != shard_payload["archive_bytes"] or sha256_file(
                        final_path
                    ) != shard_payload["sha256"]:
                        raise FileExistsError(f"packed archive collision: {final_path}")
                    staged_path.unlink()
                else:
                    staged_path.replace(final_path)
                    moved_new.append(final_path)
                shard_payloads[shard] = shard_payload
            else:
                previous_payload = previous_shards.get(shard)
                if not isinstance(previous_payload, dict):
                    raise RuntimeError(f"reusable shard metadata missing: {shard}")
                shard_payloads[shard] = dict(previous_payload)

        file_payloads = {
            relative: {
                "size": size,
                "mtime_ns": mtime_ns,
                "shard": assignments[relative],
            }
            for relative, (size, mtime_ns) in sorted(source_snapshot.entries.items())
        }
        index_payload = {
            "schema_version": 1,
            "archive_format": PACKED_STABLE_FORMAT,
            "source": str(source),
            "source_manifest_fingerprint": source_snapshot.fingerprint,
            "source_files": source_snapshot.total_files,
            "source_bytes": source_snapshot.total_bytes,
            "shard_target_bytes": shard_target_bytes,
            "created_at": datetime.now(UTC).isoformat(),
            "files": file_payloads,
            "shards": shard_payloads,
        }
        _write_json_atomic(index_path, index_payload)

        referenced = {str(payload["archive_path"]) for payload in shard_payloads.values()}
        for archive_path in destination.glob("pack-*.tar.zst"):
            if archive_path.name not in referenced:
                archive_path.unlink()
        shutil.rmtree(destination / ".staging", ignore_errors=True)

        archive_bytes = sum(int(payload["archive_bytes"]) for payload in shard_payloads.values())
        return PackedDirectoryResult(
            source_snapshot=source_snapshot,
            index_path=index_path,
            status="verified",
            transfer_status="packed" if dirty_shards else "reused_verified",
            archive_count=len(shard_payloads),
            archive_bytes=archive_bytes,
            transferred_archives=len(dirty_shards),
            reused_archives=len(reusable_shards),
        )
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        for path in moved_new:
            path.unlink(missing_ok=True)
        raise


def packed_directory_coverage(
    snapshot: PackedSourceSnapshot,
    destination: Path,
) -> tuple[str, int, int, int, int]:
    index = _load_index(destination / "index.json")
    if index is None or index.get("source_manifest_fingerprint") != snapshot.fingerprint:
        return "failed", 0, snapshot.total_files, 0, 0
    raw_files = index.get("files")
    raw_shards = index.get("shards")
    if not isinstance(raw_files, dict) or not isinstance(raw_shards, dict):
        return "failed", 0, snapshot.total_files, 0, 0

    mismatched = 0
    for relative, (size, mtime_ns) in snapshot.entries.items():
        previous = _valid_previous_file(raw_files.get(relative))
        if previous is None or previous[:2] != (size, mtime_ns):
            mismatched += 1
    missing_archives = sum(1 for payload in raw_shards.values() if not _archive_is_present(destination, payload))
    if mismatched or missing_archives or set(raw_files) != set(snapshot.entries):
        return "failed", 0, missing_archives, mismatched, 0
    archive_bytes = sum(
        int(payload["archive_bytes"])
        for payload in raw_shards.values()
        if isinstance(payload, dict) and isinstance(payload.get("archive_bytes"), int)
    )
    return "verified", snapshot.total_files, 0, 0, archive_bytes


def restore_packed_directory(source: Path, destination: Path) -> tuple[int, int]:
    index = _load_index(source / "index.json")
    if index is None:
        raise ValueError(f"packed stable index is missing or invalid: {source / 'index.json'}")
    raw_shards = index.get("shards")
    raw_files = index.get("files")
    if not isinstance(raw_shards, dict) or not isinstance(raw_files, dict):
        raise ValueError(f"packed stable index is incomplete: {source / 'index.json'}")

    destination.mkdir(parents=True, exist_ok=True)
    for shard in sorted(raw_shards):
        payload = raw_shards[shard]
        if not isinstance(payload, dict):
            raise ValueError(f"invalid shard metadata: {shard}")
        archive_path = source / str(payload.get("archive_path"))
        expected_sha256 = payload.get("sha256")
        if not isinstance(expected_sha256, str) or sha256_file(archive_path) != expected_sha256:
            raise ValueError(f"packed stable archive checksum mismatch: {archive_path}")
        restore_bundle(archive_path, destination)

    restored = snapshot_directory(destination)
    expected_sizes = {
        relative: int(payload["size"])
        for relative, payload in raw_files.items()
        if isinstance(payload, dict) and isinstance(payload.get("size"), int)
    }
    restored_sizes = {relative: size for relative, (size, _) in restored.entries.items()}
    if expected_sizes != restored_sizes:
        raise ValueError(f"restored packed stable coverage mismatch: {destination}")
    return restored.total_files, restored.total_bytes

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Iterable

from .archive_backend import FilesystemArchiveBackend, PublishedSnapshot
from .archive_model import ArchiveManifest, CatalogEntry
from .config import VaultConfig


def entries_for_manifest(manifest: ArchiveManifest) -> tuple[CatalogEntry, ...]:
    source = manifest.source
    return tuple(
        CatalogEntry(
            machine_id=source.machine_id,
            source_id=source.source_id,
            client=source.client,
            session_id=record.session_id,
            snapshot_id=manifest.snapshot_id,
            path=record.path,
            start_at=record.start_at,
            end_at=record.end_at,
            bytes=record.bytes,
            sha256=record.sha256,
            parse_status=record.parse_status,
        )
        for record in manifest.files
    )


def write_catalog_segment(backend: FilesystemArchiveBackend, published: PublishedSnapshot) -> list[Path]:
    paths: list[Path] = []
    for manifest in published.manifests:
        entries = [entry.to_payload() for entry in entries_for_manifest(manifest)]
        segment = backend.root / "catalog" / "segments" / published.snapshot.machine_id / f"{manifest.snapshot_id}.json"
        segment.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "archive.catalog-segment.v1",
            "snapshot_id": manifest.snapshot_id,
            "machine_id": published.snapshot.machine_id,
            "source_id": manifest.source.source_id,
            "entries": entries,
        }
        serialized = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        if segment.exists():
            if segment.read_text(encoding="utf-8") != serialized:
                raise ValueError(f"catalog segment collision: {segment}")
        else:
            temporary = segment.with_name(f".{segment.name}.{os.getpid()}.tmp")
            temporary.write_text(serialized, encoding="utf-8")
            temporary.replace(segment)
        paths.append(segment)
    return paths


def _iter_segment_payloads(backend: FilesystemArchiveBackend) -> Iterable[dict[str, object]]:
    root = backend.root / "catalog" / "segments"
    if not root.exists():
        return
    for path in sorted(root.rglob("*.json"), key=str):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            yield payload


def rebuild_catalog(backend: FilesystemArchiveBackend, machine_id: str | None = None) -> int:
    count = 0
    for snapshot_dir in backend.iter_snapshot_dirs(machine_id):
        published = backend.load_snapshot(snapshot_dir)
        count += len(write_catalog_segment(backend, published))
    return count


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        return result.replace(tzinfo=UTC)
    return result


def _overlaps(entry: CatalogEntry, from_at: str | None, to_at: str | None) -> bool:
    lower = _parse_time(from_at)
    upper = _parse_time(to_at)
    start = _parse_time(entry.start_at)
    end = _parse_time(entry.end_at) or start
    if lower is None and upper is None:
        return True
    if start is None and end is None:
        return False
    if lower is not None and end is not None and end < lower:
        return False
    if upper is not None and start is not None and start >= upper:
        return False
    return True


def query_catalog(
    config: VaultConfig,
    *,
    from_at: str | None = None,
    to_at: str | None = None,
    machine_id: str | None = None,
    client: str | None = "codex",
    session_id: str | None = None,
    source_id: str | None = None,
) -> list[CatalogEntry]:
    backend = FilesystemArchiveBackend(config.archive.root)
    backend.ensure_ready()
    entries_by_key: dict[tuple[str, str, str, str], CatalogEntry] = {}
    for payload in _iter_segment_payloads(backend):
        if machine_id and payload.get("machine_id") != machine_id:
            continue
        if source_id and payload.get("source_id") != source_id:
            continue
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            continue
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            entry = CatalogEntry(
                machine_id=str(raw["machine_id"]),
                source_id=str(raw["source_id"]),
                client=str(raw["client"]),
                session_id=str(raw["session_id"]),
                snapshot_id=str(raw["snapshot_id"]),
                path=str(raw["path"]),
                start_at=raw.get("start_at") if isinstance(raw.get("start_at"), str) else None,
                end_at=raw.get("end_at") if isinstance(raw.get("end_at"), str) else None,
                bytes=int(raw["bytes"]),
                sha256=str(raw["sha256"]),
                parse_status=str(raw.get("parse_status", "opaque")),
            )
            if client and entry.client != client:
                continue
            if session_id and entry.session_id != session_id:
                continue
            if _overlaps(entry, from_at, to_at):
                key = (entry.machine_id, entry.source_id, entry.session_id, entry.path)
                previous = entries_by_key.get(key)
                if previous is None or entry.snapshot_id > previous.snapshot_id:
                    entries_by_key[key] = entry
    return sorted(entries_by_key.values(), key=lambda item: (item.start_at or "", item.machine_id, item.path, item.snapshot_id))

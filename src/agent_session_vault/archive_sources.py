from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable
import uuid

from .archive_model import ArchiveFileRecord, ArchiveSource
from .config import ArchiveSourceConfig, VaultConfig


@dataclass(frozen=True)
class ScannedFile:
    source_path: Path
    record: ArchiveFileRecord


@dataclass(frozen=True)
class ScannedSource:
    source: ArchiveSource
    files: tuple[ScannedFile, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArchiveScanResult:
    machine_id: str
    sources: tuple[ScannedSource, ...]
    missing_sources: tuple[str, ...] = ()

    @property
    def files(self) -> tuple[ScannedFile, ...]:
        return tuple(item for source in self.sources for item in source.files)


def machine_id_path(config: VaultConfig) -> Path:
    return config.archive.machine_id_path.expanduser()


def load_or_create_machine_id(config: VaultConfig) -> str:
    path = machine_id_path(config)
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = f"machine-{uuid.uuid4().hex}"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(value + "\n", encoding="utf-8")
    temporary.replace(path)
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def _timestamp(value: object) -> str | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return _timestamp(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _record_metadata(path: Path) -> tuple[str | None, str | None, str | None, str]:
    session_id: str | None = None
    timestamps: list[str] = []
    parse_status = "parsed"
    try:
        with _open_text(path) as handle:
            for raw in handle:
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    parse_status = "opaque"
                    continue
                if not isinstance(value, dict):
                    continue
                obj_type = value.get("type")
                payload = value.get("payload")
                if obj_type == "session_meta" and isinstance(payload, dict):
                    candidate = payload.get("id")
                    if isinstance(candidate, str) and candidate:
                        session_id = candidate
                for key in ("timestamp", "time", "created_at"):
                    candidate = _timestamp(value.get(key))
                    if candidate:
                        timestamps.append(candidate)
                if isinstance(payload, dict):
                    for key in ("timestamp", "time", "created_at"):
                        candidate = _timestamp(payload.get(key))
                        if candidate:
                            timestamps.append(candidate)
    except (OSError, UnicodeError):
        return None, None, None, "opaque"
    if not timestamps:
        return session_id, None, None, parse_status
    return session_id, min(timestamps), max(timestamps), parse_status


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "root"


def _relative_root(config: VaultConfig, path: Path, item: ArchiveSourceConfig) -> str:
    home = config.paths.home.expanduser().resolve()
    workspace = config.paths.workspace_root.expanduser().resolve()
    resolved = path.resolve()
    if resolved == home / ".codex":
        return ".codex"
    try:
        relative = resolved.relative_to(workspace)
    except ValueError:
        relative = Path(item.label or resolved.name)
    return Path("workspace").joinpath(relative).as_posix()


def _source_id(machine_id: str, relative_root: str, item: ArchiveSourceConfig) -> str:
    identity = f"{machine_id}\0codex\0{item.kind}\0{relative_root}".encode("utf-8")
    return f"source-{hashlib.sha256(identity).hexdigest()[:24]}"


def _default_source_configs(config: VaultConfig) -> tuple[ArchiveSourceConfig, ...]:
    configured = config.archive.source_paths
    if configured:
        return configured
    values = [ArchiveSourceConfig(path="~/.codex", kind="codex_home", label="home")]
    workspace = config.paths.workspace_root.expanduser()
    if workspace.is_dir():
        for child in sorted(workspace.iterdir(), key=str):
            candidate = child / ".codex"
            if candidate.is_dir():
                values.append(ArchiveSourceConfig(path=str(candidate), kind="project_root", label=child.name))
    return tuple(values)


def _resolve_source_path(config: VaultConfig, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config.paths.home.expanduser() / path
    return path.resolve()


def _iter_allowed_files(root: Path) -> Iterable[Path]:
    for bucket in ("sessions", "archived_sessions"):
        bucket_root = root / bucket
        if not bucket_root.is_dir():
            continue
        for path in sorted(bucket_root.rglob("*"), key=str):
            if path.is_symlink() or not path.is_file():
                continue
            if path.name.endswith((".jsonl", ".jsonl.gz")):
                yield path
    index_path = root / "session_index.jsonl"
    if index_path.is_file() and not index_path.is_symlink():
        yield index_path


def _relative_file(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _scan_source(config: VaultConfig, machine_id: str, item: ArchiveSourceConfig) -> ScannedSource | None:
    root = _resolve_source_path(config, item.path)
    if not root.is_dir():
        return None
    relative_root = _relative_root(config, root, item)
    source = ArchiveSource(
        source_id=_source_id(machine_id, relative_root, item),
        machine_id=machine_id,
        client="codex",
        kind=item.kind,
        root_path=str(root),
        relative_root=relative_root,
        label=item.label,
    )
    files: list[ScannedFile] = []
    warnings: list[str] = []
    seen: set[Path] = set()
    for path in _iter_allowed_files(root):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        before = path.stat()
        digest = _sha256_file(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            warnings.append(f"unstable:{path}")
        relative = Path(relative_root) / _relative_file(root, path)
        if path.name == "session_index.jsonl":
            session_id, start_at, end_at, parse_status = "index", None, None, "index"
        else:
            session_id, start_at, end_at, parse_status = _record_metadata(path)
            if not session_id:
                session_id = f"codex-{hashlib.sha256(relative.as_posix().encode('utf-8')).hexdigest()[:24]}"
        files.append(
            ScannedFile(
                source_path=path,
                record=ArchiveFileRecord(
                    path=relative.as_posix(),
                    session_id=session_id,
                    bytes=after.st_size,
                    sha256=digest,
                    start_at=start_at,
                    end_at=end_at,
                    time_source="session_event" if start_at else "none",
                    parse_status=parse_status,
                ),
            )
        )
    return ScannedSource(source=source, files=tuple(files), warnings=tuple(sorted(set(warnings))))


def scan_codex_sources(
    config: VaultConfig,
    *,
    machine_id: str | None = None,
    source_configs: tuple[ArchiveSourceConfig, ...] | None = None,
) -> ArchiveScanResult:
    resolved_machine_id = machine_id or load_or_create_machine_id(config)
    sources: list[ScannedSource] = []
    missing: list[str] = []
    for item in source_configs or _default_source_configs(config):
        scanned = _scan_source(config, resolved_machine_id, item)
        if scanned is None:
            missing.append(item.path)
            continue
        sources.append(scanned)
    return ArchiveScanResult(
        machine_id=resolved_machine_id,
        sources=tuple(sorted(sources, key=lambda item: item.source.source_id)),
        missing_sources=tuple(sorted(missing)),
    )

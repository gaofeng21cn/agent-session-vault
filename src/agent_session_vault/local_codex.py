from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import glob
import hashlib
import json
from pathlib import Path
import re
import shutil

from .config import VaultConfig
from .projection import TERMINAL_EVENT_TYPES


STATE_FILE_NAME = "sync-state.json"


@dataclass(frozen=True)
class LocalCodexSyncResult:
    namespace: str
    codex_root: Path
    state_path: Path
    files_seen: int
    files_written: int
    files_skipped: int
    source_bytes_total: int
    dest_bytes_total: int
    token_events_total: int
    missing_sources: list[Path]


@dataclass(frozen=True)
class _SessionRoot:
    bucket: str
    root_id: str
    root_home: Path
    session_root: Path


@dataclass(frozen=True)
class _SessionFile:
    source_path: Path
    session_root: _SessionRoot
    relative_path: Path


def _json_load(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "root"


def _canonical_root_identity(root_home: Path) -> str:
    raw = root_home.as_posix()
    marker = "/.ds/cold_archive/codex_sessions/.ds/codex_homes/"
    if marker in raw:
        return raw.replace(marker, "/.ds/codex_homes/", 1)
    return raw


def _root_id(root_home: Path) -> str:
    identity = _canonical_root_identity(root_home)
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:8]
    return f"{_sanitize_slug(root_home.name)}-{digest}"


def _strip_gzip_suffix(path: Path) -> Path:
    if path.name.endswith(".jsonl.gz"):
        return path.with_name(path.name[:-3])
    return path


def _quest_root_for_session_root(session_root: Path) -> Path | None:
    parts = session_root.parts
    for index in range(len(parts) - 1):
        if parts[index] == ".ds" and parts[index + 1] == "codex_homes":
            return Path(*parts[:index])
    return None


def _read_index_archive_ref(index_path: Path, session_root: Path) -> Path | None:
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    archive_ref = payload.get("cold_archive_ref")
    if not isinstance(archive_ref, str) or not archive_ref:
        return None
    archive_path = Path(archive_ref)
    if archive_path.is_absolute():
        return archive_path
    quest_root = _quest_root_for_session_root(session_root)
    if quest_root is None:
        return None
    return quest_root / archive_path


def _add_session_root(session_roots: dict[Path, _SessionRoot], root_home: Path, bucket: str, session_root: Path) -> None:
    if not session_root.is_dir():
        return
    resolved_home = root_home.resolve()
    resolved_session = session_root.resolve()
    if resolved_session in session_roots:
        return
    session_roots[resolved_session] = _SessionRoot(
        bucket=bucket,
        root_id=_root_id(resolved_home),
        root_home=resolved_home,
        session_root=resolved_session,
    )


def discover_local_codex_session_roots(source: Path) -> list[_SessionRoot]:
    source = source.expanduser()
    if not source.exists():
        return []
    source = source.resolve()
    roots: dict[Path, _SessionRoot] = {}

    def add_home(root_home: Path) -> None:
        _add_session_root(roots, root_home, "sessions", root_home / "sessions")
        _add_session_root(roots, root_home, "archived_sessions", root_home / "archived_sessions")

    add_home(source)
    if source.name in {"sessions", "archived_sessions"}:
        _add_session_root(roots, source.parent, source.name, source)

    codex_homes = source / ".ds" / "codex_homes"
    if source.name == "codex_homes":
        codex_homes = source
    if codex_homes.is_dir():
        for run_home in sorted(path for path in codex_homes.iterdir() if path.is_dir()):
            add_home(run_home)

    cold_codex_homes = source / ".ds" / "cold_archive" / "codex_sessions" / ".ds" / "codex_homes"
    if source.name == "codex_sessions":
        cold_codex_homes = source / ".ds" / "codex_homes"
    if cold_codex_homes.is_dir():
        for run_home in sorted(path for path in cold_codex_homes.iterdir() if path.is_dir()):
            add_home(run_home)

    return sorted(roots.values(), key=lambda item: (item.root_id, str(item.session_root)))


def _iter_session_files(session_root: _SessionRoot) -> list[_SessionFile]:
    files: dict[Path, _SessionFile] = {}
    for pattern in ("*.jsonl", "*.jsonl.gz"):
        for path in sorted(session_root.session_root.rglob(pattern)):
            if path.name.endswith(".jsonl.index.json"):
                continue
            rel = _strip_gzip_suffix(path).relative_to(session_root.session_root)
            files[path.resolve()] = _SessionFile(path.resolve(), session_root, rel)

    for index_path in sorted(session_root.session_root.rglob("*.jsonl.index.json")):
        archive_path = _read_index_archive_ref(index_path, session_root.session_root)
        if archive_path is None or not archive_path.is_file():
            continue
        archive_path = archive_path.resolve()
        try:
            rel = _strip_gzip_suffix(archive_path).relative_to(session_root.session_root)
        except ValueError:
            rel = _strip_gzip_suffix(index_path).relative_to(session_root.session_root)
        if rel.name.endswith(".index.json"):
            rel = rel.with_name(rel.name[: -len(".index.json")])
        files.setdefault(archive_path, _SessionFile(archive_path, session_root, rel))

    return sorted(files.values(), key=lambda item: str(item.source_path))


def _open_session_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def _project_codex_session_file(source_path: Path, dest_path: Path) -> dict[str, int]:
    leading_session_meta_lines: list[str] = []
    turn_context_line: str | None = None
    token_event_lines: list[str] = []
    terminal_line: str | None = None
    leading_meta_open = True

    with _open_session_text(source_path) as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            obj = _json_load(line)
            if obj is None:
                leading_meta_open = False
                continue

            obj_type = obj.get("type")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

            if obj_type == "session_meta" and leading_meta_open:
                leading_session_meta_lines.append(line)
            elif obj_type != "session_meta":
                leading_meta_open = False

            if obj_type == "turn_context" and turn_context_line is None:
                turn_context_line = line

            if obj_type != "event_msg":
                continue

            event_type = payload.get("type")
            if event_type == "token_count":
                token_event_lines.append(line)
            elif event_type in TERMINAL_EVENT_TYPES:
                terminal_line = line

    lines: list[str] = []
    lines.extend(leading_session_meta_lines)
    if turn_context_line is not None:
        lines.append(turn_context_line)
    lines.extend(token_event_lines)
    if terminal_line is not None:
        lines.append(terminal_line)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with dest_path.open("w", encoding="utf-8") as handle:
        if lines:
            handle.write("\n".join(lines))
            handle.write("\n")

    return {
        "source_bytes": source_path.stat().st_size,
        "dest_bytes": dest_path.stat().st_size,
        "token_events": len(token_event_lines),
    }


def _load_state(state_path: Path) -> dict[str, object]:
    if not state_path.is_file():
        return {"version": 1, "files": {}}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "files": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
        return {"version": 1, "files": {}}
    return payload


def _source_signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _expand_sources(sources: list[Path], source_globs: list[str]) -> tuple[list[Path], list[Path]]:
    expanded: list[Path] = []
    missing: list[Path] = []
    for source in sources:
        resolved = source.expanduser()
        if resolved.exists():
            expanded.append(resolved)
        else:
            missing.append(resolved)
    for raw_glob in source_globs:
        matches = [Path(match).expanduser() for match in glob.glob(str(Path(raw_glob).expanduser()), recursive=True)]
        if matches:
            expanded.extend(matches)
        else:
            missing.append(Path(raw_glob).expanduser())
    deduped: dict[str, Path] = {}
    for source in expanded:
        deduped[str(source.resolve())] = source.resolve()
    return sorted(deduped.values(), key=str), missing


def sync_local_codex_sources(
    config: VaultConfig,
    sources: list[Path],
    *,
    source_globs: list[str] | None = None,
    namespace: str = "volatile-codex-homes",
    dry_run: bool = False,
) -> LocalCodexSyncResult:
    if not sources and not source_globs:
        raise ValueError("at least one local Codex source or source glob is required")

    namespace_slug = _sanitize_slug(namespace)
    namespace_root = config.paths.local_workspace_extras / namespace_slug
    codex_root = namespace_root / "codex"
    state_path = namespace_root / STATE_FILE_NAME
    state = _load_state(state_path)
    state_files = state["files"]
    assert isinstance(state_files, dict)

    expanded_sources, missing_sources = _expand_sources(sources, source_globs or [])
    session_files: list[_SessionFile] = []
    for source in expanded_sources:
        for session_root in discover_local_codex_session_roots(source):
            session_files.extend(_iter_session_files(session_root))
    session_files = list({str(item.source_path): item for item in session_files}.values())
    session_files.sort(key=lambda item: str(item.source_path))

    files_seen = 0
    files_written = 0
    files_skipped = 0
    source_bytes_total = 0
    dest_bytes_total = 0
    token_events_total = 0

    for session_file in session_files:
        files_seen += 1
        source_path = session_file.source_path
        signature = _source_signature(source_path)
        source_key = str(source_path)
        dest_path = codex_root / session_file.session_root.bucket / session_file.session_root.root_id / session_file.relative_path
        previous = state_files.get(source_key)
        if (
            isinstance(previous, dict)
            and previous.get("size") == signature["size"]
            and previous.get("mtime_ns") == signature["mtime_ns"]
            and dest_path.is_file()
        ):
            files_skipped += 1
            continue

        if dry_run:
            files_written += 1
            continue

        item = _project_codex_session_file(source_path, dest_path)
        files_written += 1
        source_bytes_total += item["source_bytes"]
        dest_bytes_total += item["dest_bytes"]
        token_events_total += item["token_events"]
        state_files[source_key] = {
            **signature,
            "dest": str(dest_path),
            "synced_at": datetime.now(UTC).isoformat(),
        }

    if not dry_run:
        namespace_root.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return LocalCodexSyncResult(
        namespace=namespace_slug,
        codex_root=codex_root,
        state_path=state_path,
        files_seen=files_seen,
        files_written=files_written,
        files_skipped=files_skipped,
        source_bytes_total=source_bytes_total,
        dest_bytes_total=dest_bytes_total,
        token_events_total=token_events_total,
        missing_sources=missing_sources,
    )

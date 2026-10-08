"""Privacy-preserving OpenClaw inputs for Tokscale's native parser.

This module is also embedded in Fleet's standalone projection job. Keep it
standard-library-only; zstd is the same runtime used by Vault's archive jobs.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile


OPENCLAW_PROJECTION_VERSION = 1


def openclaw_decompress(payload: bytes, expected_size: int | None = None) -> bytes:
    limit = 64 * 1024 * 1024
    if expected_size is not None and not 0 < expected_size <= limit:
        raise ValueError("invalid OpenClaw decoded payload size")
    library = ctypes.util.find_library("zstd")
    if library:
        zstd = ctypes.CDLL(library)
        zstd.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        zstd.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        size = expected_size or zstd.ZSTD_getFrameContentSize(payload, len(payload))
        if 0 < size <= limit:
            zstd.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
            zstd.ZSTD_decompress.restype = ctypes.c_size_t
            buffer = ctypes.create_string_buffer(size)
            actual = zstd.ZSTD_decompress(buffer, size, payload, len(payload))
            if actual > size or (expected_size is not None and actual != expected_size):
                raise ValueError("invalid OpenClaw zstd payload")
            return buffer.raw[:actual]
    # Streaming frames may not advertise a decoded length.
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as target:
        source.write(payload)
        source.seek(0)
        subprocess.run(["zstd", "-dq", "-c"], stdin=source, stdout=target, check=True)
        target.seek(0)
        decoded = target.read(limit + 1)
    if len(decoded) > limit or (expected_size is not None and len(decoded) != expected_size):
        raise ValueError("invalid OpenClaw decoded payload size")
    return decoded


def openclaw_project_record(event: dict) -> dict:
    result = {key: event[key] for key in ("type", "id", "parentId", "timestamp") if key in event}
    kind = event.get("type")
    if kind == "message":
        message = event.get("message")
        if isinstance(message, dict):
            result["message"] = {
                key: message[key] for key in (
                    "role", "api", "provider", "model", "usage", "timestamp", "idempotencyKey",
                ) if key in message
            }
    elif kind == "model_change":
        result.update({key: event[key] for key in ("modelId", "provider") if key in event})
    elif kind == "custom" and event.get("customType") == "model-snapshot":
        result["customType"] = "model-snapshot"
        data = event.get("data", {})
        if isinstance(data, dict):
            result["data"] = {key: data[key] for key in ("modelId", "provider") if key in data}
    return result


def openclaw_is_codex_rollout(path: Path) -> bool:
    parts = path.parts
    for index, part in enumerate(parts):
        if index and parts[index - 1] == "agent":
            if part == "codex-home":
                return len(parts) > index + 1 and parts[index + 1] in {"sessions", "archived_sessions"}
            if part == "cli-auth" and parts[index + 1:index + 2] == ("codex",):
                return len(parts) > index + 3 and parts[index + 3] in {"sessions", "archived_sessions"}
    return False


def openclaw_read_text(path: Path) -> str:
    if path.suffix == ".zst":
        return openclaw_decompress(path.read_bytes()).decode("utf-8")
    if path.suffix == ".gz":
        return gzip.decompress(path.read_bytes()).decode("utf-8")
    return path.read_text(encoding="utf-8")


def openclaw_prepare_sqlite(source: Path, destination: Path) -> None:
    """Snapshot WAL, retain analytics history, and expose no live client tables."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    records = {}
    sessions = {}

    def add(session_id, sequence, event, created_at):
        projected = openclaw_project_record(event)
        kind = projected.get("type")
        message = projected.get("message", {})
        if kind == "message":
            if message.get("role") != "assistant" or not isinstance(message.get("usage"), dict):
                return
        elif kind != "model_change" and projected.get("customType") != "model-snapshot":
            return
        text = json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        # Preserve rewound/deleted history and changed event usage. Identical
        # copies in archives/live rows collapse; Tokscale also dedups forks.
        identity = hashlib.sha256(text.encode("utf-8")).hexdigest()
        records[(session_id, identity)] = (sequence, text, created_at)

    if destination.is_file():
        with sqlite3.connect(destination) as previous:
            sessions.update({row[0]: row[1:] for row in previous.execute("SELECT * FROM session_windows")})
            for sid, seq, text, created_at in previous.execute("SELECT * FROM transcript_events"):
                add(sid, seq, json.loads(text), created_at)

    with tempfile.TemporaryDirectory(prefix=".openclaw-", dir=destination.parent) as temporary:
        snapshot = Path(temporary) / "snapshot.sqlite"
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as live:
            with sqlite3.connect(snapshot) as copy:
                live.backup(copy)
        with sqlite3.connect(snapshot) as database:
            tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("sessions", "session_windows"):
                if table in tables:
                    sessions.update({row[0]: row[1:] for row in database.execute(
                        f"SELECT session_id, model_provider, model FROM {table}"
                    )})
            if "transcript_events" in tables:
                columns = {row[1] for row in database.execute("PRAGMA table_info(transcript_events)")}
                blobs = "event_zstd, event_utf8_bytes" if "event_zstd" in columns else "NULL, NULL"
                for sid, seq, text, created_at, blob, size in database.execute(
                    f"SELECT session_id, seq, event_json, created_at, {blobs} FROM transcript_events ORDER BY session_id,seq"
                ):
                    if text is None:
                        text = openclaw_decompress(blob, size).decode("utf-8")
                    add(sid, seq, json.loads(text), created_at)
            for table in ("session_transcript_archives", "session_transcript_cold_archives"):
                if table not in tables:
                    continue
                encoding = "encoding" if table == "session_transcript_archives" else "'zstd'"
                for sid, blob, encoding in database.execute(
                    f"SELECT session_id, archive_blob, {encoding} FROM {table} WHERE archive_blob IS NOT NULL"
                ):
                    payload = openclaw_decompress(blob) if encoding == "zstd" else blob
                    for seq, line in enumerate(payload.decode("utf-8").splitlines()):
                        if line.strip():
                            event = json.loads(line)
                            timestamp = event.get("message", {}).get("timestamp", 0)
                            add(sid, seq, event, timestamp if isinstance(timestamp, (int, float)) else 0)

        output = Path(temporary) / "projection.sqlite"
        with sqlite3.connect(output) as target:
            target.executescript(
                "CREATE TABLE session_windows(session_id TEXT PRIMARY KEY,model_provider TEXT,model TEXT);"
                "CREATE TABLE transcript_events(session_id TEXT,seq INTEGER,event_json TEXT,created_at INTEGER,"
                "PRIMARY KEY(session_id,seq));"
            )
            target.executemany("INSERT INTO session_windows VALUES(?,?,?)", [
                (sid, *metadata) for sid, metadata in sorted(sessions.items())
            ])
            sequence_by_session = {}
            for (sid, identity), (_, text, created_at) in sorted(
                records.items(), key=lambda item: (item[0][0], item[1][2], item[1][0], item[0][1])
            ):
                seq = sequence_by_session.get(sid, 0)
                sequence_by_session[sid] = seq + 1
                target.execute("INSERT INTO transcript_events VALUES(?,?,?,?)", (sid, seq, text, created_at))
        if not destination.is_file() or destination.read_bytes() != output.read_bytes():
            output.replace(destination)


def openclaw_projection_files(source_root: Path, cache_root: Path, dry_run: bool = False):
    agents = source_root / "agents" if (source_root / "agents").is_dir() else source_root
    for path in sorted(agents.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(agents)
        if path.name == "openclaw-agent.sqlite" and path.parent.name == "agent":
            cached = cache_root / relative
            if not dry_run:
                openclaw_prepare_sqlite(path, cached)
            yield path if dry_run else cached, relative
        elif ".jsonl" in path.name:
            # Codex home logs/history are not OpenClaw transcripts.
            if ("codex-home" in relative.parts or "cli-auth" in relative.parts) and not openclaw_is_codex_rollout(path):
                continue
            yield path, relative

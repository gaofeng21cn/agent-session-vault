from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile


STALE_SESSION_MIN_AGE_SECONDS = 24 * 60 * 60
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SESSION_WARNING_RE = re.compile(
    r"failed to fetch Antigravity metadata for session\s+([A-Za-z0-9][A-Za-z0-9._-]*):",
    re.IGNORECASE,
)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_stale(path: Path, now: datetime, minimum_age_seconds: int) -> bool:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return False
    return (now - modified).total_seconds() >= minimum_age_seconds


def _tree_is_stale(path: Path, now: datetime, minimum_age_seconds: int) -> bool:
    if not _is_stale(path, now, minimum_age_seconds):
        return False
    try:
        return all(
            _is_stale(child, now, minimum_age_seconds)
            for child in path.rglob("*")
            if child.is_file()
        )
    except OSError:
        return False


def _entry_is_stale(entry: object, fallback_path: Path, now: datetime, minimum_age_seconds: int) -> bool:
    if isinstance(entry, dict):
        summary = entry.get("summary")
        if isinstance(summary, dict):
            for field in ("UpdatedAt", "LastModifiedTime"):
                parsed = _parse_timestamp(summary.get(field))
                if parsed is not None:
                    return (now - parsed).total_seconds() >= minimum_age_seconds
        parsed = _parse_timestamp(entry.get("last_modified_time"))
        if parsed is not None:
            return (now - parsed).total_seconds() >= minimum_age_seconds
    return _is_stale(fallback_path, now, minimum_age_seconds)


def stale_session_ids(output: str) -> list[str]:
    """Return session IDs whose metadata RPC explicitly reports a missing trajectory."""

    lines = output.splitlines()
    found: list[str] = []
    for index, line in enumerate(lines):
        match = _SESSION_WARNING_RE.search(line)
        if match is None:
            continue
        window = "\n".join(lines[index : index + 4]).lower()
        if "trajectory not found" not in window:
            continue
        session_id = match.group(1)
        if session_id not in found and _SESSION_ID_RE.fullmatch(session_id):
            found.append(session_id)
    return found


def _write_json_atomically(path: Path, payload: object) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def repair_stale_sessions(
    source_home: Path,
    output: str,
    *,
    now: datetime | None = None,
    minimum_age_seconds: int = STALE_SESSION_MIN_AGE_SECONDS,
) -> dict[str, object]:
    """Quarantine local Antigravity artifacts confirmed missing by the RPC.

    The repair is deliberately narrow: empty brain directories are safe to move;
    non-empty artifacts and index rows must be at least ``minimum_age_seconds``
    old. Every mutation is retained under a timestamped quarantine manifest.
    """

    session_ids = stale_session_ids(output)
    result: dict[str, object] = {
        "status": "not_needed" if not session_ids else "no_candidates",
        "session_ids": session_ids,
        "moved": [],
        "index_updates": [],
        "deferred": [],
        "errors": [],
    }
    if not session_ids:
        return result

    current = (now or datetime.now(UTC)).astimezone(UTC)
    gemini_root = source_home / ".gemini"
    if not gemini_root.is_dir():
        result["status"] = "no_source_root"
        return result

    quarantine_root: Path | None = None

    def ensure_quarantine() -> Path:
        nonlocal quarantine_root
        if quarantine_root is None:
            stamp = current.astimezone().strftime("%Y%m%dT%H%M%S%z")
            base = gemini_root / ".codex-retired-brain-entries"
            quarantine_root = base / f"stale-antigravity-{stamp}"
            suffix = 1
            while quarantine_root.exists():
                quarantine_root = base / f"stale-antigravity-{stamp}-{suffix}"
                suffix += 1
            quarantine_root.mkdir(parents=True, exist_ok=False)
        return quarantine_root

    def move_path(path: Path) -> None:
        if path.is_symlink():
            result["deferred"].append({"path": str(path), "reason": "symlink"})
            return
        destination = ensure_quarantine() / path.relative_to(gemini_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(path), str(destination))
        except OSError as exc:
            result["errors"].append({"path": str(path), "error": str(exc)})
            return
        moved = result["moved"]
        assert isinstance(moved, list)
        moved.append(
            {
                "source": str(path),
                "destination": str(destination),
                "sha256": _sha256(destination),
                "bytes": destination.stat().st_size if destination.is_file() else None,
            }
        )

    roots = sorted(
        path
        for path in gemini_root.glob("antigravity*")
        if path.is_dir() and path.name != ".codex-retired-brain-entries"
    )
    deferred_session_ids: set[str] = set()
    for session_id in session_ids:
        for root in roots:
            brain_path = root / "brain" / session_id
            if brain_path.exists():
                empty = brain_path.is_dir() and not brain_path.is_symlink() and not any(brain_path.iterdir())
                if empty or _tree_is_stale(brain_path, current, minimum_age_seconds):
                    move_path(brain_path)
                else:
                    result["deferred"].append({"path": str(brain_path), "reason": "recent"})
                    deferred_session_ids.add(session_id)

            conversation_root = root / "conversations"
            for suffix in (".pb", ".db"):
                conversation_path = conversation_root / f"{session_id}{suffix}"
                if not conversation_path.exists():
                    continue
                if not _is_stale(conversation_path, current, minimum_age_seconds):
                    result["deferred"].append({"path": str(conversation_path), "reason": "recent"})
                    deferred_session_ids.add(session_id)
                    continue
                move_path(conversation_path)
                if suffix == ".db":
                    for sidecar in (conversation_path.with_name(conversation_path.name + "-wal"), conversation_path.with_name(conversation_path.name + "-shm")):
                        if sidecar.exists():
                            move_path(sidecar)

    metadata_path = gemini_root / "antigravity-cli" / "cache" / "conversation_metadata.json"
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            conversations = metadata.get("conversations") if isinstance(metadata, dict) else None
            if isinstance(conversations, dict):
                removable = [
                    session_id
                    for session_id in session_ids
                    if session_id in conversations
                    and session_id not in deferred_session_ids
                    and _entry_is_stale(conversations[session_id], metadata_path, current, minimum_age_seconds)
                ]
                if removable:
                    backup = ensure_quarantine() / metadata_path.relative_to(gemini_root)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(metadata_path, backup)
                    for session_id in removable:
                        conversations.pop(session_id, None)
                    _write_json_atomically(metadata_path, metadata)
                    updates = result["index_updates"]
                    assert isinstance(updates, list)
                    updates.append(
                        {
                            "path": str(metadata_path),
                            "backup": str(backup),
                            "removed_session_ids": removable,
                        }
                    )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            result["errors"].append({"path": str(metadata_path), "error": str(exc)})

    for database_path in sorted(gemini_root.rglob("conversation_summaries.db")):
        if quarantine_root is not None and quarantine_root in database_path.parents:
            continue
        try:
            with sqlite3.connect(database_path, timeout=1) as connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_summaries'"
                ).fetchone()
                if table is None:
                    continue
                rows = connection.execute(
                    "SELECT conversation_id, last_modified_time FROM conversation_summaries "
                    "WHERE conversation_id IN ({})".format(",".join("?" for _ in session_ids)),
                    session_ids,
                ).fetchall()
                removable = [
                    row[0]
                    for row in rows
                    if row[0] not in deferred_session_ids
                    if _parse_timestamp(row[1]) is not None
                    and (current - _parse_timestamp(row[1])).total_seconds() >= minimum_age_seconds
                ]
                if not removable:
                    continue
                backup = ensure_quarantine() / database_path.relative_to(gemini_root)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(database_path, backup)
                for sidecar in (
                    database_path.with_name(database_path.name + "-wal"),
                    database_path.with_name(database_path.name + "-shm"),
                ):
                    if sidecar.exists():
                        sidecar_backup = ensure_quarantine() / sidecar.relative_to(gemini_root)
                        sidecar_backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(sidecar, sidecar_backup)
                connection.executemany(
                    "DELETE FROM conversation_summaries WHERE conversation_id = ?",
                    [(session_id,) for session_id in removable],
                )
                connection.commit()
                updates = result["index_updates"]
                assert isinstance(updates, list)
                updates.append(
                    {
                        "path": str(database_path),
                        "backup": str(backup),
                        "removed_session_ids": removable,
                    }
                )
        except (OSError, sqlite3.Error, ValueError) as exc:
            result["errors"].append({"path": str(database_path), "error": str(exc)})

    moved = result["moved"]
    updates = result["index_updates"]
    if moved or updates:
        result["status"] = "applied"
        result["quarantine_root"] = str(quarantine_root)
        manifest = dict(result)
        assert quarantine_root is not None
        (quarantine_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif result["errors"]:
        result["status"] = "failed"
    elif result["deferred"]:
        result["status"] = "deferred"
    return result

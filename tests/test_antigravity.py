from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import os

from agent_session_vault.antigravity import repair_stale_sessions, stale_session_ids


SESSION_ID = "70bd19ec-077a-43f4-9f67-f709a93fdcf1"


def _warning(session_id: str = SESSION_ID, message: str = "trajectory not found") -> str:
    return (
        f"Warning: failed to fetch Antigravity metadata for session {session_id}: "
        f"HTTPS RPC failed: not_found: {message}\n"
    )


def _warnings() -> str:
    return _warning("tempmediaStorage") + _warning()


def test_stale_session_ids_requires_trajectory_not_found() -> None:
    assert stale_session_ids(_warning()) == [SESSION_ID]
    assert stale_session_ids(_warning(message="permission denied")) == []


def test_repair_stale_sessions_quarantines_artifacts_and_indexes(tmp_path: Path) -> None:
    source_home = tmp_path / "home"
    gemini = source_home / ".gemini"
    brain = gemini / "antigravity" / "brain" / "tempmediaStorage"
    brain.mkdir(parents=True)
    ide_conversations = gemini / "antigravity-ide" / "conversations"
    ide_conversations.mkdir(parents=True)
    conversation = ide_conversations / f"{SESSION_ID}.pb"
    conversation.write_bytes(b"old conversation")
    old_time = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(conversation, (old_time, old_time))

    metadata = gemini / "antigravity-cli" / "cache" / "conversation_metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            {
                "conversations": {
                    SESSION_ID: {
                        "summary": {"ID": SESSION_ID, "UpdatedAt": "2026-03-14T07:28:09Z"},
                        "last_modified_time": "2026-03-14T07:28:09Z",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    database = gemini / "antigravity-cli" / "conversation_summaries.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "create table conversation_summaries (conversation_id text primary key, last_modified_time datetime)"
        )
        connection.execute(
            "insert into conversation_summaries values (?, ?)",
            (SESSION_ID, "2026-03-14 07:28:09+00:00"),
        )
        connection.commit()

    result = repair_stale_sessions(
        source_home,
        _warnings(),
        now=datetime(2026, 11, 17, tzinfo=UTC),
    )

    assert result["status"] == "applied"
    assert brain.exists() is False
    assert conversation.exists() is False
    assert json.loads(metadata.read_text(encoding="utf-8"))["conversations"] == {}
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "select count(*) from conversation_summaries where conversation_id = ?", (SESSION_ID,)
        ).fetchone()[0] == 0
    quarantine = Path(str(result["quarantine_root"]))
    assert (quarantine / "manifest.json").is_file()
    assert list(quarantine.rglob(f"{SESSION_ID}.pb"))


def test_repair_stale_sessions_defers_recent_nonempty_artifact(tmp_path: Path) -> None:
    source_home = tmp_path / "home"
    brain = source_home / ".gemini" / "antigravity-ide" / "brain" / SESSION_ID
    brain.mkdir(parents=True)
    (brain / "session.json").write_text("{}\n", encoding="utf-8")
    metadata = source_home / ".gemini" / "antigravity-cli" / "cache" / "conversation_metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            {
                "conversations": {
                    SESSION_ID: {
                        "summary": {"ID": SESSION_ID, "UpdatedAt": "2026-11-17T00:00:00Z"},
                        "last_modified_time": "2026-11-17T00:00:00Z",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = repair_stale_sessions(
        source_home,
        _warning(),
        now=datetime(2026, 11, 17, 1, tzinfo=UTC),
    )

    assert result["status"] == "deferred"
    assert brain.is_dir()
    assert not result.get("quarantine_root")


def test_repair_stale_sessions_defers_old_directory_with_recent_file(tmp_path: Path) -> None:
    source_home = tmp_path / "home"
    brain = source_home / ".gemini" / "antigravity-ide" / "brain" / SESSION_ID
    brain.mkdir(parents=True)
    child = brain / "session.json"
    child.write_text("{}\n", encoding="utf-8")
    old_time = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(brain, (old_time, old_time))

    result = repair_stale_sessions(
        source_home,
        _warning(),
        now=datetime.now(UTC),
    )

    assert result["status"] == "deferred"
    assert child.is_file()

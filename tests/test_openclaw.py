from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from agent_session_vault.config import load_config
from agent_session_vault.openclaw import openclaw_prepare_sqlite
from agent_session_vault.projection import (
    _build_openclaw_projection_file,
    _remote_helper_source,
    fleet_projection_request,
    import_machine_projection,
    local_home_projection_root,
    refresh_local_home_projection,
)
from agent_session_vault.views import build_tokscale_view


def _compress(text: str) -> bytes:
    return subprocess.run(["zstd", "-q", "-c"], input=text.encode(), capture_output=True, check=True).stdout


def _event(identity="a1", timestamp=1791331200000):
    return {
        "type": "message", "id": identity, "timestamp": "2026-10-07T00:00:00Z",
        "message": {
            "role": "assistant", "model": "deepseek-flash", "provider": "gflab",
            "timestamp": timestamp,
            "usage": {"input": 100, "output": 25, "cacheRead": 200, "reasoningTokens": 5},
            "content": [{"type": "text", "text": "private-conversation"}],
            "diagnostics": {"private": "private-diagnostics"},
        },
    }


def _database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;"
        "CREATE TABLE session_windows(session_id TEXT PRIMARY KEY, model_provider TEXT, model TEXT);"
        "INSERT INTO session_windows VALUES('session-a','gflab','deepseek-flash');"
        "CREATE TABLE transcript_events(session_id TEXT,seq INTEGER,event_json TEXT,created_at INTEGER,"
        "event_zstd BLOB,event_utf8_bytes INTEGER);"
        "CREATE TABLE auth_profile_store(secret TEXT);"
        "INSERT INTO auth_profile_store VALUES('private-credential');"
        "CREATE TABLE session_transcript_archives(session_id TEXT,encoding TEXT,archive_blob BLOB);"
        "CREATE TABLE session_transcript_cold_archives(session_id TEXT,archive_blob BLOB);"
    )
    connection.commit()
    return connection


def _insert(connection, seq, event, compressed=False):
    text = json.dumps(event)
    connection.execute("INSERT INTO transcript_events VALUES(?,?,?,?,?,?)", (
        "session-a", seq, None if compressed else text, 1791331200000 + seq,
        _compress(text) if compressed else None, len(text.encode()) if compressed else None,
    ))
    connection.commit()


def _config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[paths]\n" + "\n".join(
        f'{key} = "{tmp_path / value}"' for key, value in {
            "home": "home", "import_root": "imports", "projection_home": "projection-home",
            "local_workspace_extras": "extras", "stable_root": "stable",
        }.items()
    ))
    return load_config(path)


def _rows(path):
    with sqlite3.connect(path) as connection:
        return [json.loads(row[0]) for row in connection.execute("SELECT event_json FROM transcript_events ORDER BY seq")]


def test_sqlite_wal_compression_archives_privacy_and_history(tmp_path):
    source = tmp_path / "live.sqlite"
    destination = tmp_path / "cache" / "openclaw-agent.sqlite"
    with _database(source) as connection:
        _insert(connection, 1, {"type": "model_change", "modelId": "deepseek-flash", "provider": "gflab"}, True)
        _insert(connection, 2, _event(), True)
        _insert(connection, 3, _event("a2", 1791331201000))
        connection.execute("INSERT INTO session_transcript_archives VALUES(?,?,?)", (
            "session-a", "zstd", _compress(json.dumps(_event("archived", 1791331199000))),
        ))
        connection.execute("INSERT INTO session_transcript_cold_archives VALUES(?,?)", (
            "session-a", _compress(json.dumps(_event("cold", 1791331198000))),
        ))
        connection.commit()
        openclaw_prepare_sqlite(source, destination)
        assert {row.get("id") for row in _rows(destination)} == {None, "a1", "a2", "archived", "cold"}
        assert b"private-" not in destination.read_bytes()
        original = destination.read_bytes()
        openclaw_prepare_sqlite(source, destination)
        assert destination.read_bytes() == original
        connection.execute("DELETE FROM transcript_events")
        connection.execute("DELETE FROM session_transcript_archives")
        connection.execute("DELETE FROM session_transcript_cold_archives")
        connection.commit()
        _insert(connection, 4, _event("new", 1791331202000))
        openclaw_prepare_sqlite(source, destination)
        assert {row.get("id") for row in _rows(destination)} == {None, "a1", "a2", "archived", "cold", "new"}


@pytest.mark.parametrize("suffix", [".zst", ".gz", ""])
def test_archives_decode_and_remote_projector_matches_local(tmp_path, suffix):
    source = tmp_path / f"session-a.jsonl.deleted.2026-10-07{suffix}"
    text = json.dumps(_event()) + "\n"
    source.write_bytes(_compress(text) if suffix == ".zst" else gzip.compress(text.encode()) if suffix else text.encode())
    local = tmp_path / "local.jsonl"
    remote = tmp_path / "remote.jsonl"
    _build_openclaw_projection_file(source, local)
    namespace = {"__name__": "projection_helper_test"}
    exec(compile(_remote_helper_source(), "<remote>", "exec"), namespace)
    namespace["_build_openclaw_projection_file"](source, remote)
    assert local.read_bytes() == remote.read_bytes()
    row = json.loads(local.read_text())
    assert row["id"] == "a1"
    assert row["message"]["usage"]["cacheRead"] == 200
    assert "private-" not in local.read_text()


def test_local_projection_native_scan_layout_and_wal_refresh(tmp_path):
    config = _config(tmp_path)
    source = config.paths.home / ".openclaw/agents/main/agent/openclaw-agent.sqlite"
    with _database(source) as connection:
        _insert(connection, 1, _event(), True)
        first = refresh_local_home_projection(config)
        second = refresh_local_home_projection(config)
        assert first.files_written == 1
        assert second.files_written == 0
        projected = next((local_home_projection_root(config) / ".raw/openclaw").rglob("openclaw-agent.sqlite"))
        roots = [root for client, root in build_tokscale_view(config).extra_dirs if client == "openclaw"]
        assert projected == roots[0] / "main/agent/openclaw-agent.sqlite"
        _insert(connection, 2, _event("a2", 1791331201000))
        third = refresh_local_home_projection(config)
        assert third.files_written == 1
        assert len(_rows(projected)) == 2


def test_fleet_embeds_sqlite_collector_and_keeps_deleted_history(tmp_path):
    config = _config(tmp_path)
    source_home = tmp_path / "source-home"
    source = source_home / ".openclaw/agents/main/agent/openclaw-agent.sqlite"
    with _database(source) as connection:
        _insert(connection, 1, _event(), True)
        previous = None
        for index in range(2):
            script, _ = fleet_projection_request("node-a", snapshot_id=f"run-{index}", base_snapshot_id=previous)
            run = subprocess.run(
                [sys.executable, "-"], input=script, text=True, capture_output=True, check=True,
                env={**os.environ, "HOME": str(source_home)},
            )
            payload = json.loads(run.stdout)
            imported = import_machine_projection(config, "node-a", Path(payload["bundle_dir"]))
            previous = imported.snapshot_id
            connection.execute("DELETE FROM transcript_events")
            connection.commit()
            _insert(connection, 2, _event("a2", 1791331201000))
        projected = next((config.paths.import_root / "node-a/.raw/openclaw").rglob("openclaw-agent.sqlite"))
        assert {row["id"] for row in _rows(projected)} == {"a1", "a2"}


def test_failed_decode_does_not_replace_valid_projection(tmp_path):
    source = tmp_path / "live.sqlite"
    target = tmp_path / "cache/openclaw-agent.sqlite"
    with _database(source) as connection:
        _insert(connection, 1, _event())
        openclaw_prepare_sqlite(source, target)
        before = target.read_bytes()
        connection.execute("INSERT INTO transcript_events VALUES('session-a',2,NULL,1,?,10)", (b"corrupt",))
        connection.commit()
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            openclaw_prepare_sqlite(source, target)
        assert target.read_bytes() == before


def test_agent_owned_codex_rollout_uses_codex_projection(tmp_path):
    source = tmp_path / "main/agent/codex-home/sessions/rollout.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text("\n".join(json.dumps(row) for row in [
        {"type": "turn_context", "payload": {"model": "deepseek-flash", "private": "private-prompt"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 123, "output_tokens": 4},
        }}},
    ]) + "\n")
    projected = tmp_path / "projected.jsonl"
    _build_openclaw_projection_file(source, projected)
    assert "private-prompt" not in projected.read_text()
    rows = [json.loads(line) for line in projected.read_text().splitlines()]
    assert rows[0]["payload"]["model"] == "deepseek-flash"
    assert rows[1]["payload"]["info"]["last_token_usage"]["input_tokens"] == 123


def test_projection_dry_run_never_snapshots_live_database(tmp_path):
    config = _config(tmp_path)
    source = config.paths.home / ".openclaw/agents/main/agent/openclaw-agent.sqlite"
    with _database(source) as connection:
        _insert(connection, 1, _event(), True)
        result = refresh_local_home_projection(config, dry_run=True)
        assert result.files_seen == 1
        assert not config.paths.import_root.exists()


def test_openclaw_projector_upgrade_keeps_other_clients_incremental(tmp_path):
    config = _config(tmp_path)
    codex = config.paths.home / ".codex/sessions/rollout.jsonl"
    codex.parent.mkdir(parents=True)
    codex.write_text('{"type":"event_msg","payload":{"type":"token_count"}}\n')
    transcript = config.paths.home / ".openclaw/agents/main/sessions/session-a.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps(_event()) + "\n")
    first = refresh_local_home_projection(config)
    state = json.loads(first.state_path.read_text())
    state.pop("openclaw_projector_version")
    first.state_path.write_text(json.dumps(state))
    second = refresh_local_home_projection(config)
    assert second.files_written == 1
    assert second.files_skipped == 1

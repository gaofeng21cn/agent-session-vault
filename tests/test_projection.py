from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from agent_session_vault.config import load_config
from agent_session_vault.projection import (
    CODEX_PROJECTION_VERSION,
    _remote_helper_source,
    build_codex_projection_file,
    fleet_projection_request,
    import_machine_projection,
    local_home_projection_root,
    refresh_local_home_projection,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _codex_fixture() -> list[dict]:
    return [
        {
            "timestamp": "2026-07-14T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "session-1",
                "forked_from_id": "parent-1",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent-1"}}},
                "cwd": "/workspace/project",
                "private": "drop-private",
            },
        },
        {
            "timestamp": "2026-07-14T00:00:01Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model_info": {"slug": "gpt-5.4"}, "private": "drop-turn"},
        },
        {
            "timestamp": "2026-07-14T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "drop-user-body"},
        },
        {
            "timestamp": "2026-07-14T00:00:03Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "turn_id": "turn-1", "info": {"total_token_usage": {"input_tokens": 10}}},
        },
        {
            "timestamp": "2026-07-14T00:00:04Z",
            "type": "response_item",
            "payload": {"content": "drop-response-content"},
        },
        {
            "timestamp": "2026-07-14T00:00:05Z",
            "type": "turn.completed",
            "model": "gpt-5.4",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    ]


def test_codex_projection_preserves_order_and_tokscale_fields(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    destination = tmp_path / "projected.jsonl"
    fixture = _codex_fixture()
    _write(source, "\n".join(json.dumps(item) for item in fixture) + "\n")

    result = build_codex_projection_file(source, destination)
    projected = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]

    assert [item["type"] for item in projected] == [item["type"] for item in fixture]
    assert projected[0]["payload"]["forked_from_id"] == "parent-1"
    assert projected[1]["payload"]["model_info"] == {"slug": "gpt-5.4"}
    assert projected[2]["payload"]["message"] == "user"
    assert projected[3]["payload"]["turn_id"] == "turn-1"
    assert projected[5]["usage"] == fixture[5]["usage"]
    assert result["token_events"] == 1
    assert "drop-" not in destination.read_text(encoding="utf-8")


def test_remote_codex_projector_matches_local_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    local_destination = tmp_path / "local.jsonl"
    remote_destination = tmp_path / "remote.jsonl"
    _write(source, "\n".join(json.dumps(item) for item in _codex_fixture()) + "\n")
    build_codex_projection_file(source, local_destination)

    namespace = {"__name__": "projection_helper_test"}
    exec(compile(_remote_helper_source(), "<remote-projection-helper>", "exec"), namespace)
    remote_result = namespace["_build_codex_projection_file"](source, remote_destination)

    assert remote_destination.read_bytes() == local_destination.read_bytes()
    assert remote_result["token_events"] == 1


def _config(tmp_path: Path, home: Path) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
import_root = "{tmp_path / 'imports'}"
projection_home = "{tmp_path / 'projection-home'}"
local_workspace_extras = "{tmp_path / 'extras'}"
stable_root = "{tmp_path / 'stable'}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


def test_refresh_local_home_projection_is_incremental_and_slim(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_session = home / ".codex" / "sessions" / "local.jsonl"
    _write(codex_session, "\n".join(json.dumps(item) for item in _codex_fixture()) + "\n")
    _write(home / ".gemini" / "tmp" / "project" / "chats" / "chat.json", '{"sessionId":"g-1"}\n')
    _write(home / ".gemini" / "tmp" / "project" / "cache.json", '{"ignored":true}\n')
    _write(
        home / ".openclaw" / "agents" / "agent" / "sessions" / "session.jsonl",
        json.dumps(
            {
                "type": "message",
                "message": {
                    "content": [{"type": "text", "text": "drop-this"}],
                    "usage": {"input": 10, "output": 2},
                },
            }
        )
        + "\n",
    )
    _write(home / ".config" / "tokscale" / "credentials.json", '{"token":"test"}\n')
    config = load_config(_config(tmp_path, home))

    first = refresh_local_home_projection(config)
    second = refresh_local_home_projection(config)

    assert first.files_seen == 3
    assert first.files_written == 3
    assert set(first.clients) == {"codex", "gemini", "openclaw"}
    assert second.files_written == 0
    assert second.files_skipped == 3
    assert json.loads(first.state_path.read_text(encoding="utf-8"))["projector_version"] == CODEX_PROJECTION_VERSION
    assert (first.projection_home / ".config" / "tokscale" / "credentials.json").is_symlink()

    projected_root = local_home_projection_root(config) / ".raw"
    projected_codex = next((projected_root / "codex").rglob("local.jsonl"))
    projected_openclaw = next((projected_root / "openclaw").rglob("session.jsonl"))
    assert "drop-" not in projected_codex.read_text(encoding="utf-8")
    assert "drop-this" not in projected_openclaw.read_text(encoding="utf-8")
    assert not list((projected_root / "gemini").rglob("cache.json"))


def test_fleet_projection_request_exports_delta_and_imports_it(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    source_file = source_home / ".codex" / "sessions" / "one.jsonl"
    _write(source_file, '{"type":"event_msg","payload":{"type":"token_count","total":1}}\n')
    config = load_config(_config(tmp_path, tmp_path / "target-home"))

    first_script, _ = fleet_projection_request("node-a", snapshot_id="node-a-001")
    first_run = subprocess.run(
        [sys.executable, "-"],
        input=first_script,
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "HOME": str(source_home)},
    )
    first_payload = json.loads(first_run.stdout)
    first = import_machine_projection(config, "node-a", Path(first_payload["bundle_dir"]))

    assert first.mode == "projection_full"
    assert first.state_status == "rebuilt"
    assert (config.paths.import_root / "node-a" / ".raw" / "codex").is_dir()

    _write(source_file, '{"type":"event_msg","payload":{"type":"token_count","total":22}}\n')
    second_script, _ = fleet_projection_request(
        "node-a",
        snapshot_id="node-a-002",
        base_snapshot_id=first.snapshot_id,
    )
    second_run = subprocess.run(
        [sys.executable, "-"],
        input=second_script,
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "HOME": str(source_home)},
    )
    second_payload = json.loads(second_run.stdout)
    second = import_machine_projection(config, "node-a", Path(second_payload["bundle_dir"]))

    assert second.mode == "projection_delta"
    assert second.base_snapshot_id == first.snapshot_id
    assert second.state_status == "incremental"

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from agent_session_vault.config import load_config
from agent_session_vault.projection import (
    CODEX_PROJECTION_VERSION,
    _remote_helper_source,
    build_codex_projection_file,
    export_machine_projection,
    fetch_projection_bundle_ssh,
    import_machine_projection,
    local_home_projection_root,
    pending_projection_bundle_dirs,
    refresh_local_home_projection,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _codex_state_machine_fixture() -> list[dict]:
    return [
        {
            "timestamp": "2026-07-14T00:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": "01900000-0000-7000-8000-000000000002",
                "forked_from_id": "01900000-0000-7000-8000-000000000001",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent-1"}}},
                "thread_source": "subagent",
                "cwd": "/workspace/project",
                "model_provider": "openai",
                "agent_nickname": "worker",
                "private": "drop-session-private",
            },
        },
        {
            "timestamp": "2026-07-14T00:00:00.001Z",
            "type": "session_meta",
            "payload": {"id": "01900000-0000-7000-8000-000000000001"},
        },
        {
            "timestamp": "2026-07-14T00:00:00.002Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "keep the turn boundary, drop this private prompt",
            },
        },
        {
            "timestamp": "2026-07-14T00:00:00.003Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "  <environment_context>drop injected body</environment_context>",
            },
        },
        {
            "timestamp": "2026-07-14T00:00:00.004Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "01900000-0001-7000-8000-000000000003"},
        },
        {
            "timestamp": "2026-07-14T00:00:00.005Z",
            "type": "turn_context",
            "payload": {
                "model_info": {"slug": "gpt-5.4"},
                "turn_id": "01900000-0001-7000-8000-000000000003",
                "private": "drop-turn-private",
            },
        },
        {
            "timestamp": "2026-07-14T00:00:00.006Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "model": "gpt-5.4",
                    "last_token_usage": {"input_tokens": 10, "output_tokens": 2},
                    "total_token_usage": {"input_tokens": 100, "output_tokens": 20},
                },
            },
        },
        {
            "timestamp": "2026-07-14T00:00:00.007Z",
            "type": "response_item",
            "payload": {"content": "drop-response-content"},
        },
        {
            "timestamp": "2026-07-14T00:00:00.008Z",
            "type": "turn.completed",
            "model": "gpt-4o-mini",
            "usage": {"input_tokens": 12, "cached_input_tokens": 4, "output_tokens": 3},
        },
    ]


def test_codex_projection_preserves_tokscale_state_machine_order_without_conversation_body(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    destination = tmp_path / "projected.jsonl"
    fixture = _codex_state_machine_fixture()
    _write(source, "\n".join(json.dumps(item) for item in fixture) + "\n")

    result = build_codex_projection_file(source, destination)
    projected = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]

    assert [item["type"] for item in projected] == [item["type"] for item in fixture]
    assert projected[0]["payload"]["forked_from_id"] == fixture[0]["payload"]["forked_from_id"]
    assert projected[0]["payload"]["source"] == fixture[0]["payload"]["source"]
    assert projected[1]["payload"]["id"] == fixture[1]["payload"]["id"]
    assert projected[2]["payload"]["message"] == "user"
    assert projected[3]["payload"]["message"] == "<environment_context>"
    assert projected[4]["payload"]["turn_id"] == fixture[4]["payload"]["turn_id"]
    assert projected[5]["payload"]["model_info"] == {"slug": "gpt-5.4"}
    assert projected[6]["payload"]["info"] == fixture[6]["payload"]["info"]
    assert projected[8]["usage"] == fixture[8]["usage"]
    assert result["token_events"] == 1
    assert "drop-" not in destination.read_text(encoding="utf-8")


def test_remote_codex_projector_matches_local_v2_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    local_destination = tmp_path / "local.jsonl"
    remote_destination = tmp_path / "remote.jsonl"
    _write(source, "\n".join(json.dumps(item) for item in _codex_state_machine_fixture()) + "\n")
    build_codex_projection_file(source, local_destination)

    namespace = {"__name__": "projection_helper_test"}
    exec(compile(_remote_helper_source(), "<remote-projection-helper>", "exec"), namespace)
    remote_result = namespace["_build_codex_projection_file"](source, remote_destination)

    assert remote_destination.read_bytes() == local_destination.read_bytes()
    assert remote_result["token_events"] == 1


def _write_config(
    tmp_path: Path,
    *,
    source_home: Path,
    target_home: Path,
    clients: list[str],
    root_blocks: str,
) -> Path:
    imports = target_home / ".config" / "tokscale" / "imports"
    relay_root = tmp_path / "relay"
    archive_root = tmp_path / "archive"
    workspace_root = tmp_path / "workspace"
    shadow_home = target_home / ".config" / "tokscale" / "shadow-home"
    extras = target_home / ".config" / "tokscale" / "local-workspace-extras"
    config_path = tmp_path / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"""
[paths]
home = "{target_home}"
workspace_root = "{workspace_root}"
import_root = "{imports}"
shadow_home = "{shadow_home}"
local_workspace_extras = "{extras}"
archive_root = "{archive_root}"
relay_root = "{relay_root}"

[sync]
projection_transport = "auto"
projection_direct_max_bundle_bytes = 1073741824

[machines.machine-a]
import_name = "machine-a"
ssh_target = "session-sync-a"
source_home = "{source_home}"
remote_relay_root = "{relay_root}"
remote_state_root = "{tmp_path / "remote-state"}"
clients = {json.dumps(clients)}

{root_blocks.strip()}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


def test_refresh_local_home_projection_is_incremental_and_strips_conversation_content(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_session = home / ".codex" / "sessions" / "2026" / "07" / "14" / "local.jsonl"
    _write(
        codex_session,
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "local-1"}}),
                json.dumps({"type": "turn_context", "payload": {"model_info": {"slug": "gpt-5.4"}}}),
                json.dumps({"type": "event_msg", "payload": {"type": "token_count", "total": 123}}),
                json.dumps({"type": "response_item", "payload": {"secret": "drop-this"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
            ]
        )
        + "\n",
    )
    _write(home / ".gemini" / "tmp" / "project" / "chats" / "chat.json", '{"sessionId":"g-1"}\n')
    _write(home / ".config" / "tokscale" / "credentials.json", '{"token":"test"}\n')
    _write(home / ".config" / "tokscale" / "device.json", '{"device":"test"}\n')
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
    config = load_config(
        _write_config(
            tmp_path,
            source_home=tmp_path / "remote-home",
            target_home=home,
            clients=["codex"],
            root_blocks="",
        )
    )

    first = refresh_local_home_projection(config)
    second = refresh_local_home_projection(config)

    assert first.files_seen == 3
    assert first.files_written == 3
    assert set(first.clients) == {"codex", "gemini", "openclaw"}
    assert second.files_seen == 3
    assert second.files_written == 0
    assert second.files_skipped == 3
    assert first.state_path.is_file()
    assert json.loads(first.state_path.read_text(encoding="utf-8"))["projector_version"] == CODEX_PROJECTION_VERSION
    assert first.projection_home.is_dir()
    assert (first.projection_home / ".config" / "tokscale" / "credentials.json").is_symlink()
    assert (first.projection_home / ".config" / "tokscale" / "device.json").is_symlink()

    projected_root = local_home_projection_root(config) / ".raw"
    projected_codex = next((projected_root / "codex").rglob("local.jsonl"))
    projected_openclaw = next((projected_root / "openclaw").rglob("session.jsonl"))
    assert "token_count" in projected_codex.read_text(encoding="utf-8")
    assert "drop-this" not in projected_codex.read_text(encoding="utf-8")
    assert "drop-this" not in projected_openclaw.read_text(encoding="utf-8")
    assert '"usage":{"input":10,"output":2}' in projected_openclaw.read_text(encoding="utf-8")

    with codex_session.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "total": 456}}) + "\n")
    third = refresh_local_home_projection(config)
    assert third.files_written == 1
    assert third.files_skipped == 2
    assert projected_codex.read_text(encoding="utf-8").count("token_count") == 2


def test_projection_export_import_round_trip(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    imports = target_home / ".config" / "tokscale" / "imports"
    relay_root = tmp_path / "relay"
    archive_root = tmp_path / "archive"
    workspace_root = tmp_path / "workspace"
    shadow_home = target_home / ".config" / "tokscale" / "shadow-home"
    extras = target_home / ".config" / "tokscale" / "local-workspace-extras"

    codex_root = source_home / ".codex"
    _write(
        codex_root / "sessions" / "2026" / "04" / "08" / "one.jsonl",
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "sess-1"}}),
                json.dumps({"type": "turn_context", "payload": {"model_info": {"slug": "gpt-5.4"}}}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "model": "gpt-5.4",
                                "last_token_usage": {"input_tokens": 10, "output_tokens": 2},
                            },
                        },
                    }
                ),
                json.dumps({"type": "response_item", "payload": {"huge": "drop-me"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
            ]
        )
        + "\n",
    )
    _write(source_home / ".gemini" / "tmp" / "proj-a" / "chats" / "chat.json", '{"sessionId":"g-1"}\n')
    _write(source_home / ".gemini" / "tmp" / "proj-a" / "logs.json", '{"noise":true}\n')

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{target_home}"
workspace_root = "{workspace_root}"
import_root = "{imports}"
shadow_home = "{shadow_home}"
local_workspace_extras = "{extras}"
archive_root = "{archive_root}"
relay_root = "{relay_root}"

[sync]
projection_transport = "auto"
projection_direct_max_bundle_bytes = 1073741824

[machines.machine-a]
import_name = "machine-a"
ssh_target = "session-sync-a"
source_home = "{source_home}"
remote_relay_root = "{relay_root}"
remote_state_root = "{tmp_path / "remote-state"}"
clients = ["codex", "gemini"]

[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"

[[machines.machine-a.roots]]
client = "gemini"
path = "~/.gemini"
kind = "home_root"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)

    bundle = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=relay_root,
    )
    imported = import_machine_projection(config, "machine-a", bundle.bundle_dir, canonicalize_command=None)

    projected_codex = imports / "machine-a" / ".raw" / "codex" / "sessions"
    projected_files = sorted(projected_codex.rglob("*.jsonl"))
    assert projected_files
    projected_text = projected_files[0].read_text(encoding="utf-8")
    assert "token_count" in projected_text
    assert "drop-me" not in projected_text

    projected_gemini = imports / "machine-a" / ".raw" / "gemini"
    assert any(path.name == "chat.json" for path in projected_gemini.rglob("chat.json"))
    assert not any(path.name == "logs.json" for path in projected_gemini.rglob("logs.json"))

    manifest = json.loads((bundle.bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    roots_manifest = json.loads((bundle.bundle_dir / "roots-manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "projection_full"
    assert roots_manifest["projector_versions"] == {"codex": CODEX_PROJECTION_VERSION}
    assert imported.snapshot_id == bundle.snapshot_id


def test_projection_version_change_rejects_an_old_delta_baseline(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    source_file = source_home / ".codex" / "sessions" / "2026" / "07" / "14" / "one.jsonl"
    _write(source_file, json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")
    config = load_config(
        _write_config(
            tmp_path,
            source_home=source_home,
            target_home=target_home,
            clients=["codex"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""",
        )
    )

    first = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=config.paths.relay_root,
    )
    roots_manifest = json.loads(first.roots_manifest_path.read_text(encoding="utf-8"))
    roots_manifest.pop("projector_versions")
    first.roots_manifest_path.write_text(json.dumps(roots_manifest) + "\n", encoding="utf-8")

    second = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=config.paths.relay_root,
        base_snapshot_id=first.snapshot_id,
    )

    assert second.mode == "projection_full"
    assert second.base_snapshot_id is None
    assert second.fallback_reason == "roots_manifest_changed"


def test_fetch_projection_bundle_ssh_stages_before_final_bundle_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_bundle_dir = Path("/remote/relay/projection/machine-a/machine-a-000001")
    local_bundle_dir = tmp_path / "OneDrive" / "relay" / "projection" / "machine-a" / "machine-a-000001"
    rsync_destinations: list[Path] = []

    def fake_run(command: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        assert check is True
        destination = Path(command[-1].rstrip("/"))
        rsync_destinations.append(destination)
        if destination == local_bundle_dir:
            raise subprocess.CalledProcessError(23, command)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "manifest.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("agent_session_vault.projection.subprocess.run", fake_run)

    fetched_dir = fetch_projection_bundle_ssh(
        ssh_target="session-sync-a",
        remote_bundle_dir=remote_bundle_dir,
        local_bundle_dir=local_bundle_dir,
    )

    assert fetched_dir == local_bundle_dir
    assert rsync_destinations == [path for path in rsync_destinations if path != local_bundle_dir]
    assert len(rsync_destinations) == 1
    assert (local_bundle_dir / "manifest.json").read_text(encoding="utf-8") == "{}"
    assert not rsync_destinations[0].exists()


def test_gemini_projection_keeps_only_chat_json(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"

    _write(source_home / ".gemini" / "tmp" / "proj-a" / "chats" / "chat.json", '{"sessionId":"g-1"}\n')
    _write(source_home / ".gemini" / "tmp" / "proj-a" / "bin" / "rg", "binary-noise\n")
    _write(source_home / ".gemini" / "tmp" / "proj-a" / "logs.json", '{"noise":true}\n')
    _write(source_home / ".gemini" / "tmp" / "proj-a" / ".project_root", "/tmp/project\n")

    config = load_config(
        _write_config(
            tmp_path,
            source_home=source_home,
            target_home=target_home,
            clients=["gemini"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "gemini"
path = "~/.gemini"
kind = "home_root"
""",
        )
    )

    bundle = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
    )
    import_machine_projection(config, "machine-a", bundle.bundle_dir, canonicalize_command=None)

    projected_gemini = target_home / ".config" / "tokscale" / "imports" / "machine-a" / ".raw" / "gemini"
    projected_files = sorted(path.relative_to(projected_gemini) for path in projected_gemini.rglob("*") if path.is_file())

    assert any(path.parts[-2:] == ("chats", "chat.json") for path in projected_files)
    assert all("chats" in path.parts for path in projected_files)
    assert not any(path.name in {"logs.json", ".project_root", "rg"} for path in projected_files)


def test_openclaw_projection_keeps_jsonl_only_and_slims_message_content(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    openclaw_root = source_home / ".openclaw" / "agents" / "agent-a"

    _write(
        openclaw_root / "main" / "sessions" / "session-1.jsonl",
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session",
                        "version": 3,
                        "id": "session-1",
                        "timestamp": "2026-04-08T00:00:00.000Z",
                        "cwd": "/tmp/workspace",
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "id": "user-1",
                        "parentId": None,
                        "timestamp": "2026-04-08T00:00:01.000Z",
                        "message": {
                            "role": "user",
                            "content": "very long prompt that should be removed",
                            "timestamp": 1775606401000,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "id": "assistant-1",
                        "parentId": "user-1",
                        "timestamp": "2026-04-08T00:00:02.000Z",
                        "message": {
                            "role": "assistant",
                            "api": "openai-responses",
                            "provider": "openclaw",
                            "model": "gpt-5.4",
                            "responseId": "resp-1",
                            "usage": {
                                "input": 10,
                                "output": 5,
                                "cacheRead": 0,
                                "cacheWrite": 0,
                                "totalTokens": 15,
                            },
                            "stopReason": "stop",
                            "timestamp": 1775606402000,
                            "content": [
                                {"type": "thinking", "thinking": "huge reasoning", "thinkingSignature": "sig-1"},
                                {
                                    "type": "toolCall",
                                    "id": "call-1",
                                    "name": "read",
                                    "arguments": {"file": "/tmp/secret.txt"},
                                    "partialJson": "{\"file\":\"/tmp/secret.txt\"}",
                                },
                                {"type": "image", "mimeType": "image/png", "data": "base64-data"},
                                {"type": "text", "text": "large answer body", "textSignature": "sig-2"},
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
    )
    _write(openclaw_root / "main" / "sessions" / "sessions.json.guard-reset.2026-04-08T00-00-00Z", '{"huge":"drop"}\n')
    _write(openclaw_root / "main" / "agent" / "models.json", '{"models":["gpt-5.4"]}\n')
    _write(openclaw_root / "main" / "agent" / "auth-profiles.json", '{"profiles":["default"]}\n')

    config = load_config(
        _write_config(
            tmp_path,
            source_home=source_home,
            target_home=target_home,
            clients=["openclaw"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "openclaw"
path = "~/.openclaw"
kind = "home_root"
""",
        )
    )

    bundle = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
    )
    import_machine_projection(config, "machine-a", bundle.bundle_dir, canonicalize_command=None)

    projected_openclaw = target_home / ".config" / "tokscale" / "imports" / "machine-a" / ".raw" / "openclaw"
    projected_files = sorted(path.relative_to(projected_openclaw) for path in projected_openclaw.rglob("*") if path.is_file())

    assert any(path.name == "session-1.jsonl" for path in projected_files)
    assert not any(path.name.startswith("sessions.json") for path in projected_files)
    assert not any(path.name in {"models.json", "auth-profiles.json"} for path in projected_files)

    projected_session = next(projected_openclaw.rglob("session-1.jsonl"))
    records = [json.loads(line) for line in projected_session.read_text(encoding="utf-8").splitlines() if line.strip()]
    user_message = next(item for item in records if item["type"] == "message" and item["message"]["role"] == "user")
    assistant_message = next(
        item for item in records if item["type"] == "message" and item["message"]["role"] == "assistant"
    )

    assert user_message["message"]["content"] == ""
    assert assistant_message["message"]["usage"]["totalTokens"] == 15
    assert assistant_message["message"]["content"][0]["thinking"] == ""
    assert assistant_message["message"]["content"][1]["arguments"] == {}
    assert assistant_message["message"]["content"][1]["partialJson"] == ""
    assert assistant_message["message"]["content"][2]["data"] == ""
    assert assistant_message["message"]["content"][3]["text"] == ""


def test_openclaw_projection_normalizes_non_reset_suffix_variants_to_jsonl(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    openclaw_root = source_home / ".openclaw" / "agents" / "agent-a"

    _write(
        openclaw_root / "main" / "sessions" / "variant.jsonl.guard-reset.2026-04-08T00-00-00Z",
        json.dumps(
            {
                "type": "message",
                "id": "assistant-1",
                "parentId": None,
                "timestamp": "2026-04-08T00:00:02.000Z",
                "message": {
                    "role": "assistant",
                    "api": "openai-responses",
                    "provider": "openclaw",
                    "model": "gpt-5.4",
                    "responseId": "resp-1",
                    "usage": {
                        "input": 10,
                        "output": 5,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "totalTokens": 15,
                    },
                    "stopReason": "stop",
                    "timestamp": 1775606402000,
                    "content": [{"type": "text", "text": "large answer body"}],
                },
            }
        )
        + "\n",
    )

    config = load_config(
        _write_config(
            tmp_path,
            source_home=source_home,
            target_home=target_home,
            clients=["openclaw"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "openclaw"
path = "~/.openclaw"
kind = "home_root"
""",
        )
    )

    bundle = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
    )
    import_machine_projection(config, "machine-a", bundle.bundle_dir, canonicalize_command=None)

    projected_openclaw = target_home / ".config" / "tokscale" / "imports" / "machine-a" / ".raw" / "openclaw"
    projected_files = sorted(path.relative_to(projected_openclaw) for path in projected_openclaw.rglob("*") if path.is_file())
    normalized = [path for path in projected_files if path.name.endswith(".jsonl") and "_normalized" in path.parts]

    assert normalized
    assert normalized[0].name == "variant__jsonl__.guard-reset.2026-04-08T00-00-00Z.jsonl"


def test_projection_export_without_local_state_falls_back_to_full(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"

    _write(
        source_home / ".codex" / "sessions" / "2026" / "04" / "08" / "one.jsonl",
        json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n",
    )

    config = load_config(
        _write_config(
            tmp_path,
            source_home=source_home,
            target_home=target_home,
            clients=["codex"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""",
        )
    )

    bundle = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=target_home / ".config" / "tokscale" / "imports" / "machine-a",
    )
    manifest = json.loads((bundle.bundle_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["mode"] == "projection_full"
    assert manifest["fallback_reason"] == "missing_local_state"


def test_projection_export_with_existing_state_emits_delta_and_skips_unchanged_files(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    import_root = target_home / ".config" / "tokscale" / "imports" / "machine-a"

    source_file = source_home / ".codex" / "sessions" / "2026" / "04" / "08" / "one.jsonl"
    _write(
        source_file,
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "sess-1"}}),
                json.dumps({"type": "turn_context", "payload": {"model_info": {"slug": "gpt-5.4"}}}),
                json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}),
            ]
        )
        + "\n",
    )

    config = load_config(
        _write_config(
            tmp_path,
            source_home=source_home,
            target_home=target_home,
            clients=["codex"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""",
        )
    )

    first = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    import_machine_projection(config, "machine-a", first.bundle_dir, canonicalize_command=None)

    _write(
        source_file,
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "sess-1"}}),
                json.dumps({"type": "turn_context", "payload": {"model_info": {"slug": "gpt-5.4"}}}),
                json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "token_count", "payload": {"round": 2}}}),
            ]
        )
        + "\n",
    )
    _write(
        source_home / ".codex" / "sessions" / "2026" / "04" / "08" / "two.jsonl",
        json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n",
    )

    second = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    manifest = json.loads((second.bundle_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["mode"] == "projection_delta"
    assert manifest["base_snapshot_id"] == first.snapshot_id
    assert sorted(Path(path).name for path in manifest["changed_files"]) == ["one.jsonl", "two.jsonl"]


def test_projection_delta_import_preserves_locally_accumulated_files_deleted_remotely(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    import_root = target_home / ".config" / "tokscale" / "imports" / "machine-a"

    keep_file = source_home / ".codex" / "sessions" / "2026" / "04" / "08" / "keep.jsonl"
    drop_file = source_home / ".codex" / "sessions" / "2026" / "04" / "08" / "drop.jsonl"
    _write(keep_file, json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")
    _write(drop_file, json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")

    config = load_config(
        _write_config(
            tmp_path,
            source_home=source_home,
            target_home=target_home,
            clients=["codex"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""",
        )
    )

    first = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    import_machine_projection(config, "machine-a", first.bundle_dir, canonicalize_command=None)

    drop_file.unlink()

    second = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    manifest = json.loads((second.bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    import_machine_projection(config, "machine-a", second.bundle_dir, canonicalize_command=None)

    projected_root = import_root / ".raw" / "codex" / "sessions"
    assert manifest["mode"] == "projection_delta"
    assert sorted(Path(path).name for path in manifest["deleted_files"]) == ["drop.jsonl"]
    assert any(path.name == "keep.jsonl" for path in projected_root.rglob("*.jsonl"))
    assert any(path.name == "drop.jsonl" for path in projected_root.rglob("*.jsonl"))


def test_pending_projection_bundle_dirs_returns_latest_applicable_delta(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    import_root = target_home / ".config" / "tokscale" / "imports" / "machine-a"

    source_file = source_home / ".codex" / "sessions" / "2026" / "04" / "08" / "one.jsonl"
    _write(source_file, json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")

    config = load_config(
        _write_config(
            tmp_path,
            source_home=source_home,
            target_home=target_home,
            clients=["codex"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""",
        )
    )

    first = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    import_machine_projection(config, "machine-a", first.bundle_dir, canonicalize_command=None)

    _write(source_file, json.dumps({"type": "event_msg", "payload": {"type": "token_count", "n": 2}}) + "\n")
    second = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    _write(source_file, json.dumps({"type": "event_msg", "payload": {"type": "token_count", "n": 3}}) + "\n")
    third = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )

    pending = pending_projection_bundle_dirs(config, "machine-a")
    assert pending == [third.bundle_dir]


def test_pending_projection_bundle_dirs_skips_stale_snapshot_dirs_before_reading_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    import_root = target_home / ".config" / "tokscale" / "imports" / "machine-a"

    config = load_config(
        _write_config(
            tmp_path,
            source_home=source_home,
            target_home=target_home,
            clients=["codex"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""",
        )
    )

    current_snapshot_id = "machine-a-20260413T191545309655Z"
    _write(import_root / ".projection-state.json", json.dumps({"current_snapshot_id": current_snapshot_id}) + "\n")

    stale_bundle_dir = config.paths.relay_root / "projection" / "machine-a" / "machine-a-20260413T103630441243Z"
    stale_manifest_path = stale_bundle_dir / "manifest.json"
    _write(stale_manifest_path, "{}\n")

    original_read_text = Path.read_text

    def fake_read_text(path: Path, *args, **kwargs) -> str:
        if path == stale_manifest_path:
            raise OSError(11, "Resource deadlock avoided")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    pending = pending_projection_bundle_dirs(config, "machine-a")
    assert pending == []


def test_projection_export_with_roots_manifest_change_falls_back_to_full(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    import_root = target_home / ".config" / "tokscale" / "imports" / "machine-a"

    _write(
        source_home / ".codex" / "sessions" / "2026" / "04" / "08" / "one.jsonl",
        json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n",
    )

    initial_config = load_config(
        _write_config(
            tmp_path / "initial",
            source_home=source_home,
            target_home=target_home,
            clients=["codex"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""",
        )
    )

    first = export_machine_projection(
        machine=initial_config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    import_machine_projection(initial_config, "machine-a", first.bundle_dir, canonicalize_command=None)

    _write(
        source_home / ".gemini" / "tmp" / "proj-a" / "chats" / "chat.json",
        '{"sessionId":"g-1"}\n',
    )

    changed_config = load_config(
        _write_config(
            tmp_path / "changed",
            source_home=source_home,
            target_home=target_home,
            clients=["codex", "gemini"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"

[[machines.machine-a.roots]]
client = "gemini"
path = "~/.gemini"
kind = "home_root"
""",
        )
    )

    second = export_machine_projection(
        machine=changed_config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    manifest = json.loads((second.bundle_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["mode"] == "projection_full"
    assert manifest["fallback_reason"] == "roots_manifest_changed"


def test_projection_full_import_preserves_removed_root_content_for_local_tokscale_history(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    import_root = target_home / ".config" / "tokscale" / "imports" / "machine-a"

    openclaw_session = source_home / ".openclaw" / "agents" / "agent-a" / "main" / "sessions" / "session-1.jsonl"
    _write(
        openclaw_session,
        json.dumps({"type": "message", "message": {"role": "assistant", "content": "usage"}}) + "\n",
    )

    config = load_config(
        _write_config(
            tmp_path,
            source_home=source_home,
            target_home=target_home,
            clients=["openclaw"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "openclaw"
path = "~/.openclaw"
kind = "home_root"
""",
        )
    )

    first = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    import_machine_projection(config, "machine-a", first.bundle_dir, canonicalize_command=None)

    shutil.rmtree(source_home / ".openclaw")

    second = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    manifest = json.loads((second.bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    import_machine_projection(config, "machine-a", second.bundle_dir, canonicalize_command=None)

    projected_openclaw = import_root / ".raw" / "openclaw"
    projected_files = sorted(path.relative_to(projected_openclaw) for path in projected_openclaw.rglob("*") if path.is_file())

    assert manifest["mode"] == "projection_full"
    assert manifest["fallback_reason"] == "roots_manifest_changed"
    assert len(projected_files) == 1
    assert projected_files[0].name == "session-1.jsonl"


def test_projection_export_with_missing_base_snapshot_falls_back_to_full(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    import_root = target_home / ".config" / "tokscale" / "imports" / "machine-a"

    source_file = source_home / ".codex" / "sessions" / "2026" / "04" / "08" / "one.jsonl"
    _write(source_file, json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")

    config = load_config(
        _write_config(
            tmp_path,
            source_home=source_home,
            target_home=target_home,
            clients=["codex"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""",
        )
    )

    first = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    import_machine_projection(config, "machine-a", first.bundle_dir, canonicalize_command=None)

    shutil.rmtree(first.bundle_dir)
    _write(source_file, json.dumps({"type": "event_msg", "payload": {"type": "token_count", "n": 2}}) + "\n")

    second = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    manifest = json.loads((second.bundle_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["mode"] == "projection_full"
    assert manifest["fallback_reason"] == "missing_base_snapshot"


def test_projection_delta_import_rejects_base_snapshot_mismatch(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    import_root = target_home / ".config" / "tokscale" / "imports" / "machine-a"

    source_file = source_home / ".codex" / "sessions" / "2026" / "04" / "08" / "one.jsonl"
    _write(source_file, json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")

    config = load_config(
        _write_config(
            tmp_path,
            source_home=source_home,
            target_home=target_home,
            clients=["codex"],
            root_blocks="""
[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""",
        )
    )

    first = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )
    import_machine_projection(config, "machine-a", first.bundle_dir, canonicalize_command=None)

    _write(source_file, json.dumps({"type": "event_msg", "payload": {"type": "token_count", "n": 2}}) + "\n")
    second = export_machine_projection(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
        machine_root=import_root,
    )

    (import_root / ".projection-state.json").write_text(
        json.dumps({"current_snapshot_id": "machine-a-override", "last_imported_at": "2026-04-08T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="base snapshot mismatch"):
        import_machine_projection(config, "machine-a", second.bundle_dir, canonicalize_command=None)

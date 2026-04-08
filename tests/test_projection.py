from __future__ import annotations

import json
from pathlib import Path

from agent_session_vault.config import load_config
from agent_session_vault.projection import export_machine_projection, import_machine_projection


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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

[machines.imac]
import_name = "imac"
ssh_target = "tokscale-sync-imac"
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

[machines.imac]
import_name = "imac"
ssh_target = "tokscale-sync-imac"
source_home = "{source_home}"
remote_relay_root = "{relay_root}"
remote_state_root = "{tmp_path / "remote-state"}"
clients = ["codex", "gemini"]

[[machines.imac.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"

[[machines.imac.roots]]
client = "gemini"
path = "~/.gemini"
kind = "home_root"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)

    bundle = export_machine_projection(
        machine=config.machines["imac"],
        source_home=source_home,
        relay_root=relay_root,
    )
    imported = import_machine_projection(config, "imac", bundle.bundle_dir, canonicalize_command=None)

    projected_codex = imports / "imac" / ".raw" / "codex" / "sessions"
    projected_files = sorted(projected_codex.rglob("*.jsonl"))
    assert projected_files
    projected_text = projected_files[0].read_text(encoding="utf-8")
    assert "token_count" in projected_text
    assert "drop-me" not in projected_text

    projected_gemini = imports / "imac" / ".raw" / "gemini"
    assert any(path.name == "chat.json" for path in projected_gemini.rglob("chat.json"))
    assert not any(path.name == "logs.json" for path in projected_gemini.rglob("logs.json"))

    manifest = json.loads((bundle.bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "projection_full"
    assert imported.snapshot_id == bundle.snapshot_id


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
[[machines.imac.roots]]
client = "gemini"
path = "~/.gemini"
kind = "home_root"
""",
        )
    )

    bundle = export_machine_projection(
        machine=config.machines["imac"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
    )
    import_machine_projection(config, "imac", bundle.bundle_dir, canonicalize_command=None)

    projected_gemini = target_home / ".config" / "tokscale" / "imports" / "imac" / ".raw" / "gemini"
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
[[machines.imac.roots]]
client = "openclaw"
path = "~/.openclaw"
kind = "home_root"
""",
        )
    )

    bundle = export_machine_projection(
        machine=config.machines["imac"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
    )
    import_machine_projection(config, "imac", bundle.bundle_dir, canonicalize_command=None)

    projected_openclaw = target_home / ".config" / "tokscale" / "imports" / "imac" / ".raw" / "openclaw"
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
[[machines.imac.roots]]
client = "openclaw"
path = "~/.openclaw"
kind = "home_root"
""",
        )
    )

    bundle = export_machine_projection(
        machine=config.machines["imac"],
        source_home=source_home,
        relay_root=tmp_path / "relay",
    )
    import_machine_projection(config, "imac", bundle.bundle_dir, canonicalize_command=None)

    projected_openclaw = target_home / ".config" / "tokscale" / "imports" / "imac" / ".raw" / "openclaw"
    projected_files = sorted(path.relative_to(projected_openclaw) for path in projected_openclaw.rglob("*") if path.is_file())
    normalized = [path for path in projected_files if path.name.endswith(".jsonl") and "_normalized" in path.parts]

    assert normalized
    assert normalized[0].name == "variant__jsonl__.guard-reset.2026-04-08T00-00-00Z.jsonl"

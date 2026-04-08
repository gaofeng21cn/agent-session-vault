from __future__ import annotations

from pathlib import Path
import json

from agent_session_vault.cli import main
from agent_session_vault.config import load_config
from agent_session_vault.projection import ProjectionBundle, export_machine_projection_ssh, import_machine_projection
from agent_session_vault.relay import (
    _remote_helper_source,
    export_machine_delta,
    export_machine_delta_ssh,
    import_machine_delta,
    inspect_machine_delta_ssh,
    pending_relay_bundle_dirs,
)
from agent_session_vault.syncing import (
    DeltaStats,
    ProjectionTransportDecision,
    choose_projection_transport,
    choose_sync_strategy,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_choose_sync_strategy_uses_thresholds_and_machine_override(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[paths]
home = "/tmp/home"
workspace_root = "/tmp/workspace"
import_root = "/tmp/imports"
shadow_home = "/tmp/shadow-home"
local_workspace_extras = "/tmp/extras"
archive_root = "/tmp/archive"
relay_root = "/tmp/relay"

[sync]
default_strategy = "auto"
direct_max_delta_files = 10
direct_max_delta_bytes = 100

[machines.imac]
import_name = "imac"
ssh_target = "tokscale-sync-imac"
source_home = "/remote/home"
remote_relay_root = "/remote/home/agent-session-vault/relay"
remote_state_root = "/remote/home/.config/agent-session-vault/relay-state"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)

    direct = choose_sync_strategy(
        config,
        "imac",
        DeltaStats(machine_name="imac", changed_files=2, changed_bytes=32, previous_snapshot_id=None, next_snapshot_id="imac-000001"),
    )
    relay = choose_sync_strategy(
        config,
        "imac",
        DeltaStats(machine_name="imac", changed_files=20, changed_bytes=1000, previous_snapshot_id=None, next_snapshot_id="imac-000002"),
    )

    assert direct.strategy == "direct"
    assert relay.strategy == "relay"


def test_choose_projection_transport_prefers_relay_for_large_bundle(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[paths]
home = "/tmp/home"
workspace_root = "/tmp/workspace"
import_root = "/tmp/imports"
shadow_home = "/tmp/shadow-home"
local_workspace_extras = "/tmp/extras"
archive_root = "/tmp/archive"
relay_root = "/tmp/relay"

[sync]
projection_transport = "auto"
projection_direct_max_bundle_bytes = 1024

[machines.imac]
import_name = "imac"
ssh_target = "tokscale-sync-imac"
source_home = "/remote/home"
remote_relay_root = "/remote/home/agent-session-vault/relay"
remote_state_root = "/remote/home/.config/agent-session-vault/relay-state"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)

    direct = choose_projection_transport(config, "imac", bundle_bytes=100)
    relay = choose_projection_transport(config, "imac", bundle_bytes=2048)

    assert isinstance(direct, ProjectionTransportDecision)
    assert direct.transport == "ssh"
    assert relay.transport == "relay"


def test_remote_relay_helper_can_inspect_export_and_import_with_local_python_runner(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    workspace = tmp_path / "workspace"
    imports = target_home / ".config" / "tokscale" / "imports"
    shadow_home = target_home / ".config" / "tokscale" / "shadow-home"
    extras = target_home / ".config" / "tokscale" / "local-workspace-extras"
    archive = tmp_path / "archive"
    relay_root = tmp_path / "relay-root"
    remote_state_root = tmp_path / "remote-state"

    _write(source_home / ".codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl", "a")
    _write(source_home / ".gemini" / "tmp" / "proj" / "chats" / "chat.json", "g")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{target_home}"
workspace_root = "{workspace}"
import_root = "{imports}"
shadow_home = "{shadow_home}"
local_workspace_extras = "{extras}"
archive_root = "{archive}"
relay_root = "{relay_root}"

[sync]
default_strategy = "auto"
direct_max_delta_files = 10
direct_max_delta_bytes = 100

[machines.imac]
import_name = "imac"
ssh_target = "tokscale-sync-imac"
source_home = "{source_home}"
remote_relay_root = "{relay_root}"
remote_state_root = "{remote_state_root}"
clients = ["codex", "gemini"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)

    stats = inspect_machine_delta_ssh(
        machine_name="imac",
        source_home=source_home,
        relay_root=relay_root,
        state_root=remote_state_root,
        command_prefix=["python3", "-"],
    )
    assert stats.changed_files == 2
    assert stats.changed_bytes == 2

    bundle = export_machine_delta_ssh(
        machine_name="imac",
        source_home=source_home,
        relay_root=relay_root,
        state_root=remote_state_root,
        command_prefix=["python3", "-"],
    )
    import_machine_delta(config=config, machine_name="imac", bundle_dir=bundle.bundle_dir)

    assert (imports / "imac" / ".raw" / "codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl").read_text(encoding="utf-8") == "a"
    assert (imports / "imac" / ".raw" / "gemini" / "proj" / "chats" / "chat.json").read_text(encoding="utf-8") == "g"

    post = inspect_machine_delta_ssh(
        machine_name="imac",
        source_home=source_home,
        relay_root=relay_root,
        state_root=remote_state_root,
        command_prefix=["python3", "-"],
    )
    assert post.changed_files == 0


def test_remote_relay_helper_source_stays_python39_compatible() -> None:
    helper = _remote_helper_source()
    assert "from datetime import datetime, timezone" in helper
    assert "datetime.now(timezone.utc)" in helper


def test_remote_projection_helper_exports_delta_after_existing_local_base(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    imports = target_home / ".config" / "tokscale" / "imports"
    shadow_home = target_home / ".config" / "tokscale" / "shadow-home"
    extras = target_home / ".config" / "tokscale" / "local-workspace-extras"
    archive = tmp_path / "archive"
    relay_root = tmp_path / "relay-root"
    remote_state_root = tmp_path / "remote-state"

    source_file = source_home / ".codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl"
    _write(source_file, '{"type":"event_msg","payload":{"type":"token_count"}}\n')

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{target_home}"
workspace_root = "{tmp_path / "workspace"}"
import_root = "{imports}"
shadow_home = "{shadow_home}"
local_workspace_extras = "{extras}"
archive_root = "{archive}"
relay_root = "{relay_root}"

[sync]
projection_transport = "auto"
projection_direct_max_bundle_bytes = 1073741824

[machines.imac]
import_name = "imac"
ssh_target = "tokscale-sync-imac"
source_home = "{source_home}"
remote_relay_root = "{relay_root}"
remote_state_root = "{remote_state_root}"
clients = ["codex"]

[[machines.imac.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    machine_root = imports / "imac"

    first = export_machine_projection_ssh(
        machine=config.machines["imac"],
        source_home=source_home,
        relay_root=relay_root,
        ssh_target="",
        command_prefix=["python3", "-"],
        base_snapshot_id=None,
    )
    assert first.mode == "projection_full"
    import_machine_projection(config, "imac", first.bundle_dir, canonicalize_command=None)

    _write(source_file, '{"type":"event_msg","payload":{"type":"token_count","n":2}}\n')

    second = export_machine_projection_ssh(
        machine=config.machines["imac"],
        source_home=source_home,
        relay_root=relay_root,
        ssh_target="",
        command_prefix=["python3", "-"],
        base_snapshot_id=json.loads((machine_root / ".projection-state.json").read_text(encoding="utf-8"))["current_snapshot_id"],
    )

    assert second.mode == "projection_delta"
    assert second.base_snapshot_id == first.snapshot_id


def test_sync_auto_json_includes_projection_mode_metadata(tmp_path: Path, monkeypatch, capsys) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    imports = target_home / ".config" / "tokscale" / "imports"
    shadow_home = target_home / ".config" / "tokscale" / "shadow-home"
    extras = target_home / ".config" / "tokscale" / "local-workspace-extras"
    archive = tmp_path / "archive"
    relay_root = tmp_path / "relay-root"
    remote_state_root = tmp_path / "remote-state"
    bundle_dir = relay_root / "projection" / "imac" / "imac-20260408T000000000000Z"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = bundle_dir / "manifest.json"
    bundle_path = bundle_dir / "payload.tar.zst"
    roots_manifest_path = bundle_dir / "roots-manifest.json"
    inventory_path = bundle_dir / "inventory.json"
    for path in (manifest_path, bundle_path, roots_manifest_path, inventory_path):
        path.write_text("{}\n", encoding="utf-8")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{target_home}"
workspace_root = "{tmp_path / "workspace"}"
import_root = "{imports}"
shadow_home = "{shadow_home}"
local_workspace_extras = "{extras}"
archive_root = "{archive}"
relay_root = "{relay_root}"

[sync]
projection_transport = "relay"
projection_direct_max_bundle_bytes = 1

[machines.imac]
import_name = "imac"
ssh_target = "tokscale-sync-imac"
source_home = "{source_home}"
remote_relay_root = "{relay_root}"
remote_state_root = "{remote_state_root}"
clients = ["codex"]

[[machines.imac.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    def _fake_export_machine_projection_ssh(*args, **kwargs) -> ProjectionBundle:
        return ProjectionBundle(
            machine_name="imac",
            snapshot_id="imac-20260408T000000000000Z",
            bundle_dir=bundle_dir,
            manifest_path=manifest_path,
            bundle_path=bundle_path,
            roots_manifest_path=roots_manifest_path,
            inventory_path=inventory_path,
            bundle_bytes=2048,
            mode="projection_full",
            base_snapshot_id=None,
            fallback_reason="missing_local_state",
        )

    monkeypatch.setattr("agent_session_vault.cli.export_machine_projection_ssh", _fake_export_machine_projection_ssh)

    exit_code = main(["--config", str(config_path), "sync", "auto", "imac", "--json", "--dry-run"])
    assert exit_code == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["bundle"]["mode"] == "projection_full"
    assert payload["bundle"]["fallback_reason"] == "missing_local_state"


def test_pending_relay_bundle_dirs_returns_contiguous_unimported_chain(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    workspace = tmp_path / "workspace"
    imports = target_home / ".config" / "tokscale" / "imports"
    shadow_home = target_home / ".config" / "tokscale" / "shadow-home"
    extras = target_home / ".config" / "tokscale" / "local-workspace-extras"
    archive = tmp_path / "archive"
    relay_root = tmp_path / "relay-root"
    remote_state_root = tmp_path / "remote-state"

    _write(source_home / ".codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl", "a")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{target_home}"
workspace_root = "{workspace}"
import_root = "{imports}"
shadow_home = "{shadow_home}"
local_workspace_extras = "{extras}"
archive_root = "{archive}"
relay_root = "{relay_root}"

[machines.imac]
import_name = "imac"
ssh_target = "tokscale-sync-imac"
source_home = "{source_home}"
remote_relay_root = "{relay_root}"
remote_state_root = "{remote_state_root}"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)

    first = export_machine_delta("imac", source_home, relay_root, remote_state_root)
    _write(source_home / ".codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl", "aa")
    second = export_machine_delta("imac", source_home, relay_root, remote_state_root)

    pending = pending_relay_bundle_dirs(config, "imac")
    assert pending == [first.bundle_dir, second.bundle_dir]

    import_machine_delta(config=config, machine_name="imac", bundle_dir=first.bundle_dir)
    pending_after_first = pending_relay_bundle_dirs(config, "imac")
    assert pending_after_first == [second.bundle_dir]

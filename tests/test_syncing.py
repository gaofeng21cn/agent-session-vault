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

[machines.machine-a]
import_name = "machine-a"
ssh_target = "session-sync-a"
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
        "machine-a",
        DeltaStats(machine_name="machine-a", changed_files=2, changed_bytes=32, previous_snapshot_id=None, next_snapshot_id="machine-a-000001"),
    )
    relay = choose_sync_strategy(
        config,
        "machine-a",
        DeltaStats(machine_name="machine-a", changed_files=20, changed_bytes=1000, previous_snapshot_id=None, next_snapshot_id="machine-a-000002"),
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

[machines.machine-a]
import_name = "machine-a"
ssh_target = "session-sync-a"
source_home = "/remote/home"
remote_relay_root = "/remote/home/agent-session-vault/relay"
remote_state_root = "/remote/home/.config/agent-session-vault/relay-state"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)

    direct = choose_projection_transport(config, "machine-a", bundle_bytes=100)
    relay = choose_projection_transport(config, "machine-a", bundle_bytes=2048)

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

[machines.machine-a]
import_name = "machine-a"
ssh_target = "session-sync-a"
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
        machine_name="machine-a",
        source_home=source_home,
        relay_root=relay_root,
        state_root=remote_state_root,
        command_prefix=["python3", "-"],
    )
    assert stats.changed_files == 2
    assert stats.changed_bytes == 2

    bundle = export_machine_delta_ssh(
        machine_name="machine-a",
        source_home=source_home,
        relay_root=relay_root,
        state_root=remote_state_root,
        command_prefix=["python3", "-"],
    )
    import_machine_delta(config=config, machine_name="machine-a", bundle_dir=bundle.bundle_dir)

    assert (imports / "machine-a" / ".raw" / "codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl").read_text(encoding="utf-8") == "a"
    assert (imports / "machine-a" / ".raw" / "gemini" / "proj" / "chats" / "chat.json").read_text(encoding="utf-8") == "g"

    post = inspect_machine_delta_ssh(
        machine_name="machine-a",
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

[machines.machine-a]
import_name = "machine-a"
ssh_target = "session-sync-a"
source_home = "{source_home}"
remote_relay_root = "{relay_root}"
remote_state_root = "{remote_state_root}"
clients = ["codex"]

[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    machine_root = imports / "machine-a"

    first = export_machine_projection_ssh(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=relay_root,
        ssh_target="",
        command_prefix=["python3", "-"],
        base_snapshot_id=None,
    )
    assert first.mode == "projection_full"
    assert first.state_status == "rebuilt"
    assert first.files_seen == 1
    assert first.files_projected == 1
    assert first.files_reused == 0
    imported_first = import_machine_projection(config, "machine-a", first.bundle_dir, canonicalize_command=None)
    assert imported_first.state_status == "rebuilt"
    assert imported_first.files_projected == 1

    _write(source_file, '{"type":"event_msg","payload":{"type":"token_count","n":2}}\n')

    second = export_machine_projection_ssh(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=relay_root,
        ssh_target="",
        command_prefix=["python3", "-"],
        base_snapshot_id=json.loads((machine_root / ".projection-state.json").read_text(encoding="utf-8"))["current_snapshot_id"],
    )

    assert second.mode == "projection_delta"
    assert second.base_snapshot_id == first.snapshot_id
    assert second.state_status == "incremental"
    assert second.files_seen == 1
    assert second.files_projected == 1
    assert second.files_reused == 0

    third = export_machine_projection_ssh(
        machine=config.machines["machine-a"],
        source_home=source_home,
        relay_root=relay_root,
        ssh_target="",
        command_prefix=["python3", "-"],
        base_snapshot_id=first.snapshot_id,
    )
    third_manifest = json.loads(third.manifest_path.read_text(encoding="utf-8"))
    state_path = remote_state_root / "machine-a" / "projection" / "state.json"

    assert third.mode == "projection_delta"
    assert third.state_status == "incremental"
    assert third.files_projected == 0
    assert third.files_reused == 1
    assert third_manifest["projection_state"]["files_projected"] == 0
    assert state_path.is_file()


def test_sync_auto_json_includes_projection_mode_metadata(tmp_path: Path, monkeypatch, capsys) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    imports = target_home / ".config" / "tokscale" / "imports"
    shadow_home = target_home / ".config" / "tokscale" / "shadow-home"
    extras = target_home / ".config" / "tokscale" / "local-workspace-extras"
    archive = tmp_path / "archive"
    relay_root = tmp_path / "relay-root"
    remote_state_root = tmp_path / "remote-state"
    bundle_dir = relay_root / "projection" / "machine-a" / "machine-a-20260408T000000000000Z"
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

[machines.machine-a]
import_name = "machine-a"
ssh_target = "session-sync-a"
source_home = "{source_home}"
remote_relay_root = "{relay_root}"
remote_state_root = "{remote_state_root}"
clients = ["codex"]

[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    def _fake_export_machine_projection_ssh(*args, **kwargs) -> ProjectionBundle:
        return ProjectionBundle(
            machine_name="machine-a",
            snapshot_id="machine-a-20260408T000000000000Z",
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

    exit_code = main(["--config", str(config_path), "sync", "auto", "machine-a", "--json", "--dry-run"])
    assert exit_code == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["bundle"]["mode"] == "projection_full"
    assert payload["bundle"]["fallback_reason"] == "missing_local_state"


def test_projection_fetch_ssh_fetches_remote_bundle_to_expected_local_dir(tmp_path: Path, monkeypatch, capsys) -> None:
    target_home = tmp_path / "target-home"
    relay_root = tmp_path / "relay-root"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{target_home}"
workspace_root = "{tmp_path / "workspace"}"
import_root = "{target_home / ".config" / "tokscale" / "imports"}"
shadow_home = "{target_home / ".config" / "tokscale" / "shadow-home"}"
local_workspace_extras = "{target_home / ".config" / "tokscale" / "local-workspace-extras"}"
archive_root = "{tmp_path / "archive"}"
relay_root = "{relay_root}"

[machines.machine-a]
import_name = "machine-a"
ssh_target = "session-sync-a"
source_home = "/remote/home"
remote_relay_root = "/remote/relay"
remote_state_root = "/remote/state"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    remote_bundle_dir = Path("/remote/relay/projection/machine-a/machine-a-20260707T000000000000Z")
    expected_bundle_dir = relay_root / "projection" / "machine-a" / "machine-a-20260707T000000000000Z"
    calls: list[tuple[str, Path, Path]] = []

    def _fake_fetch_projection_bundle_ssh(
        *,
        ssh_target: str,
        remote_bundle_dir: Path,
        local_bundle_dir: Path,
    ) -> Path:
        calls.append((ssh_target, remote_bundle_dir, local_bundle_dir))
        return local_bundle_dir

    monkeypatch.setattr("agent_session_vault.cli.fetch_projection_bundle_ssh", _fake_fetch_projection_bundle_ssh)

    exit_code = main(
        [
            "--config",
            str(config_path),
            "sync",
            "projection-fetch-ssh",
            "machine-a",
            "--remote-bundle-dir",
            str(remote_bundle_dir),
            "--json",
        ]
    )

    assert exit_code == 0
    assert calls == [("session-sync-a", remote_bundle_dir, expected_bundle_dir)]
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "machine": "machine-a",
        "remote_bundle_dir": str(remote_bundle_dir),
        "bundle_dir": str(expected_bundle_dir),
    }


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

[machines.machine-a]
import_name = "machine-a"
ssh_target = "session-sync-a"
source_home = "{source_home}"
remote_relay_root = "{relay_root}"
remote_state_root = "{remote_state_root}"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)

    first = export_machine_delta("machine-a", source_home, relay_root, remote_state_root)
    _write(source_home / ".codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl", "aa")
    second = export_machine_delta("machine-a", source_home, relay_root, remote_state_root)

    pending = pending_relay_bundle_dirs(config, "machine-a")
    assert pending == [first.bundle_dir, second.bundle_dir]

    import_machine_delta(config=config, machine_name="machine-a", bundle_dir=first.bundle_dir)
    pending_after_first = pending_relay_bundle_dirs(config, "machine-a")
    assert pending_after_first == [second.bundle_dir]

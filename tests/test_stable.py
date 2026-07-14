from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from agent_session_vault.config import load_config
from agent_session_vault.stable import (
    default_stable_root,
    migration_plan_payload,
    mirror_stable_layer,
    stable_mirror_payload,
)


def _write_config(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    imports = tmp_path / "imports"
    extras = tmp_path / "local-workspace-extras"
    archive = tmp_path / "OneDrive" / "agent-session-vault" / "archive"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{tmp_path / 'workspace'}"
import_root = "{imports}"
shadow_home = "{tmp_path / 'shadow-home'}"
local_workspace_extras = "{extras}"
archive_root = "{archive}"

[machines.machine-a]
import_name = "machine-a"
ssh_target = "session-sync-a"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (imports / "machine-a" / ".raw" / "codex").mkdir(parents=True)
    (imports / "machine-a" / ".raw" / "codex" / "one.jsonl").write_text("codex", encoding="utf-8")
    (extras / "volatile-codex-homes" / "codex").mkdir(parents=True)
    (extras / "volatile-codex-homes" / "codex" / "two.jsonl").write_text("extra", encoding="utf-8")
    return config_path


def test_default_stable_root_uses_archive_parent(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))

    assert default_stable_root(config) == tmp_path / "OneDrive" / "agent-session-vault" / "stable"


def test_mirror_stable_layer_plans_imports_extras_and_config(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))

    result = mirror_stable_layer(config, dry_run=True)
    payload = stable_mirror_payload(result)

    assert payload["dry_run"] is True
    destinations = {item["label"]: item["destination"] for item in payload["items"]}
    assert destinations["imports"].endswith("/stable/tokscale/imports")
    assert destinations["local_workspace_extras"].endswith("/stable/tokscale/local-workspace-extras")
    assert destinations["config"].endswith("/stable/config/config.toml")
    assert {item["status"] for item in payload["items"]} == {"planned"}


def test_mirror_stable_layer_runs_rsync_for_directories_and_copies_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(_write_config(tmp_path))
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        source = Path(command[-2].removesuffix("/"))
        destination = Path(command[-1].removesuffix("/"))
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("agent_session_vault.stable.subprocess.run", fake_run)

    result = mirror_stable_layer(config)
    payload = stable_mirror_payload(result)

    assert [command[:2] for command in commands] == [["rsync", "-a"], ["rsync", "-a"]]
    assert (default_stable_root(config) / "config" / "config.toml").read_text(encoding="utf-8") == config.config_path.read_text(
        encoding="utf-8"
    )
    assert payload["manifest_path"].endswith("/stable/stable-layer-manifest.json")
    assert {item["status"] for item in payload["items"]} == {"mirrored"}
    assert {item["coverage_status"] for item in payload["items"]} == {"verified"}
    assert payload["status"] == "verified"


def test_migration_profile_includes_live_sessions_and_reports_readiness(tmp_path: Path, monkeypatch) -> None:
    config = load_config(_write_config(tmp_path))
    sessions = config.paths.home / ".codex" / "sessions" / "2026" / "07" / "14"
    archived = config.paths.home / ".codex" / "archived_sessions"
    sessions.mkdir(parents=True)
    archived.mkdir(parents=True)
    (sessions / "live.jsonl").write_text("live\n", encoding="utf-8")
    (archived / "old.jsonl").write_text("old\n", encoding="utf-8")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        source = Path(command[-2].removesuffix("/"))
        destination = Path(command[-1].removesuffix("/"))
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("agent_session_vault.stable.subprocess.run", fake_run)

    before = migration_plan_payload(config)
    assert before["readiness"]["full_fidelity_restore_ready"] is False
    assert "live_sessions_not_verified" not in before["readiness"]["blockers"]
    assert "live_sessions_not_verified" in before["readiness"]["optional_migration_blockers"]

    result = mirror_stable_layer(config, include_live_sessions=True)
    after = migration_plan_payload(config)

    assert result.profile == "migration"
    assert result.status == "verified"
    assert after["readiness"]["analytics_restore_ready"] is True
    assert after["readiness"]["full_fidelity_restore_ready"] is True
    labels = {item["label"] for item in after["items"] if item["source_exists"]}
    assert "live_codex_sessions" in labels
    assert "live_codex_archived_sessions" in labels

    (sessions / "live.jsonl").write_text("live changed after mirror\n", encoding="utf-8")
    stale = migration_plan_payload(config)
    assert stale["readiness"]["analytics_restore_ready"] is True
    assert stale["readiness"]["full_fidelity_restore_ready"] is False


def test_stable_mirror_stages_changed_destination_before_copy(tmp_path: Path, monkeypatch) -> None:
    config = load_config(_write_config(tmp_path))

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        source = Path(command[-2].removesuffix("/"))
        destination = Path(command[-1].removesuffix("/"))
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("agent_session_vault.stable.subprocess.run", fake_run)
    first = mirror_stable_layer(config)
    source_file = config.paths.import_root / "machine-a" / ".raw" / "codex" / "one.jsonl"
    destination_file = default_stable_root(config) / "tokscale" / "imports" / "machine-a" / ".raw" / "codex" / "one.jsonl"
    source_file.write_text("updated-content", encoding="utf-8")

    second = mirror_stable_layer(config)

    assert first.status == "verified"
    assert second.status == "verified"
    assert destination_file.read_text(encoding="utf-8") == "updated-content"
    assert not any((default_stable_root(config) / ".asv-replaced").rglob("*"))


def test_stable_mirror_reuses_verified_manifest_without_rsync(tmp_path: Path, monkeypatch) -> None:
    config = load_config(_write_config(tmp_path))
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        source = Path(command[-2].removesuffix("/"))
        destination = Path(command[-1].removesuffix("/"))
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("agent_session_vault.stable.subprocess.run", fake_run)

    first = mirror_stable_layer(config)
    first_command_count = len(commands)
    second = mirror_stable_layer(config)

    assert first.status == "verified"
    assert second.status == "verified"
    assert len(commands) == first_command_count
    assert {item.transfer_status for item in second.items} == {"reused_verified"}


def test_stable_mirror_repairs_missing_destination_after_manifest_reuse(tmp_path: Path, monkeypatch) -> None:
    config = load_config(_write_config(tmp_path))
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        source = Path(command[-2].removesuffix("/"))
        destination = Path(command[-1].removesuffix("/"))
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("agent_session_vault.stable.subprocess.run", fake_run)
    first = mirror_stable_layer(config)
    missing_destination = (
        default_stable_root(config) / "tokscale" / "imports" / "machine-a" / ".raw" / "codex" / "one.jsonl"
    )
    missing_destination.unlink()

    second = mirror_stable_layer(config)

    assert first.status == "verified"
    assert second.status == "verified"
    assert missing_destination.read_text(encoding="utf-8") == "codex"
    assert len(commands) == 3
    assert next(item for item in second.items if item.label == "imports").transfer_status == "transferred"

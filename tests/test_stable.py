from __future__ import annotations

from pathlib import Path
import subprocess

from agent_session_vault.config import load_config
from agent_session_vault.stable import default_stable_root, mirror_stable_layer, stable_mirror_payload


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

[machines.imac]
import_name = "imac"
ssh_target = "imac-sync"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (imports / "imac" / ".raw" / "codex").mkdir(parents=True)
    (imports / "imac" / ".raw" / "codex" / "one.jsonl").write_text("codex", encoding="utf-8")
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

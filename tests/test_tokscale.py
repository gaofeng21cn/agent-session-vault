from pathlib import Path

from agent_session_vault.cli import main
from agent_session_vault.config import load_config
from agent_session_vault.tokscale import (
    DEFAULT_TOKSCALE_PACKAGE,
    TOKSCALE_PACKAGE_ENV,
    build_tokscale_invocation,
)


def test_build_tokscale_invocation_for_raw_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    projection_home = tmp_path / "projection-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    (home / ".codex" / "sessions").mkdir(parents=True)
    (imports / "machine-a" / ".raw" / "codex").mkdir(parents=True)
    (imports / "local-home" / ".raw" / "codex").mkdir(parents=True)
    projection_home.mkdir()
    (workspace / "proj-a" / ".codex").mkdir(parents=True)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{workspace}"
import_root = "{imports}"
projection_home = "{projection_home}"
shadow_home = "{shadow_home}"
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

    config = load_config(config_path)
    invocation = build_tokscale_invocation(config, mode="raw", args=["submit", "--dry-run"])

    assert invocation.env["HOME"] == str(projection_home)
    assert "imports/local-home/.raw/codex" in invocation.env["TOKSCALE_EXTRA_DIRS"]
    assert "imports/machine-a/.raw/codex" in invocation.env["TOKSCALE_EXTRA_DIRS"]
    assert invocation.command[:3] == ["npx", "-y", DEFAULT_TOKSCALE_PACKAGE]
    assert invocation.command[-2:] == ["submit", "--dry-run"]


def test_build_tokscale_invocation_for_canonical_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    (imports / "machine-a" / "codex").mkdir(parents=True)
    (extras / "proj-a" / "codex").mkdir(parents=True)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{workspace}"
import_root = "{imports}"
shadow_home = "{shadow_home}"
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

    config = load_config(config_path)
    invocation = build_tokscale_invocation(
        config,
        mode="canonical",
        omx_replay_dedupe="strict",
        args=["submit", "--dry-run"],
    )

    assert invocation.env["HOME"] == str(shadow_home)
    assert "imports/machine-a/codex" in invocation.env["TOKSCALE_EXTRA_DIRS"]
    assert str(extras / "proj-a" / "codex") in invocation.env["TOKSCALE_EXTRA_DIRS"]


def test_build_tokscale_invocation_strips_codex_home_override(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    (home / ".codex" / "sessions").mkdir(parents=True)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{workspace}"
import_root = "{imports}"
shadow_home = "{shadow_home}"
local_workspace_extras = "{extras}"
archive_root = "{archive}"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("CODEX_HOME", "/tmp/paperclip-codex-home")

    config = load_config(config_path)
    invocation = build_tokscale_invocation(config, mode="raw", args=["submit", "--dry-run"])

    assert invocation.env["HOME"] == str(config.paths.projection_home)
    assert invocation.env["HOME"] != str(home)
    assert "CODEX_HOME" not in invocation.env


def test_build_tokscale_invocation_allows_package_override(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{workspace}"
import_root = "{imports}"
shadow_home = "{shadow_home}"
local_workspace_extras = "{extras}"
archive_root = "{archive}"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv(TOKSCALE_PACKAGE_ENV, "tokscale@3.1.2")

    config = load_config(config_path)
    invocation = build_tokscale_invocation(config, mode="raw", args=["submit", "--dry-run"])

    assert invocation.command[:3] == ["npx", "-y", "tokscale@3.1.2"]


def test_build_tokscale_invocation_accepts_explicit_package_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{tmp_path / 'workspace'}"
import_root = "{tmp_path / 'imports'}"
shadow_home = "{tmp_path / 'shadow-home'}"
local_workspace_extras = "{tmp_path / 'extras'}"
archive_root = "{tmp_path / 'archive'}"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    invocation = build_tokscale_invocation(
        config,
        mode="raw",
        args=["submit", "--help"],
        package_override="tokscale@4.5.2",
    )

    assert invocation.command[:3] == ["npx", "-y", "tokscale@4.5.2"]
    assert invocation.env[TOKSCALE_PACKAGE_ENV] == "tokscale@4.5.2"


def test_cli_raw_exec_refreshes_local_home_projection_first(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    home = tmp_path / "home"
    home.mkdir()
    config_path.write_text(f'[paths]\nhome = "{home}"\n', encoding="utf-8")
    refreshed: list[Path] = []
    commands: list[list[str]] = []

    def fake_refresh(config, **kwargs):
        refreshed.append(config.paths.home)
        return None

    def fake_run(command, env=None, dry_run=False):
        commands.append(command)
        return 0

    monkeypatch.setattr("agent_session_vault.cli.refresh_local_home_projection", fake_refresh)
    monkeypatch.setattr("agent_session_vault.cli._run_subprocess", fake_run)

    exit_code = main(
        [
            "--config",
            str(config_path),
            "tokscale",
            "exec",
            "--mode",
            "raw",
            "--",
            "submit",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert refreshed == [home]
    assert commands and commands[0][-2:] == ["submit", "--dry-run"]

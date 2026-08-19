from pathlib import Path

from agent_session_vault.cli import main
from agent_session_vault.config import load_config
from agent_session_vault.tokscale import (
    DEFAULT_TOKSCALE_PACKAGE,
    TOKSCALE_PACKAGE_ENV,
    build_tokscale_invocation,
)


def _config(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    imports = tmp_path / "imports"
    projection_home = tmp_path / "projection-home"
    (imports / "local-home" / ".raw" / "codex").mkdir(parents=True)
    (imports / "local-home" / ".raw" / "zcode").mkdir(parents=True)
    (imports / "fleet-node" / ".raw" / "gemini").mkdir(parents=True)
    (imports / "fleet-node" / ".raw" / "antigravity").mkdir(parents=True)
    projection_home.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
import_root = "{imports}"
projection_home = "{projection_home}"
local_workspace_extras = "{tmp_path / 'extras'}"
stable_root = "{tmp_path / 'stable'}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


def test_build_tokscale_invocation_uses_projection_only_view(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path))
    invocation = build_tokscale_invocation(config, args=["submit", "--dry-run"])

    assert invocation.env["HOME"] == str(config.paths.projection_home)
    assert invocation.env["HOME"] != str(config.paths.home)
    assert "imports/local-home/.raw/codex" in invocation.env["TOKSCALE_EXTRA_DIRS"]
    assert "imports/fleet-node/.raw/gemini" in invocation.env["TOKSCALE_EXTRA_DIRS"]
    assert "imports/local-home/.raw/zcode" in invocation.env["TOKSCALE_EXTRA_DIRS"]
    assert "imports/fleet-node/.raw/antigravity" in invocation.env["TOKSCALE_EXTRA_DIRS"]
    assert invocation.command[:3] == ["npx", "-y", DEFAULT_TOKSCALE_PACKAGE]
    assert invocation.command[-2:] == ["submit", "--dry-run"]


def test_build_tokscale_invocation_strips_codex_home_and_accepts_package_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", "/tmp/runtime-codex-home")
    monkeypatch.setenv(TOKSCALE_PACKAGE_ENV, "tokscale@4.5.2")
    config = load_config(_config(tmp_path))

    invocation = build_tokscale_invocation(config, args=["submit", "--help"])

    assert "CODEX_HOME" not in invocation.env
    assert invocation.command[:3] == ["npx", "-y", "tokscale@4.5.2"]

    explicit = build_tokscale_invocation(
        config,
        args=["submit", "--help"],
        package_override="tokscale@4.6.0",
    )
    assert explicit.command[:3] == ["npx", "-y", "tokscale@4.6.0"]
    assert explicit.env[TOKSCALE_PACKAGE_ENV] == "tokscale@4.6.0"


def test_cli_exec_refreshes_local_projection_before_running(tmp_path: Path, monkeypatch) -> None:
    config_path = _config(tmp_path)
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
            "--",
            "submit",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert refreshed == [load_config(config_path).paths.home]
    assert commands and commands[0][-2:] == ["submit", "--dry-run"]

from pathlib import Path

from agent_session_vault.config import load_config
from agent_session_vault.tokscale import build_tokscale_invocation


def test_build_tokscale_invocation_for_raw_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    (home / ".codex" / "sessions").mkdir(parents=True)
    (imports / "imac" / ".raw" / "codex").mkdir(parents=True)
    (workspace / "proj-a" / ".codex").mkdir(parents=True)

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

[machines.imac]
import_name = "imac"
ssh_target = "imac-sync"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    invocation = build_tokscale_invocation(config, mode="raw", args=["submit", "--dry-run"])

    assert invocation.env["HOME"] == str(home)
    assert "imports/imac/.raw/codex" in invocation.env["TOKSCALE_EXTRA_DIRS"]
    assert invocation.command[-2:] == ["submit", "--dry-run"]


def test_build_tokscale_invocation_for_canonical_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    (imports / "imac" / "codex").mkdir(parents=True)
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

[machines.imac]
import_name = "imac"
ssh_target = "imac-sync"
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
    assert "imports/imac/codex" in invocation.env["TOKSCALE_EXTRA_DIRS"]
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

    assert invocation.env["HOME"] == str(home)
    assert "CODEX_HOME" not in invocation.env

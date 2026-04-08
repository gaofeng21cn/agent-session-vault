from pathlib import Path

from agent_session_vault.adapters import build_canonicalize_machine_command, build_direct_sync_command
from agent_session_vault.config import load_config


def test_build_direct_sync_command_uses_configured_machine(tmp_path: Path) -> None:
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

[machines.imac]
import_name = "imac"
ssh_target = "imac-sync"
clients = ["codex", "gemini", "openclaw"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    command = build_direct_sync_command(config, "imac")

    assert command == ["tokscale-pull-remote", "imac", "imac-sync"]


def test_build_canonicalize_machine_command_uses_import_name(tmp_path: Path) -> None:
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

[machines.imac]
import_name = "imac"
ssh_target = "imac-sync"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    command = build_canonicalize_machine_command(config, "imac")

    assert command == ["tokscale-canonicalize-import-machine", "imac"]

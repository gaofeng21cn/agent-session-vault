from pathlib import Path

from agent_session_vault.config import load_config
from agent_session_vault.views import build_view


def test_load_config_reads_machine_definitions(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[paths]
home = "/tmp/home"
workspace_root = "/tmp/workspace"
import_root = "/tmp/imports"
shadow_home = "/tmp/shadow-home"
local_workspace_extras = "/tmp/local-workspace-extras"
archive_root = "/tmp/archive"
relay_root = "/tmp/relay"

[sync]
default_strategy = "auto"
direct_max_delta_files = 123
direct_max_delta_bytes = 456789
projection_transport = "auto"
projection_direct_max_bundle_bytes = 1073741824

[machines.imac]
import_name = "imac"
ssh_target = "imac-sync"
source_home = "/remote/home"
remote_relay_root = "/remote/home/agent-session-vault/relay"
remote_state_root = "/remote/home/.config/agent-session-vault/relay-state"
sync_strategy = "relay"
direct_max_delta_files = 22
direct_max_delta_bytes = 333
clients = ["codex", "gemini", "openclaw"]

[[machines.imac.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"

[[machines.imac.root_globs]]
client = "codex"
glob = "~/projects/*/.codex"
kind = "project_root"

[[retention.rules]]
name = "imac-codex-raw"
layer = "imports_raw"
machine = "imac"
client = "codex"
max_age_days = 7
min_size_bytes = 1024
archive_subdir = "imports-raw"
remove_source = true
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.paths.home == Path("/tmp/home")
    assert config.paths.relay_root == Path("/tmp/relay")
    assert config.sync.default_strategy == "auto"
    assert config.sync.direct_max_delta_files == 123
    assert config.sync.direct_max_delta_bytes == 456789
    assert config.sync.projection_transport == "auto"
    assert config.sync.projection_direct_max_bundle_bytes == 1073741824
    assert config.machines["imac"].ssh_target == "imac-sync"
    assert config.machines["imac"].source_home == Path("/remote/home")
    assert config.machines["imac"].remote_relay_root == Path("/remote/home/agent-session-vault/relay")
    assert config.machines["imac"].remote_state_root == Path("/remote/home/.config/agent-session-vault/relay-state")
    assert config.machines["imac"].sync_strategy == "relay"
    assert config.machines["imac"].direct_max_delta_files == 22
    assert config.machines["imac"].direct_max_delta_bytes == 333
    assert config.machines["imac"].clients == ("codex", "gemini", "openclaw")
    assert len(config.machines["imac"].roots) == 1
    assert config.machines["imac"].roots[0].client == "codex"
    assert config.machines["imac"].roots[0].path == "~/.codex"
    assert len(config.machines["imac"].root_globs) == 1
    assert config.machines["imac"].root_globs[0].glob == "~/projects/*/.codex"
    assert len(config.retention_rules) == 1
    assert config.retention_rules[0].name == "imac-codex-raw"
    assert config.retention_rules[0].remove_source is True


def test_build_raw_view_uses_live_home_and_raw_imports(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".gemini" / "tmp").mkdir(parents=True)
    (imports / "imac" / ".raw" / "codex").mkdir(parents=True)
    (imports / "imac" / ".raw" / "gemini").mkdir(parents=True)
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
clients = ["codex", "gemini"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    view = build_view(config, mode="raw")

    assert view.home == home
    assert ("codex", imports / "imac" / ".raw" / "codex") in view.extra_dirs
    assert ("gemini", imports / "imac" / ".raw" / "gemini") in view.extra_dirs
    assert ("codex", workspace / "proj-a" / ".codex") in view.extra_dirs


def test_build_raw_view_includes_home_project_codex_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".codex" / "projects" / "proj-b" / "archive" / "20260411T000000Z" / "codex" / "sessions").mkdir(
        parents=True
    )

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
    view = build_view(config, mode="raw")

    assert ("codex", home / ".codex" / "projects" / "proj-b") in view.extra_dirs


def test_build_canonical_view_uses_shadow_home_and_extras(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    (imports / "imac" / "codex").mkdir(parents=True)
    (imports / "imac" / "openclaw").mkdir(parents=True)
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
clients = ["codex", "openclaw"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    view = build_view(config, mode="canonical", omx_replay_dedupe="strict")

    assert view.home == shadow_home
    assert ("codex", imports / "imac" / "codex") in view.extra_dirs
    assert ("openclaw", imports / "imac" / "openclaw") in view.extra_dirs
    assert ("codex", extras / "proj-a" / "codex") in view.extra_dirs

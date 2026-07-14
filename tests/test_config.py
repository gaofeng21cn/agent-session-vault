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
projection_home = "/tmp/projection-home"
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

[machines.machine-a]
import_name = "machine-a"
ssh_target = "session-sync-a"
source_home = "/remote/home"
remote_relay_root = "/remote/home/agent-session-vault/relay"
remote_state_root = "/remote/home/.config/agent-session-vault/relay-state"
sync_strategy = "relay"
direct_max_delta_files = 22
direct_max_delta_bytes = 333
clients = ["codex", "gemini", "openclaw"]

[[machines.machine-a.roots]]
client = "codex"
path = "~/.codex"
kind = "home_root"

[[machines.machine-a.root_globs]]
client = "codex"
glob = "~/projects/*/.codex"
kind = "project_root"

[[retention.rules]]
name = "machine-a-codex-raw"
layer = "imports_raw"
machine = "machine-a"
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
    assert config.paths.projection_home == Path("/tmp/projection-home")
    assert config.paths.relay_root == Path("/tmp/relay")
    assert config.sync.default_strategy == "auto"
    assert config.sync.direct_max_delta_files == 123
    assert config.sync.direct_max_delta_bytes == 456789
    assert config.sync.projection_transport == "auto"
    assert config.sync.projection_direct_max_bundle_bytes == 1073741824
    assert config.machines["machine-a"].ssh_target == "session-sync-a"
    assert config.machines["machine-a"].source_home == Path("/remote/home")
    assert config.machines["machine-a"].remote_relay_root == Path("/remote/home/agent-session-vault/relay")
    assert config.machines["machine-a"].remote_state_root == Path("/remote/home/.config/agent-session-vault/relay-state")
    assert config.machines["machine-a"].sync_strategy == "relay"
    assert config.machines["machine-a"].direct_max_delta_files == 22
    assert config.machines["machine-a"].direct_max_delta_bytes == 333
    assert config.machines["machine-a"].clients == ("codex", "gemini", "openclaw")
    assert len(config.machines["machine-a"].roots) == 1
    assert config.machines["machine-a"].roots[0].client == "codex"
    assert config.machines["machine-a"].roots[0].path == "~/.codex"
    assert len(config.machines["machine-a"].root_globs) == 1
    assert config.machines["machine-a"].root_globs[0].glob == "~/projects/*/.codex"
    assert len(config.retention_rules) == 1
    assert config.retention_rules[0].name == "machine-a-codex-raw"
    assert config.retention_rules[0].remove_source is True


def test_build_raw_view_uses_stable_local_extras_and_raw_imports(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    projection_home = tmp_path / "projection-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".gemini" / "tmp").mkdir(parents=True)
    (imports / "machine-a" / ".raw" / "codex").mkdir(parents=True)
    (imports / "machine-a" / ".raw" / "gemini").mkdir(parents=True)
    (imports / "local-home" / ".raw" / "codex").mkdir(parents=True)
    (imports / "local-home" / ".raw" / "gemini").mkdir(parents=True)
    projection_home.mkdir()
    (workspace / "proj-a" / ".codex").mkdir(parents=True)
    (extras / "volatile-codex-homes" / "codex" / "sessions").mkdir(parents=True)
    (extras / "volatile-codex-homes" / "sync-state.json").write_text("{}\n", encoding="utf-8")
    (extras / "canonical-only" / "codex" / "sessions").mkdir(parents=True)

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
clients = ["codex", "gemini"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    view = build_view(config, mode="raw")

    assert view.home == projection_home
    assert view.home != home
    assert ("codex", imports / "local-home" / ".raw" / "codex") in view.extra_dirs
    assert ("gemini", imports / "local-home" / ".raw" / "gemini") in view.extra_dirs
    assert ("codex", imports / "machine-a" / ".raw" / "codex") in view.extra_dirs
    assert ("gemini", imports / "machine-a" / ".raw" / "gemini") in view.extra_dirs
    assert ("codex", workspace / "proj-a" / ".codex") not in view.extra_dirs
    assert ("codex", extras / "volatile-codex-homes" / "codex") in view.extra_dirs
    assert ("codex", extras / "canonical-only" / "codex") not in view.extra_dirs


def test_build_raw_view_does_not_read_home_project_archives_directly(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    (home / ".codex" / "sessions").mkdir(parents=True)
    archived_codex = home / ".codex" / "projects" / "proj-b" / "archive" / "20260411T000000Z" / "codex"
    (archived_codex / "sessions").mkdir(parents=True)
    (home / ".codex" / "projects" / "proj-b" / "runtime-state" / "codex" / "sessions").mkdir(parents=True)
    (home / ".codex" / "projects" / "external" / "ppt-master").mkdir(parents=True)
    (extras / "home-project-archives" / "codex" / "sessions").mkdir(parents=True)
    (extras / "home-project-archives" / "sync-state.json").write_text("{}\n", encoding="utf-8")

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
    view = build_view(config, mode="raw")

    assert ("codex", archived_codex) not in view.extra_dirs
    assert ("codex", extras / "home-project-archives" / "codex") in view.extra_dirs
    assert ("codex", home / ".codex" / "projects" / "proj-b") not in view.extra_dirs
    assert all("runtime-state" not in root.as_posix() for client, root in view.extra_dirs if client == "codex")


def test_build_canonical_view_uses_shadow_home_and_extras(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    (imports / "machine-a" / "codex").mkdir(parents=True)
    (imports / "machine-a" / "openclaw").mkdir(parents=True)
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
clients = ["codex", "openclaw"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    view = build_view(config, mode="canonical", omx_replay_dedupe="strict")

    assert view.home == shadow_home
    assert ("codex", imports / "machine-a" / "codex") in view.extra_dirs
    assert ("openclaw", imports / "machine-a" / "openclaw") in view.extra_dirs
    assert ("codex", extras / "proj-a" / "codex") in view.extra_dirs

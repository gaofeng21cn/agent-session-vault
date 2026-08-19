from pathlib import Path

import pytest

from agent_session_vault.config import load_config
from agent_session_vault.views import build_tokscale_view


def test_load_config_reads_current_paths_and_archive(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[paths]
home = "/tmp/home"
workspace_root = "/tmp/workspace"
import_root = "/tmp/imports"
projection_home = "/tmp/projection-home"
local_workspace_extras = "/tmp/local-workspace-extras"
stable_root = "/tmp/stable"

[archive]
root = "/tmp/archive"
cadence_days = 14
cold_age_days = 30
staging_root = "/tmp/archive-staging"
machine_id_path = "/tmp/machine-id"
source_paths = ["~/.codex"]
require_quiescent_for_prune = true
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.paths.home == Path("/tmp/home")
    assert config.paths.projection_home == Path("/tmp/projection-home")
    assert config.paths.stable_root == Path("/tmp/stable")
    assert config.archive.root == Path("/tmp/archive")
    assert config.archive.cadence_days == 14
    assert config.archive.cold_age_days == 30
    assert config.archive.source_paths[0].path == "~/.codex"


@pytest.mark.parametrize(
    "payload, error",
    [
        ("[unsupported]\nvalue = true\n", "unsupported top-level fields"),
        ("[paths]\nunsupported = \"value\"\n", "unsupported paths fields"),
        ("[archive]\ncadence_days = \"14\"\n", "archive.cadence_days must be an integer"),
        ("[archive]\nsource_paths = [{}]\n", "archive.source_paths[0].path must be a non-empty string"),
    ],
)
def test_load_config_rejects_non_current_schema(tmp_path: Path, payload: str, error: str) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match=error.replace("[", r"\[").replace("]", r"\]")):
        load_config(config_path)


def test_tokscale_view_uses_only_managed_projection_inputs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    imports = tmp_path / "imports"
    projection_home = tmp_path / "projection-home"
    extras = tmp_path / "extras"
    workspace = tmp_path / "workspace"

    (home / ".codex" / "sessions").mkdir(parents=True)
    (workspace / "project" / ".codex").mkdir(parents=True)
    (imports / "local-home" / ".raw" / "codex").mkdir(parents=True)
    (imports / "fleet-node" / ".raw" / "gemini").mkdir(parents=True)
    (extras / "managed" / "codex").mkdir(parents=True)
    (extras / "managed" / "sync-state.json").write_text("{}\n", encoding="utf-8")
    (extras / "unmanaged" / "codex").mkdir(parents=True)
    projection_home.mkdir()

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{workspace}"
import_root = "{imports}"
projection_home = "{projection_home}"
local_workspace_extras = "{extras}"
stable_root = "{tmp_path / 'stable'}"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    view = build_tokscale_view(load_config(config_path))

    assert view.home == projection_home
    assert ("codex", imports / "local-home" / ".raw" / "codex") in view.extra_dirs
    assert ("gemini", imports / "fleet-node" / ".raw" / "gemini") in view.extra_dirs
    assert ("codex", extras / "managed" / "codex") in view.extra_dirs
    assert ("codex", workspace / "project" / ".codex") not in view.extra_dirs
    assert ("codex", extras / "unmanaged" / "codex") not in view.extra_dirs
    assert all(home / ".codex" != path for _, path in view.extra_dirs)

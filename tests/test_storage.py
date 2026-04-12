from pathlib import Path

from agent_session_vault.config import load_config
from agent_session_vault.storage import summarize_storage


def test_summarize_storage_collects_existing_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    target = home / ".codex" / "sessions"
    target.mkdir(parents=True)
    (target / "a.jsonl").write_text("x" * 16, encoding="utf-8")
    (imports / "imac" / ".raw" / "codex").mkdir(parents=True)
    (imports / "imac" / ".raw" / "codex" / "b.jsonl").write_text("y" * 8, encoding="utf-8")
    shadow_home.mkdir(parents=True)

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
    summary = summarize_storage(config)

    keys = {item.label for item in summary.items}
    assert "live:codex" in keys
    assert "imports_raw:imac:codex" in keys
    assert "canonical:shadow_home" in keys
    assert summary.total_bytes >= 24


def test_summarize_storage_collects_home_project_codex_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"

    migrated_project = home / ".codex" / "projects" / "proj-b" / "archive" / "20260411T000000Z" / "codex" / "sessions"
    migrated_project.mkdir(parents=True)
    (migrated_project / "session.jsonl").write_text("z" * 12, encoding="utf-8")

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
    summary = summarize_storage(config)

    keys = {item.label for item in summary.items}
    assert "live:home_project_codex:proj-b" in keys
    assert summary.total_bytes >= 12

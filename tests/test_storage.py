from pathlib import Path

from agent_session_vault.config import load_config
from agent_session_vault.storage import summarize_storage


def _config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{tmp_path / 'home'}"
workspace_root = "{tmp_path / 'workspace'}"
import_root = "{tmp_path / 'imports'}"
projection_home = "{tmp_path / 'projection-home'}"
local_workspace_extras = "{tmp_path / 'extras'}"
stable_root = "{tmp_path / 'stable'}"

[archive]
root = "{tmp_path / 'archive'}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


def test_summarize_storage_reports_current_domains(tmp_path: Path) -> None:
    (tmp_path / "home" / ".codex" / "sessions").mkdir(parents=True)
    (tmp_path / "home" / ".codex" / "sessions" / "a.jsonl").write_text("x" * 16, encoding="utf-8")
    (tmp_path / "imports" / "node-a" / ".raw" / "codex").mkdir(parents=True)
    (tmp_path / "imports" / "node-a" / ".raw" / "codex" / "b.jsonl").write_text("y" * 8, encoding="utf-8")
    (tmp_path / "extras" / "managed" / "codex").mkdir(parents=True)
    (tmp_path / "stable").mkdir()
    (tmp_path / "archive").mkdir()

    summary = summarize_storage(load_config(_config(tmp_path)))
    keys = {item.label for item in summary.items}

    assert "live:codex" in keys
    assert "projection:node-a:codex" in keys
    assert "projection:managed_extras" in keys
    assert "stable:analytics" in keys
    assert "archive:full_fidelity" in keys
    assert summary.total_bytes >= 24


def test_summarize_storage_collects_home_project_codex_archives(tmp_path: Path) -> None:
    migrated = tmp_path / "home" / ".codex" / "projects" / "proj-b" / "archive" / "20260411T000000Z" / "codex"
    (migrated / "sessions").mkdir(parents=True)
    (migrated / "sessions" / "session.jsonl").write_text("z" * 12, encoding="utf-8")
    runtime_file = tmp_path / "home" / ".codex" / "projects" / "proj-b" / "runtime-state" / "cache.bin"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("ignored", encoding="utf-8")

    summary = summarize_storage(load_config(_config(tmp_path)))
    items = [item for item in summary.items if item.label.startswith("live:home_project_codex:")]

    assert len(items) == 1
    assert items[0].label == "live:home_project_codex:proj-b:20260411T000000Z"
    assert items[0].size_bytes == 12

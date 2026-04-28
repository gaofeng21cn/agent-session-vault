import gzip
import json
from pathlib import Path

from agent_session_vault.config import load_config
from agent_session_vault.local_codex import sync_local_codex_sources
from scripts.sync_local_codex_tokscale_sources import discover_runtime_roots


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _write_jsonl_gz(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(line) for line in lines) + "\n")


def _config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{tmp_path / 'home'}"
workspace_root = "{tmp_path / 'workspace'}"
import_root = "{tmp_path / 'imports'}"
shadow_home = "{tmp_path / 'shadow-home'}"
local_workspace_extras = "{tmp_path / 'extras'}"
archive_root = "{tmp_path / 'archive'}"
relay_root = "{tmp_path / 'relay'}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


def test_sync_local_codex_sources_projects_live_and_cold_archived_sessions(tmp_path: Path) -> None:
    quest_root = tmp_path / "quest"
    live_session = (
        quest_root
        / ".ds"
        / "codex_homes"
        / "run-live"
        / "sessions"
        / "2026"
        / "04"
        / "28"
        / "live.jsonl"
    )
    archived_session = (
        quest_root
        / ".ds"
        / "cold_archive"
        / "codex_sessions"
        / ".ds"
        / "codex_homes"
        / "run-cold"
        / "sessions"
        / "2026"
        / "04"
        / "27"
        / "cold.jsonl.gz"
    )
    token_event = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"total_tokens": 100}},
        },
    }
    noisy_message = {"type": "response_item", "payload": {"type": "message", "content": "not needed"}}
    _write_jsonl(live_session, [{"type": "session_meta"}, noisy_message, token_event])
    _write_jsonl_gz(archived_session, [{"type": "session_meta"}, token_event])

    config = load_config(_config(tmp_path))
    result = sync_local_codex_sources(config, sources=[quest_root], namespace="volatile-codex-homes")

    stable_root = tmp_path / "extras" / "volatile-codex-homes" / "codex"
    projected = sorted(stable_root.rglob("*.jsonl"))
    assert result.files_seen == 2
    assert result.files_written == 2
    assert len(projected) == 2
    assert all("/sessions/" in path.as_posix() for path in projected)
    assert sum(path.read_text(encoding="utf-8").count('"token_count"') for path in projected) == 2
    assert all("not needed" not in path.read_text(encoding="utf-8") for path in projected)

    live_session.unlink()
    second = sync_local_codex_sources(config, sources=[quest_root], namespace="volatile-codex-homes")

    assert second.files_written == 0
    assert second.files_skipped == 1
    assert len(sorted(stable_root.rglob("*.jsonl"))) == 2


def test_discover_runtime_roots_finds_ds_codex_homes_and_cold_archive(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    quest_a = workspace / "project-a" / "ops" / "quest-a"
    quest_b = workspace / "project-b" / "ops" / "quest-b"
    ignored = workspace / "project-c"

    (quest_a / ".ds" / "codex_homes").mkdir(parents=True)
    (quest_b / ".ds" / "cold_archive" / "codex_sessions").mkdir(parents=True)
    (ignored / ".ds" / "bash_exec").mkdir(parents=True)

    assert discover_runtime_roots(workspace) == [quest_a.resolve(), quest_b.resolve()]

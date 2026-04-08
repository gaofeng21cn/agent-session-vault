from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path

from agent_session_vault.archive import inventory_bundles
from agent_session_vault.config import load_config
from agent_session_vault.retention import apply_archive_plan, build_archive_plan


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _set_mtime(path: Path, timestamp: float) -> None:
    os.utime(path, (timestamp, timestamp))


def test_build_archive_plan_selects_due_import_rule(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"
    relay = tmp_path / "relay"
    source = imports / "imac" / ".raw" / "codex" / "sessions" / "2026" / "03" / "20" / "one.jsonl"
    _write(source, "alpha")
    _set_mtime(source, datetime(2026, 3, 20, tzinfo=UTC).timestamp())

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
relay_root = "{relay}"

[machines.imac]
import_name = "imac"
ssh_target = "tokscale-sync-imac"
clients = ["codex"]

[[retention.rules]]
name = "imac-codex-raw"
layer = "imports_raw"
machine = "imac"
client = "codex"
max_age_days = 7
min_size_bytes = 1
archive_subdir = "imports-raw"
remove_source = false
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    plan = build_archive_plan(config, now=datetime(2026, 4, 7, tzinfo=UTC))

    assert len(plan) == 1
    candidate = plan[0]
    assert candidate.rule_name == "imac-codex-raw"
    assert candidate.source == imports / "imac" / ".raw" / "codex"
    assert candidate.archive_dir == archive / "imports-raw"
    assert candidate.age_days >= 7
    assert candidate.size_bytes >= 1


def test_apply_archive_plan_offloads_bundle_and_removes_source_when_requested(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    imports = tmp_path / "imports"
    shadow_home = tmp_path / "shadow-home"
    extras = tmp_path / "extras"
    archive = tmp_path / "archive"
    relay = tmp_path / "relay"
    source = imports / "imac" / ".raw" / "codex" / "sessions" / "2026" / "03" / "20" / "one.jsonl"
    _write(source, "alpha")
    _set_mtime(source, datetime(2026, 3, 20, tzinfo=UTC).timestamp())

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
relay_root = "{relay}"

[machines.imac]
import_name = "imac"
ssh_target = "tokscale-sync-imac"
clients = ["codex"]

[[retention.rules]]
name = "imac-codex-raw"
layer = "imports_raw"
machine = "imac"
client = "codex"
max_age_days = 7
min_size_bytes = 1
archive_subdir = "imports-raw"
remove_source = true
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    plan = build_archive_plan(config, now=datetime(2026, 4, 7, tzinfo=UTC))
    results = apply_archive_plan(config, plan)

    assert len(results) == 1
    assert results[0].bundle_path.exists()
    assert not (imports / "imac" / ".raw" / "codex").exists()
    inventory = inventory_bundles(archive)
    assert results[0].bundle_path in {item.bundle_path for item in inventory}

from __future__ import annotations

import json
from pathlib import Path
import shutil

from agent_session_vault.cli import main
from agent_session_vault.config import load_config
from agent_session_vault.stable import (
    default_stable_root,
    migration_plan_payload,
    mirror_stable_layer,
    restore_stable_layer,
    stable_mirror_payload,
)


def _write_config(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    imports = tmp_path / "imports"
    extras = tmp_path / "local-workspace-extras"
    archive = tmp_path / "OneDrive" / "agent-session-vault" / "archive"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{tmp_path / 'workspace'}"
import_root = "{imports}"
shadow_home = "{tmp_path / 'shadow-home'}"
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
    (imports / "machine-a" / ".raw" / "codex").mkdir(parents=True)
    (imports / "machine-a" / ".raw" / "codex" / "one.jsonl").write_text("codex", encoding="utf-8")
    (extras / "volatile-codex-homes" / "codex").mkdir(parents=True)
    (extras / "volatile-codex-homes" / "codex" / "two.jsonl").write_text("extra", encoding="utf-8")
    pricing_path = home / ".config" / "tokscale" / "projection-home" / ".config" / "tokscale" / "custom-pricing.json"
    pricing_path.parent.mkdir(parents=True)
    pricing_path.write_text('{"models":{"custom-model":{"input_cost_per_million_tokens":1}}}\n', encoding="utf-8")
    return config_path


def test_default_stable_root_uses_archive_parent(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))

    assert default_stable_root(config) == tmp_path / "OneDrive" / "agent-session-vault" / "stable"


def test_mirror_stable_layer_plans_imports_extras_and_config(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))

    result = mirror_stable_layer(config, dry_run=True)
    payload = stable_mirror_payload(result)

    assert payload["dry_run"] is True
    destinations = {item["label"]: item["destination"] for item in payload["items"]}
    assert destinations["imports"].endswith("/stable/packs/imports")
    assert destinations["local_workspace_extras"].endswith("/stable/packs/local-workspace-extras")
    assert destinations["config"].endswith("/stable/config/config.toml")
    assert destinations["tokscale_custom_pricing"].endswith("/stable/config/tokscale/custom-pricing.json")
    assert {item["status"] for item in payload["items"]} == {"planned"}
    assert payload["mirror_semantics"] == "packed-source-covered"


def test_mirror_stable_layer_packs_directories_and_copies_config(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))

    result = mirror_stable_layer(config)
    payload = stable_mirror_payload(result)

    stable_root = default_stable_root(config)
    assert list((stable_root / "packs" / "imports").glob("pack-*.tar.zst"))
    assert list((stable_root / "packs" / "local-workspace-extras").glob("pack-*.tar.zst"))
    assert (default_stable_root(config) / "config" / "config.toml").read_text(encoding="utf-8") == config.config_path.read_text(
        encoding="utf-8"
    )
    assert (default_stable_root(config) / "config" / "tokscale" / "custom-pricing.json").read_text(
        encoding="utf-8"
    ) == (config.paths.projection_home / ".config" / "tokscale" / "custom-pricing.json").read_text(
        encoding="utf-8"
    )
    assert payload["manifest_path"].endswith("/stable/stable-layer-manifest.json")
    assert {item["status"] for item in payload["items"]} == {"mirrored"}
    assert {item["coverage_status"] for item in payload["items"]} == {"verified"}
    assert {item["archive_format"] for item in payload["items"] if item["kind"] == "directory"} == {
        "tar-zstd-shards-v1"
    }
    assert payload["status"] == "verified"


def test_migration_profile_includes_live_sessions_and_reports_readiness(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    sessions = config.paths.home / ".codex" / "sessions" / "2026" / "07" / "14"
    archived = config.paths.home / ".codex" / "archived_sessions"
    sessions.mkdir(parents=True)
    archived.mkdir(parents=True)
    (sessions / "live.jsonl").write_text("live\n", encoding="utf-8")
    (archived / "old.jsonl").write_text("old\n", encoding="utf-8")

    before = migration_plan_payload(config)
    assert before["readiness"]["full_fidelity_restore_ready"] is False
    assert "live_sessions_not_verified" not in before["readiness"]["blockers"]
    assert "live_sessions_not_verified" in before["readiness"]["optional_migration_blockers"]

    result = mirror_stable_layer(config, include_live_sessions=True)
    after = migration_plan_payload(config)

    assert result.profile == "migration"
    assert result.status == "verified"
    assert after["readiness"]["analytics_restore_ready"] is True
    assert after["readiness"]["full_fidelity_restore_ready"] is True
    labels = {item["label"] for item in after["items"] if item["source_exists"]}
    assert "live_codex_sessions" in labels
    assert "live_codex_archived_sessions" in labels

    (sessions / "live.jsonl").write_text("live changed after mirror\n", encoding="utf-8")
    stale = migration_plan_payload(config)
    assert stale["readiness"]["analytics_restore_ready"] is True
    assert stale["readiness"]["full_fidelity_restore_ready"] is False


def test_stable_mirror_repacks_changed_source_and_restores_updated_bytes(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    first = mirror_stable_layer(config)
    source_file = config.paths.import_root / "machine-a" / ".raw" / "codex" / "one.jsonl"
    source_file.write_text("updated-content", encoding="utf-8")

    second = mirror_stable_layer(config)
    restore_root = tmp_path / "restore"
    restored = restore_stable_layer(default_stable_root(config), restore_root, labels={"imports"})
    destination_file = restore_root / "tokscale" / "imports" / "machine-a" / ".raw" / "codex" / "one.jsonl"

    assert first.status == "verified"
    assert second.status == "verified"
    assert destination_file.read_text(encoding="utf-8") == "updated-content"
    assert restored["restored_files"] == 1
    assert not any((default_stable_root(config) / ".asv-replaced").rglob("*"))


def test_stable_mirror_reuses_verified_packs(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    first = mirror_stable_layer(config)
    packs = sorted((default_stable_root(config) / "packs").rglob("pack-*.tar.zst"))
    first_pack_state = [(path.name, path.stat().st_mtime_ns) for path in packs]
    second = mirror_stable_layer(config)

    assert first.status == "verified"
    assert second.status == "verified"
    assert [(path.name, path.stat().st_mtime_ns) for path in packs] == first_pack_state
    assert {item.transfer_status for item in second.items} == {"reused_verified"}


def test_stable_mirror_repairs_missing_pack_after_manifest_reuse(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    first = mirror_stable_layer(config)
    imports_pack_root = default_stable_root(config) / "packs" / "imports"
    missing_pack = next(imports_pack_root.glob("pack-*.tar.zst"))
    missing_pack.unlink()

    second = mirror_stable_layer(config)
    repaired_packs = list(imports_pack_root.glob("pack-*.tar.zst"))

    assert first.status == "verified"
    assert second.status == "verified"
    assert repaired_packs
    assert next(item for item in second.items if item.label == "imports").transfer_status == "packed"


def test_stable_mirror_rewrites_only_the_changed_shard(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    source_root = config.paths.import_root
    for index in range(8):
        (source_root / f"{index:02d}.jsonl").write_text("x" * 600, encoding="utf-8")

    first = mirror_stable_layer(config, shard_target_bytes=1024)
    pack_root = default_stable_root(config) / "packs" / "imports"
    first_archives = {path.name for path in pack_root.glob("pack-*.tar.zst")}
    index_payload = json.loads((pack_root / "index.json").read_text(encoding="utf-8"))
    changed_relative = "00.jsonl"
    changed_shard = index_payload["files"][changed_relative]["shard"]
    unchanged_archives = {
        payload["archive_path"]
        for shard, payload in index_payload["shards"].items()
        if shard != changed_shard
    }

    (source_root / changed_relative).write_text("changed", encoding="utf-8")
    second = mirror_stable_layer(config, shard_target_bytes=1024)
    second_archives = {path.name for path in pack_root.glob("pack-*.tar.zst")}

    assert first.status == "verified"
    assert second.status == "verified"
    assert unchanged_archives.issubset(first_archives & second_archives)
    assert len(first_archives - second_archives) == 1
    assert len(second_archives - first_archives) == 1


def test_prune_unpacked_removes_only_legacy_stable_trees_after_verification(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    stable_root = default_stable_root(config)
    legacy_imports = stable_root / "tokscale" / "imports"
    legacy_extras = stable_root / "tokscale" / "local-workspace-extras"
    legacy_imports.mkdir(parents=True)
    legacy_extras.mkdir(parents=True)
    (legacy_imports / "old.jsonl").write_text("old import", encoding="utf-8")
    (legacy_extras / "old.jsonl").write_text("old extra", encoding="utf-8")

    result = mirror_stable_layer(config, prune_unpacked=True)
    payload = stable_mirror_payload(result)

    assert result.status == "verified"
    assert not legacy_imports.exists()
    assert not legacy_extras.exists()
    assert payload["pruned_unpacked_files"] == 2
    assert len(payload["pruned_unpacked_paths"]) == 2
    assert list((stable_root / "packs" / "imports").glob("pack-*.tar.zst"))


def test_restore_stable_layer_round_trip_all_analytics_items(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    result = mirror_stable_layer(config)
    relocated_stable = tmp_path / "relocated-stable"
    shutil.copytree(default_stable_root(config), relocated_stable)
    restore_root = tmp_path / "restore"

    restored = restore_stable_layer(relocated_stable, restore_root)

    assert result.status == "verified"
    assert restored["status"] == "verified"
    assert restored["restored_files"] == 4
    assert (
        restore_root / "tokscale" / "imports" / "machine-a" / ".raw" / "codex" / "one.jsonl"
    ).read_text(encoding="utf-8") == "codex"
    assert (
        restore_root
        / "tokscale"
        / "local-workspace-extras"
        / "volatile-codex-homes"
        / "codex"
        / "two.jsonl"
    ).read_text(encoding="utf-8") == "extra"
    assert (restore_root / "config" / "config.toml").is_file()
    assert (
        restore_root / "tokscale" / "projection-home" / ".config" / "tokscale" / "custom-pricing.json"
    ).is_file()


def test_cli_mirror_and_restore_stable_round_trip(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    restore_root = tmp_path / "cli-restore"

    mirror_exit = main(["--config", str(config_path), "storage", "mirror-stable", "--json"])
    mirror_payload = json.loads(capsys.readouterr().out)
    restore_exit = main(
        [
            "--config",
            str(config_path),
            "storage",
            "restore-stable",
            "--dest-root",
            str(restore_root),
            "--json",
        ]
    )
    restore_payload = json.loads(capsys.readouterr().out)

    assert mirror_exit == 0
    assert mirror_payload["status"] == "verified"
    assert restore_exit == 0
    assert restore_payload["status"] == "verified"
    assert restore_payload["restored_files"] == 4
    assert (
        restore_root / "tokscale" / "projection-home" / ".config" / "tokscale" / "custom-pricing.json"
    ).is_file()

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from agent_session_vault.cli import main
from agent_session_vault.config import load_config
from agent_session_vault.stable import (
    LEGACY_STABLE_ARCHIVE_FORMAT,
    STABLE_ARCHIVE_FORMAT,
    default_stable_root,
    migration_plan_payload,
    mirror_stable_layer,
    restore_stable_layer,
    stable_mirror_payload,
    stable_repo_path,
)


def _write_config(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    imports = tmp_path / "imports"
    extras = tmp_path / "local-workspace-extras"
    stable = tmp_path / "OneDrive" / "agent-session-vault" / "stable"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{tmp_path / 'workspace'}"
import_root = "{imports}"
projection_home = "{home / '.config' / 'tokscale' / 'projection-home'}"
local_workspace_extras = "{extras}"
stable_root = "{stable}"
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


def test_default_stable_root_uses_configured_path(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    assert default_stable_root(config) == tmp_path / "OneDrive" / "agent-session-vault" / "stable"


def test_mirror_stable_layer_plans_imports_extras_and_config(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))

    result = mirror_stable_layer(config, dry_run=True)
    payload = stable_mirror_payload(result)

    assert payload["dry_run"] is True
    assert payload["mirror_semantics"] == "restic-source-covered"
    assert {item["label"] for item in payload["items"]} == {
        "imports",
        "local_workspace_extras",
        "config",
        "tokscale_custom_pricing",
    }
    assert {item["status"] for item in payload["items"]} == {"planned"}
    assert not stable_repo_path(default_stable_root(config)).exists()


def test_mirror_stable_layer_backs_up_to_restic_repo(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))

    result = mirror_stable_layer(config)
    payload = stable_mirror_payload(result)

    stable_root = default_stable_root(config)
    assert (stable_repo_path(stable_root) / "config").is_file()
    assert payload["manifest_path"].endswith("/stable/stable-layer-manifest.json")
    assert {item["status"] for item in payload["items"]} == {"mirrored"}
    assert {item["coverage_status"] for item in payload["items"]} == {"verified"}
    assert {item["archive_format"] for item in payload["items"]} == {STABLE_ARCHIVE_FORMAT}
    assert all(isinstance(item["restic_snapshot_id"], str) and item["restic_snapshot_id"] for item in payload["items"])
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


def test_stable_mirror_updates_changed_source_and_restores_updated_bytes(tmp_path: Path) -> None:
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


def test_stable_mirror_reuses_verified_restic_snapshots(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    first = mirror_stable_layer(config)
    first_snaps = {item.label: item.restic_snapshot_id for item in first.items}

    second = mirror_stable_layer(config)
    second_snaps = {item.label: item.restic_snapshot_id for item in second.items}

    assert first.status == "verified"
    assert second.status == "verified"
    assert second_snaps == first_snaps
    assert {item.transfer_status for item in second.items} == {"reused_verified"}


def test_stable_mirror_repairs_missing_restic_snapshot(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    first = mirror_stable_layer(config)
    imports_snap = next(item.restic_snapshot_id for item in first.items if item.label == "imports")
    assert imports_snap is not None
    (stable_repo_path(default_stable_root(config)) / "snapshots" / f"{imports_snap}.json").unlink()

    second = mirror_stable_layer(config)
    imports_after = next(item for item in second.items if item.label == "imports")

    assert second.status == "verified"
    assert imports_after.transfer_status == "backed_up"
    assert imports_after.restic_snapshot_id != imports_snap


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


def test_restore_stable_layer_refuses_legacy_tar_shards_and_existing_target(tmp_path: Path) -> None:
    legacy_root = tmp_path / "legacy-stable"
    legacy_root.mkdir()
    (legacy_root / "stable-layer-manifest.json").write_text(
        json.dumps(
            {
                "status": "verified",
                "items": [
                    {
                        "label": "imports",
                        "kind": "directory",
                        "source_exists": True,
                        "archive_format": LEGACY_STABLE_ARCHIVE_FORMAT,
                        "restore_relative_path": "tokscale/imports",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="legacy tar-zstd-shards-v1"):
        restore_stable_layer(legacy_root, tmp_path / "out")

    config = load_config(_write_config(tmp_path))
    assert mirror_stable_layer(config).status == "verified"
    existing_dest = tmp_path / "existing-dest"
    (existing_dest / "config" / "config.toml").parent.mkdir(parents=True)
    (existing_dest / "config" / "config.toml").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(FileExistsError):
        restore_stable_layer(default_stable_root(config), existing_dest, labels={"config"})


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

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
import shutil

import pytest

from agent_session_vault.archive_ops import archive_cycle, build_snapshot, init_backend, publish_snapshot, verify_snapshot
from agent_session_vault.archive_prune import apply_prune_plan, build_prune_plan, load_prune_plan, write_prune_plan
from agent_session_vault.archive_restore import build_restore_plan, restore_plan
from agent_session_vault.archive_sources import scan_codex_sources
from agent_session_vault.config import load_config
from agent_session_vault.stable import mirror_stable_layer


def _config(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    source = home / ".codex"
    backend = tmp_path / "nas"
    staging = tmp_path / "staging"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{tmp_path / 'workspace'}"
import_root = "{tmp_path / 'imports'}"
projection_home = "{tmp_path / 'projection-home'}"
shadow_home = "{tmp_path / 'shadow-home'}"
local_workspace_extras = "{tmp_path / 'extras'}"
archive_root = "{tmp_path / 'legacy-archive'}"
relay_root = "{tmp_path / 'relay'}"

[archive]
primary_backend = "nas"
primary_root = "{backend}"
staging_root = "{staging}"
machine_id_path = "{tmp_path / 'machine-id'}"
source_paths = ["{source}"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (source / "sessions" / "2026" / "08" / "19").mkdir(parents=True)
    (source / "archived_sessions").mkdir(parents=True)
    return config_path, source, backend


def _write_session(path: Path, session_id: str, message: str) -> None:
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": session_id}}),
                json.dumps({"type": "event_msg", "timestamp": "2026-08-19T01:02:03Z", "payload": {"type": "user_message", "message": message}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _prepare_projection_and_stable(config, source_path: Path) -> Path:
    destination = (
        config.paths.import_root
        / "local-home"
        / ".raw"
        / "codex"
        / "archived_sessions"
        / "home-test"
        / source_path.name
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text('{"type":"event_msg","payload":{"type":"token_count","total":42}}\n', encoding="utf-8")
    state_path = config.paths.import_root / "local-home" / ".local-home-projection-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "status": "valid",
                "files": {
                    str(source_path.resolve()): {
                        "destination": str(destination),
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert mirror_stable_layer(config).status == "verified"
    return destination


def _preview(_config) -> dict[str, object]:
    return {
        "package": "tokscale@4.13.0",
        "clients": ["codex", "gemini", "openclaw"],
        "client_args": ["-c", "codex,gemini,openclaw"],
        "statistics": {"total_tokens": 42},
    }


def _current_machine_id(config) -> str:
    return scan_codex_sources(config).machine_id


def test_codex_snapshot_publish_and_deep_verify_reuses_unchanged_objects(tmp_path: Path) -> None:
    config_path, source, backend = _config(tmp_path)
    config = load_config(config_path)
    _write_session(source / "sessions" / "2026" / "08" / "19" / "one.jsonl", "session-one", "one")
    _write_session(source / "archived_sessions" / "old.jsonl", "session-old", "old")
    init_backend(config)

    first = build_snapshot(config, machine_id="machine-test")
    published_first = publish_snapshot(config, first.staging_root)
    assert len(published_first) == 1
    first_snapshot_id = first.snapshots[0].snapshot_id
    assert verify_snapshot(config, first_snapshot_id, deep=True)["status"] == "verified"

    first_objects = sorted((backend / "objects").glob("*.tar.zst"))
    assert len(first_objects) == 1

    _write_session(source / "sessions" / "2026" / "08" / "19" / "one.jsonl", "session-one", "changed")
    second = build_snapshot(config, machine_id="machine-test")
    publish_snapshot(config, second.staging_root)
    second_snapshot_id = second.snapshots[0].snapshot_id
    assert verify_snapshot(config, second_snapshot_id, deep=True)["status"] == "verified"
    assert len(list((backend / "objects").glob("*.tar.zst"))) == 2


def test_catalog_and_staging_restore_round_trip_selected_session(tmp_path: Path) -> None:
    config_path, source, _ = _config(tmp_path)
    config = load_config(config_path)
    _write_session(source / "sessions" / "2026" / "08" / "19" / "one.jsonl", "session-one", "one")
    _write_session(source / "archived_sessions" / "old.jsonl", "session-old", "old")
    init_backend(config)
    result = build_snapshot(config, machine_id="machine-test")
    publish_snapshot(config, result.staging_root)

    destination = tmp_path / "restore"
    plan = build_restore_plan(config, destination=destination, session_id="session-one")
    restored = restore_plan(config, plan)
    assert restored["status"] == "verified"
    restored_files = list(destination.rglob("*.jsonl"))
    assert len(restored_files) == 1
    assert "session-one" in restored_files[0].read_text(encoding="utf-8")


def test_staging_restore_reuses_hardlinked_codex_session_content(tmp_path: Path) -> None:
    config_path, source, _ = _config(tmp_path)
    config = load_config(config_path)
    session = source / "sessions" / "2026" / "08" / "19" / "one.jsonl"
    _write_session(session, "session-one", "one")
    os.link(session, source / "archived_sessions" / "one.jsonl")
    init_backend(config)
    result = build_snapshot(config, machine_id="machine-test")
    publish_snapshot(config, result.staging_root)
    assert verify_snapshot(config, result.snapshots[0].snapshot_id, deep=True)["status"] == "verified"

    destination = tmp_path / "restore"
    plan = build_restore_plan(config, destination=destination, session_id="session-one")
    restored = restore_plan(config, plan)
    assert restored["restored_files"] == 2
    restored_files = sorted(destination.rglob("*.jsonl"))
    assert len(restored_files) == 2
    assert restored_files[0].read_bytes() == restored_files[1].read_bytes()


def test_missing_codex_source_is_reported_without_creating_a_snapshot(tmp_path: Path) -> None:
    config_path, source, _ = _config(tmp_path)
    config = load_config(config_path)
    init_backend(config)
    shutil.rmtree(source)
    result = build_snapshot(config, machine_id="machine-test")
    assert result.snapshots == ()
    assert str(source) in result.scan.missing_sources


def test_archive_cycle_is_idempotent_with_cadence_state(tmp_path: Path) -> None:
    config_path, source, _ = _config(tmp_path)
    config = load_config(config_path)
    _write_session(source / "archived_sessions" / "old.jsonl", "session-old", "old")
    init_backend(config)
    first = archive_cycle(config, machine_id="machine-test", due_only=False)
    assert first.status == "verified"
    second = archive_cycle(config, machine_id="machine-test", due_only=True)
    assert second.status == "not_due"


def test_deep_verify_detects_manifest_checksum_corruption(tmp_path: Path) -> None:
    config_path, source, backend = _config(tmp_path)
    config = load_config(config_path)
    _write_session(source / "archived_sessions" / "old.jsonl", "session-old", "old")
    init_backend(config)
    result = build_snapshot(config, machine_id="machine-test")
    published = publish_snapshot(config, result.staging_root)
    manifest_path = published[0].snapshot_dir / "manifest.json"
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8").replace("session-old", "session-corrupt"), encoding="utf-8")
    verification = verify_snapshot(config, result.snapshots[0].snapshot_id, deep=True)
    assert verification["status"] == "failed"
    assert "manifest_checksum_mismatch" in verification["failures"]


def test_prune_plan_requires_snapshot_projection_and_stable_coverage_then_preserves_tokscale(tmp_path: Path) -> None:
    config_path, source, _ = _config(tmp_path)
    config = load_config(config_path)
    old = source / "archived_sessions" / "old.jsonl"
    _write_session(old, "session-old", "old")
    init_backend(config)
    built = build_snapshot(config, machine_id=_current_machine_id(config))
    publish_snapshot(config, built.staging_root)
    projected = _prepare_projection_and_stable(config, old)

    plan = build_prune_plan(config, now=datetime(2026, 9, 20, tzinfo=UTC), preview_runner=_preview)
    assert len(plan.entries) == 1
    assert plan.entries[0].source_path == str(old)
    assert plan.entries[0].projection_path == str(projected)
    plan_path = write_prune_plan(plan, tmp_path / "prune-plan.json")
    loaded = load_prune_plan(plan_path)
    assert loaded.plan_digest == plan.plan_digest

    result = apply_prune_plan(config, loaded, preview_runner=_preview)
    assert result["status"] == "verified"
    assert result["deleted_file_count"] == 1
    assert not old.exists()
    assert projected.is_file()


def test_prune_plan_skips_session_hardlinks(tmp_path: Path) -> None:
    config_path, source, _ = _config(tmp_path)
    config = load_config(config_path)
    active = source / "sessions" / "2026" / "08" / "19" / "one.jsonl"
    _write_session(active, "session-one", "old")
    archived = source / "archived_sessions" / "one.jsonl"
    os.link(active, archived)
    init_backend(config)
    built = build_snapshot(config, machine_id=_current_machine_id(config))
    publish_snapshot(config, built.staging_root)
    _prepare_projection_and_stable(config, archived)

    plan = build_prune_plan(config, now=datetime(2026, 9, 20, tzinfo=UTC), preview_runner=_preview)
    assert not plan.entries
    assert plan.skipped["shared_with_sessions"] == 1
    assert archived.exists()


def test_prune_apply_refuses_source_changed_after_plan(tmp_path: Path) -> None:
    config_path, source, _ = _config(tmp_path)
    config = load_config(config_path)
    old = source / "archived_sessions" / "old.jsonl"
    _write_session(old, "session-old", "old")
    init_backend(config)
    built = build_snapshot(config, machine_id=_current_machine_id(config))
    publish_snapshot(config, built.staging_root)
    _prepare_projection_and_stable(config, old)
    plan = build_prune_plan(config, now=datetime(2026, 9, 20, tzinfo=UTC), preview_runner=_preview)

    _write_session(old, "session-old", "changed")
    with pytest.raises(ValueError, match="metadata changed|checksum changed"):
        apply_prune_plan(config, plan, preview_runner=_preview)
    assert old.exists()

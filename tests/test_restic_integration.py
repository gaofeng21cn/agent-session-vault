"""Real end-to-end integration tests invoking the actual ``restic`` binary."""
from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from agent_session_vault.archive_catalog import query_catalog
from agent_session_vault.archive_compact import compact_archive
from agent_session_vault.archive_ops import archive_backend, archive_cycle, build_snapshot, init_backend, publish_snapshot, verify_snapshot
from agent_session_vault.archive_prune import apply_prune_plan, build_prune_plan
from agent_session_vault.archive_restore import build_restore_plan, restore_plan
from agent_session_vault.archive_sources import scan_codex_sources
from agent_session_vault.config import load_config
from agent_session_vault.restic import restic_available
from agent_session_vault.stable import (
    default_stable_root,
    migration_plan_payload,
    mirror_stable_layer,
    restore_stable_layer,
)


pytestmark = [
    pytest.mark.restic_integration,
    pytest.mark.skipif(not restic_available(), reason="restic binary is unavailable on PATH"),
]


def _make_config(tmp_path: Path):
    home = tmp_path / "home"
    source = home / ".codex"
    (source / "sessions" / "2026" / "08" / "19").mkdir(parents=True)
    (source / "archived_sessions").mkdir(parents=True)
    imports = tmp_path / "imports"
    extras = tmp_path / "extras"
    (imports / "local-home" / ".raw" / "codex").mkdir(parents=True)
    (imports / "local-home" / ".raw" / "codex" / "proj.jsonl").write_text('{"tokens":42}\n', encoding="utf-8")
    (extras / "ns" / "codex").mkdir(parents=True)
    (extras / "ns" / "codex" / "extra.jsonl").write_text('{"tokens":7}\n', encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{tmp_path / 'workspace'}"
import_root = "{imports}"
projection_home = "{tmp_path / 'projection-home'}"
local_workspace_extras = "{extras}"
stable_root = "{tmp_path / 'stable'}"

[archive]
root = "{tmp_path / 'nas' / 'archive'}"
staging_root = "{tmp_path / 'staging'}"
machine_id_path = "{tmp_path / 'machine-id'}"
source_paths = ["{source}"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return load_config(config_path), source


def _write_session(path: Path, session_id: str, message: str) -> None:
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": session_id}})
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "timestamp": "2026-08-19T01:02:03Z",
                "payload": {"type": "user_message", "message": message},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _preview(_config) -> dict[str, object]:
    return {
        "package": "tokscale@4.18.0",
        "clients": ["codex"],
        "client_args": ["-c", "codex"],
        "statistics": {"total_tokens": 42},
    }


def test_real_restic_full_archive_dedup_restore_compact_and_prune(tmp_path: Path) -> None:
    config, source = _make_config(tmp_path)
    machine_id = scan_codex_sources(config).machine_id

    live_bracket = source / "sessions" / "2026" / "08" / "19" / "case [1].jsonl"
    archived_old = source / "archived_sessions" / "old.jsonl"
    _write_session(live_bracket, "session-bracket", "payload with brackets " * 20)
    _write_session(archived_old, "session-old", "historical cold conversation " * 20)

    init_payload = init_backend(config)
    assert init_payload["format"] == "agent-session-vault-restic-v2"
    assert init_payload["restic_repo_id"]

    # 1. First real archive cycle with deep restic check --read-data
    first = archive_cycle(config, machine_id=machine_id, due_only=False, deep=True)
    assert first.status == "verified"
    assert first.staging_cleanup == "removed"
    assert first.dedup is not None
    assert first.dedup["new_content_files"] == 2
    assert first.dedup["restic_data_blobs"] >= 2

    # 2. Repeat with renamed/moved file (same bytes) -> restic deduplicates blobs
    moved = source / "archived_sessions" / "case [1].jsonl"
    live_bracket.rename(moved)
    second = archive_cycle(config, machine_id=machine_id, due_only=True, deep=True)
    assert second.status == "verified"
    assert second.dedup is not None
    assert second.dedup["new_content_files"] == 0
    assert second.dedup["new_objects"] == 0
    assert second.dedup["reused_files"] == 2
    assert second.dedup["restic_data_blobs"] == 0

    # 3. Staging restore of special-character filename verified by SHA-256
    #    (catalog retains both the original sessions/ path and moved archived_sessions/ path)
    restore_dir = tmp_path / "staging-restore"
    rplan = build_restore_plan(config, destination=restore_dir, session_id="session-bracket")
    restored = restore_plan(config, rplan)
    assert restored["status"] == "verified"
    assert restored["restored_files"] == 2
    assert (restore_dir / ".codex" / "archived_sessions" / "case [1].jsonl").read_bytes() == moved.read_bytes()
    assert (restore_dir / ".codex" / "sessions" / "2026" / "08" / "19" / "case [1].jsonl").read_bytes() == moved.read_bytes()

    # 4. Content status & compaction (prune + forget --prune + check --read-data)
    status = archive_backend(config).content_status()
    assert status["status"] == "measured"
    assert status["restic_snapshot_count"] == 2
    assert status["restic_raw_data"]["total_size"] > 0

    compacted = compact_archive(config, keep_last=1, verify=True, machine_id=machine_id)
    assert compacted["status"] == "verified"
    assert compacted["repo_check"]["ok"] is True
    assert len(archive_backend(config).restic().snapshots()) == 1

    # 5. Full business prune gate with real restic archive + real restic stable mirror
    proj_dest = config.paths.import_root / "local-home" / ".raw" / "codex" / "archived_sessions" / "old.jsonl"
    proj_dest.parent.mkdir(parents=True, exist_ok=True)
    proj_dest.write_text('{"tokens":42}\n', encoding="utf-8")
    state_path = config.paths.import_root / "local-home" / ".local-home-projection-state.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "valid",
                "files": {
                    str(archived_old.resolve()): {"destination": str(proj_dest)},
                    str(moved.resolve()): {"destination": str(proj_dest)},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    stable_first = mirror_stable_layer(config)
    assert stable_first.status == "verified"
    assert migration_plan_payload(config)["readiness"]["analytics_restore_ready"] is True

    # Reuse check on unchanged stable layer
    stable_second = mirror_stable_layer(config)
    assert stable_second.status == "verified"
    assert {item.transfer_status for item in stable_second.items if item.source_exists} == {"reused_verified"}

    # Restore stable analytics round-trip via real restic
    stable_restore_dir = tmp_path / "stable-restore"
    stable_restored = restore_stable_layer(default_stable_root(config), stable_restore_dir)
    assert stable_restored["status"] == "verified"
    assert (stable_restore_dir / "tokscale" / "imports" / "local-home" / ".raw" / "codex" / "proj.jsonl").is_file()

    # Guarded local prune plan & apply
    pplan = build_prune_plan(config, now=datetime(2026, 9, 20, tzinfo=UTC), preview_runner=_preview)
    assert len(pplan.entries) == 2
    pruned = apply_prune_plan(config, pplan, preview_runner=_preview)
    assert pruned["status"] == "verified"
    assert pruned["deleted_file_count"] == 2
    assert not archived_old.exists()
    assert not moved.exists()
    assert len(query_catalog(config, session_id="session-old")) == 1


def test_real_restic_check_read_data_detects_pack_corruption(tmp_path: Path) -> None:
    config, source = _make_config(tmp_path)
    _write_session(source / "archived_sessions" / "old.jsonl", "session-old", "integrity guard " * 40)
    init_backend(config)
    built = build_snapshot(config, machine_id="machine-1")
    published = publish_snapshot(config, built.staging_root, verify_staged=True)
    snap_id = published[0].snapshot.snapshot_id
    assert verify_snapshot(config, snap_id, deep=True)["status"] == "verified"

    # Corrupt an actual encrypted restic pack file on disk and verify deep check catches it.
    pack_files = [path for path in (config.archive.root / "repo" / "data").rglob("*") if path.is_file()]
    assert pack_files
    pack_files[0].chmod(0o600)
    corrupted = bytearray(pack_files[0].read_bytes())
    corrupted[0] ^= 0xFF
    pack_files[0].write_bytes(bytes(corrupted))

    failed = verify_snapshot(config, snap_id, deep=True)
    assert failed["status"] == "failed"
    assert any(item.startswith("restic_check_errors:") for item in failed["failures"])

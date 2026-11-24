from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_session_vault.archive_backend import LEGACY_ARCHIVE_FORMAT, LegacyArchiveFormatError, ResticArchiveBackend
from agent_session_vault.archive_compact import compact_archive
from agent_session_vault.archive_ops import archive_backend, archive_cycle, init_backend, verify_snapshot
from agent_session_vault.cli import main
from agent_session_vault.config import load_config
from agent_session_vault.restic import ResticCheckResult


def _setup(tmp_path: Path):
    home = tmp_path / "home"
    source = home / ".codex"
    (source / "sessions" / "2026" / "08" / "19").mkdir(parents=True)
    (source / "archived_sessions").mkdir(parents=True)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{tmp_path / 'workspace'}"
import_root = "{tmp_path / 'imports'}"
projection_home = "{tmp_path / 'projection-home'}"
local_workspace_extras = "{tmp_path / 'extras'}"
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
    return config_path, load_config(config_path), source


def _write_session(path: Path, session_id: str, body: str) -> None:
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": session_id}})
        + "\n"
        + json.dumps({"type": "event_msg", "timestamp": "2026-08-19T01:02:03Z", "payload": {"type": "user_message", "message": body}})
        + "\n",
        encoding="utf-8",
    )


def test_compact_archive_prunes_verifies_and_writes_receipt(tmp_path: Path) -> None:
    _, config, source = _setup(tmp_path)
    _write_session(source / "archived_sessions" / "a.jsonl", "s-a", "alpha")
    init_backend(config)
    first = archive_cycle(config, machine_id="machine-1", due_only=False)
    assert first.status == "verified"

    _write_session(source / "archived_sessions" / "b.jsonl", "s-b", "beta")
    second = archive_cycle(config, machine_id="machine-1", due_only=False)
    assert second.status == "verified"

    dry = compact_archive(config, keep_last=1, dry_run=True, machine_id="machine-1")
    assert dry["status"] == "planned"
    assert dry["mode"] == "forget_prune"
    assert dry["dry_run"] is True
    assert dry["receipt_path"] is None
    assert len(archive_backend(config).restic().snapshots()) == 2

    applied = compact_archive(config, keep_last=1, dry_run=False, verify=True, machine_id="machine-1")
    assert applied["status"] == "verified"
    assert applied["mode"] == "forget_prune"
    assert applied["repo_check"]["ok"] is True
    assert Path(str(applied["receipt_path"])).is_file()
    assert len(archive_backend(config).restic().snapshots()) == 1
    assert verify_snapshot(config, second.snapshot_ids[0], deep=True)["status"] == "verified"


def test_compact_archive_without_retention_runs_prune_only(tmp_path: Path, capsys) -> None:
    config_path, config, source = _setup(tmp_path)
    _write_session(source / "archived_sessions" / "a.jsonl", "s-a", "alpha")
    init_backend(config)
    assert archive_cycle(config, machine_id="machine-1", due_only=False).status == "verified"

    exit_code = main(["--config", str(config_path), "archive", "compact", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "verified"
    assert payload["mode"] == "prune"
    assert payload["repo_check"]["ok"] is True


def test_compact_archive_rejects_maintenance_fence_and_check_failure(tmp_path: Path, monkeypatch) -> None:
    _, config, source = _setup(tmp_path)
    _write_session(source / "archived_sessions" / "a.jsonl", "s-a", "alpha")
    init_backend(config)
    assert archive_cycle(config, machine_id="machine-1", due_only=False).status == "verified"

    fence = archive_backend(config).maintenance_path
    fence.write_text('{"run_id": "existing"}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="maintenance in progress"):
        compact_archive(config)
    fence.unlink()

    repo = archive_backend(config).restic()
    monkeypatch.setattr(
        type(repo),
        "check",
        lambda self, read_data=False: ResticCheckResult(False, 2, ("pack-bad",), False, False, {"num_errors": 2}),
    )
    with pytest.raises(RuntimeError, match="restic check --read-data failed"):
        compact_archive(config, verify=True)


def test_legacy_v1_archive_is_detected_read_only_and_never_mutated(tmp_path: Path, capsys) -> None:
    config_path, config, source = _setup(tmp_path)
    legacy_root = config.archive.root
    (legacy_root / "objects").mkdir(parents=True)
    (legacy_root / "snapshots" / "machine-old" / "snap-1").mkdir(parents=True)
    (legacy_root / "snapshots" / "machine-old" / "snap-1" / "COMMITTED").write_text("2026-08-01T00:00:00Z\n")
    (legacy_root / "catalog" / "segments" / "machine-old").mkdir(parents=True)
    (legacy_root / "catalog" / "segments" / "machine-old" / "snap-1.json").write_text("{}\n")
    (legacy_root / "objects" / ("a" * 64 + ".tar.zst")).write_bytes(b"legacy-bytes")
    marker = legacy_root / "root-marker.json"
    marker.write_text(
        json.dumps({"format": LEGACY_ARCHIVE_FORMAT, "schema_version": 1, "vault_id": "vault-legacy", "created_at": "2026-08-01T00:00:00Z"}),
        encoding="utf-8",
    )
    before_files = sorted(str(p.relative_to(legacy_root)) for p in legacy_root.rglob("*"))

    exit_code = main(["--config", str(config_path), "archive", "legacy-status", "--json"])
    status = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert status["status"] == "legacy_v1_detected"
    assert status["read_only"] is True
    assert status["migration_required"] is True
    assert status["snapshot_count"] == 1
    assert status["object_count"] == 1
    assert status["object_bytes"] == len(b"legacy-bytes")
    assert status["catalog_segment_count"] == 1

    backend = ResticArchiveBackend(legacy_root, password_file=config.archive.password_file)
    with pytest.raises(LegacyArchiveFormatError, match="legacy archive-v1"):
        backend.initialize()
    with pytest.raises(LegacyArchiveFormatError, match="legacy archive-v1"):
        backend.ensure_ready()
    with pytest.raises(LegacyArchiveFormatError, match="legacy archive-v1"):
        compact_archive(config)

    after_files = sorted(str(p.relative_to(legacy_root)) for p in legacy_root.rglob("*"))
    assert after_files == before_files
    assert not (legacy_root / "repo").exists()


def test_content_status_cli_reports_restic_dedup_stats(tmp_path: Path, capsys) -> None:
    config_path, config, source = _setup(tmp_path)
    _write_session(source / "archived_sessions" / "a.jsonl", "s-a", "alpha")
    init_backend(config)
    assert archive_cycle(config, machine_id="machine-1", due_only=False).status == "verified"

    exit_code = main(["--config", str(config_path), "archive", "content-status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "measured"
    assert payload["vault_snapshot_count"] == 1
    assert payload["restic_snapshot_count"] == 1
    assert payload["unique_content_files"] == 1
    assert payload["restic_raw_data"]["total_size"] > 0

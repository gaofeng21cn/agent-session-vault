from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

from agent_session_vault import cold_archive as cold
from agent_session_vault import archive_maintenance as maintenance
from test_archive_maintenance import stage


@pytest.fixture
def cold_environment(environment, monkeypatch):
    config, run_dir = environment
    monkeypatch.setattr(cold, "session_gate", lambda *_: {})
    monkeypatch.setattr(cold, "preflight", lambda _: {"status": "ready"})
    monkeypatch.setattr(cold, "_references", lambda _: ([], "", []))
    now = datetime.now(UTC) + timedelta(days=60)
    root = config.paths.home / ".codex"
    batch = root / "automations/example/runs/completed-batch"
    batch.mkdir(parents=True)
    (batch / "receipt.json").write_text('{"status":"completed"}')
    (batch / "data.bin").write_bytes(b"archive content" * 200)
    return config, run_dir, root, batch, now


def planned(cold_environment):
    config, run_dir, root, batch, now = cold_environment
    inventory = cold.inventory(config, run_dir, now=now)
    stage(run_dir, "cold-inventory", inventory)
    decisions = run_dir / "decisions.json"
    maintenance.write_json(decisions, {"schema_version": "codex-cold-decisions.v1", "run_id": run_dir.name, "inventory_sha256": inventory["inventory_sha256"],
        "archive": [{"source": str(batch), "completion": "receipt confirms completed independent run",
                     "reference_review": "current pointers and work owners reviewed; no dependency",
                     "evidence_paths": [str(batch / "receipt.json")]}]})
    result = cold.build_plan(config, run_dir, decisions)
    stage(run_dir, "cold-plan", result)
    return result


def test_complete_batch_pack_restore_and_exact_trash_purge(cold_environment):
    config, run_dir, root, batch, _ = cold_environment
    # Logical content must be counted even when TAR represents it as a hardlink.
    os.link(batch / "data.bin", batch / "linked.bin")
    unrelated = config.paths.home / ".Trash/keep-user-file"
    unrelated.parent.mkdir()
    unrelated.write_text("user trash")
    result = planned(cold_environment)
    assert result["units"] == 1 and result["file_count"] == 3
    packed = cold.pack(config, run_dir)
    stage(run_dir, "cold-pack", packed)
    assert len(packed["packages"]) == 1
    assert packed["packages"][0]["logical_bytes"] == result["logical_bytes"]
    receipt = cold.cleanup(config, run_dir)
    assert receipt["status"] == "purged" and receipt["purged_files"] == 3
    assert receipt["purged_logical_bytes"] == result["logical_bytes"]
    assert not batch.exists() and not Path(receipt["trash"]).exists()
    assert unrelated.read_text() == "user trash"
    assert Path(packed["packages"][0]["path"]).is_file()


def test_changed_source_stops_pack_and_preserves_live_batch(cold_environment):
    config, run_dir, _, batch, _ = cold_environment
    planned(cold_environment)
    (batch / "data.bin").write_bytes(b"changed after review")
    with pytest.raises(ValueError, match="changed since plan"):
        cold.pack(config, run_dir)
    assert batch.exists() and not (run_dir / "cold-pack.receipt.json").exists()


def test_current_pointer_with_extensionless_id_retains_completed_record(cold_environment, monkeypatch):
    config, run_dir, _, batch, now = cold_environment
    measured = cold.inventory(config, run_dir, now=now)
    stage(run_dir, "cold-inventory", measured)
    decisions = run_dir / "decisions.json"
    maintenance.write_json(decisions, {"schema_version": "codex-cold-decisions.v1", "run_id": run_dir.name, "inventory_sha256": measured["inventory_sha256"],
        "archive": [{"source": str(batch), "completion": "completed", "reference_review": "reviewed",
                     "evidence_paths": [str(batch / "receipt.json")]}]})
    monkeypatch.setattr(cold, "_references", lambda _: ([("state/current.json", '{"batch":"completed-batch"}')], "", []))
    result = cold.build_plan(config, run_dir, decisions)
    assert result["units"] == 0
    assert "current_control_reference" in maintenance.read_json(run_dir / "cold-plan.json")["retained"][0]["reason"]
    row = {"source": str(batch.with_name("episode-123.json")), "category": "automations"}
    assert cold._reference_hit(row, config.paths.home / ".codex", ([("state", "episode-123")], "", []))


def test_reference_added_after_pack_prevents_any_trash_move(cold_environment, monkeypatch):
    config, run_dir, _, batch, _ = cold_environment
    planned(cold_environment)
    stage(run_dir, "cold-pack", cold.pack(config, run_dir))
    monkeypatch.setattr(cold, "_references", lambda _: ([("current", str(batch))], "", []))
    with pytest.raises(ValueError, match="current reference"):
        cold.cleanup(config, run_dir)
    assert batch.exists()
    assert not (config.paths.home / ".Trash" / ("codex-cold-" + run_dir.name)).exists()


def test_tampered_package_prevents_trash_move(cold_environment):
    config, run_dir, _, batch, _ = cold_environment
    planned(cold_environment)
    packed = cold.pack(config, run_dir)
    stage(run_dir, "cold-pack", packed)
    with Path(packed["packages"][0]["path"]).open("ab") as output:
        output.write(b"tampered")
    with pytest.raises(ValueError, match="NAS package changed"):
        cold.cleanup(config, run_dir)
    assert batch.exists()


def test_symlink_and_live_roots_cannot_be_cleanup_candidates(cold_environment):
    _, _, root, batch, _ = cold_environment
    outside = root.parent / "external"
    outside.mkdir()
    (batch / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        cold.collect(root, batch, hashes=True)
    for relative in ("sessions/old", "thread_history_1.sqlite", "automations/example/state/current.json", "plugins/cache/plugin"):
        with pytest.raises(ValueError):
            cold._category(root, root / relative)


def test_unexpected_trash_file_or_directory_stops_exact_purge(tmp_path):
    trash = tmp_path / "dedicated"
    trash.mkdir()
    (trash / "file").write_bytes(b"kept")
    (trash / "unexpected-empty-directory").mkdir()
    expected = {"file": (4, hashlib.sha256(b"kept").hexdigest())}
    with pytest.raises(ValueError, match="differ"):
        cold.purge_exact(trash, expected, set(), io.StringIO())
    assert (trash / "file").read_bytes() == b"kept"


@pytest.mark.parametrize("kind", ["symlink", "escape", "duplicate"])
def test_archive_structural_attack_is_rejected(tmp_path, kind):
    plain, compressed = tmp_path / "bad.tar", tmp_path / "bad.tar.zst"
    with tarfile.open(plain, "w") as archive:
        info = tarfile.TarInfo("../escape" if kind == "escape" else "file")
        if kind == "symlink":
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        else:
            info.size = 1
        archive.addfile(info, None if kind == "symlink" else io.BytesIO(b"x"))
        if kind == "duplicate":
            archive.addfile(info, io.BytesIO(b"x"))
    with compressed.open("wb") as output:
        subprocess.run(["zstd", "-cq", str(plain)], stdout=output, check=True)
    with pytest.raises(ValueError):
        cold.inspect_package(compressed, [])


def test_asset_scan_error_keeps_assets(cold_environment, monkeypatch):
    config, _, root, _, _ = cold_environment
    (root / "sessions").mkdir()
    config.paths.workspace_root.mkdir()
    unit = {"source": str(root / "generated_images/old-session"), "category": "generated_images"}
    monkeypatch.setattr(cold.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 2, "", "unreadable input"))
    assert cold._asset_references(config, [unit]) == {unit["source"]}


def test_packages_keep_current_and_two_recent_versions(cold_environment):
    config, _, root, _, _ = cold_environment
    releases = root / "packages/standalone/releases"
    for version in ("0.167.1", "0.167.0", "0.165.0", "0.164.0"):
        (releases / (version + "-aarch64-apple-darwin") / "bin").mkdir(parents=True)
    current = releases / "0.167.1-aarch64-apple-darwin"
    (current / "bin/codex").write_text("binary")
    (releases.parent / "current").symlink_to(current, target_is_directory=True)
    cli = config.paths.home / ".local/bin/codex"
    cli.parent.mkdir(parents=True)
    cli.symlink_to(current / "bin/codex")
    assert cold._protected_versions(config) == {"0.167.1-aarch64-apple-darwin", "0.167.0-aarch64-apple-darwin", "0.165.0-aarch64-apple-darwin"}


def test_empty_review_produces_no_empty_package_or_trash(cold_environment):
    config, run_dir, _, _, now = cold_environment
    measured = cold.inventory(config, run_dir, now=now)
    stage(run_dir, "cold-inventory", measured)
    decisions = run_dir / "decisions.json"
    maintenance.write_json(decisions, {"schema_version": "codex-cold-decisions.v1", "run_id": run_dir.name,
                                      "inventory_sha256": measured["inventory_sha256"], "archive": []})
    stage(run_dir, "cold-plan", cold.build_plan(config, run_dir, decisions))
    packed = cold.pack(config, run_dir)
    assert packed["packages"] == [] and not Path(packed["restore_root"]).exists()
    stage(run_dir, "cold-pack", packed)
    result = cold.cleanup(config, run_dir)
    assert result["status"] == "no_candidates" and result["purged_files"] == 0
    assert not Path(result["trash"]).exists()


def test_missing_inventory_exit_code_blocks_plan(cold_environment):
    config, run_dir, _, _, now = cold_environment
    cold.inventory(config, run_dir, now=now)
    with pytest.raises(ValueError, match="real exit code"):
        cold.build_plan(config, run_dir, run_dir / "decisions.json")

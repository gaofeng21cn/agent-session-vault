from dataclasses import replace
import io
import json
import os
from pathlib import Path
import random
import struct
import subprocess
import tarfile

import pytest

from agent_session_vault import archive_compact as compact
from agent_session_vault.archive import pack_paths, sha256_file
from agent_session_vault.archive_backend import FilesystemArchiveBackend
from agent_session_vault.archive_catalog import write_catalog_segment
from agent_session_vault.archive_content import ArchiveContentIndex
from agent_session_vault.archive_model import ArchiveBundle
from agent_session_vault.archive_ops import build_snapshot, init_backend, publish_snapshot, archive_cycle
from agent_session_vault.archive_prune import build_prune_plan, apply_prune_plan
from agent_session_vault.archive_restore import build_restore_plan, restore_plan
from agent_session_vault.config import load_config
from test_archive_vault import _config, _write_session


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(compact, "preflight", lambda config: {"status": "ready"})
    path, source, root = _config(tmp_path)
    config = load_config(path)
    one = source / "archived_sessions/one.jsonl"
    two = source / "archived_sessions/two.jsonl"
    _write_session(one, "session-one", random.Random(1).randbytes(256 * 1024).hex())
    # Exercise a real bsdtar hardlink, not just two equal independent files.
    os.link(one, two)
    init_backend(config)
    built = build_snapshot(config, machine_id="machine-test")
    backend = FilesystemArchiveBackend(root)
    old_pack = tmp_path / "old.tar.zst"
    pack_paths(source, ["archived_sessions/one.jsonl", "archived_sessions/two.jsonl"], old_pack)
    digest = sha256_file(old_pack)
    bundle = ArchiveBundle(f"bundle-{digest}", f"objects/{digest}.tar.zst", digest,
                           old_pack.stat().st_size, one.stat().st_size * 2, 2)
    files = tuple(replace(record, bundle_id=bundle.bundle_id, member=Path(record.path).relative_to(".codex").as_posix())
                  for record in built.manifests[0].files)
    manifest = replace(built.manifests[0], files=files, bundles=(bundle,))
    published = backend.publish_snapshot(built.snapshots[0], (manifest,), {bundle.bundle_id: old_pack})
    write_catalog_segment(backend, published)
    # Add the exact old bytes under a new bundle, simulating old path-based incrementals.
    old = built.snapshots[0]
    old_pack2 = tmp_path / "old2.tar.zst"
    pack_paths(source, ["archived_sessions/two.jsonl"], old_pack2)
    digest2 = sha256_file(old_pack2)
    bundle2 = replace(bundle, bundle_id=f"bundle-{digest2}", sha256=digest2,
                      object_path=f"objects/{digest2}.tar.zst", bytes=old_pack2.stat().st_size,
                      file_count=1, source_bytes=one.stat().st_size)
    second_manifest = replace(manifest, snapshot_id=old.snapshot_id + "-second",
                              bundles=(bundle2,), files=(replace(files[1], bundle_id=bundle2.bundle_id),))
    second = replace(old, snapshot_id=second_manifest.snapshot_id, parent_snapshot_id=old.snapshot_id,
                     captured_at="2026-08-20T00:00:00Z")
    write_catalog_segment(backend, backend.publish_snapshot(second, (second_manifest,), {bundle2.bundle_id: old_pack2}))
    # A changed revision must remain a distinct original byte representation.
    two.unlink()
    _write_session(one, "session-one", random.Random(2).randbytes(256 * 1024).hex())
    current = build_snapshot(config, machine_id="machine-test")
    publish_snapshot(config, current.staging_root)
    run = compact.local_root(config) / "ops/archive-compaction/test-migration"
    run.mkdir(parents=True)
    return config, root, run


def test_compaction_preserves_all_versions_paths_and_old_restore_plan(legacy, monkeypatch):
    config, root, run = legacy
    monkeypatch.setattr(compact, "CHUNK_BYTES", 512 * 1024)
    backend = FilesystemArchiveBackend(root)
    before = compact._logical_digest(backend)
    original_marker = backend.ensure_ready()
    old_restore = build_restore_plan(config, destination=run / "restore-original-plan")
    old_contents = ArchiveContentIndex.load(backend).report()
    assert old_contents["cross_object_duplicate_contents"] == 1
    compact.compact_plan(config, run)
    result = compact.compact_apply(config, run / "plan.json")
    assert result["status"] == "verified"
    assert compact._logical_digest(backend) == before
    assert backend.ensure_ready()["vault_id"] == original_marker["vault_id"]
    assert backend.ensure_ready()["storage_generation"] == run.name
    report = ArchiveContentIndex.load(backend).report()
    assert report["unique_file_contents"] == 2
    assert report["stored_member_locations"] == 2
    assert report["published_objects"] == 2
    assert report["cross_object_duplicate_contents"] == 0
    assert not backend.maintenance_path.exists()
    work = Path(result["receipt_path"]).parent
    assert not (work / "retired").exists()
    assert not (work / "candidate").exists()
    assert sha256_file(Path(result["metadata_backup"]["path"])) == result["metadata_backup"]["sha256"]
    assert restore_plan(config, old_restore)["status"] == "verified"
    assert compact.compact_status(config, run / "plan.json")["status"] == "verified"
    for directory in backend.iter_snapshot_dirs():
        assert backend.verify_snapshot(directory, deep=True)["status"] == "verified"
    with pytest.raises(ValueError, match="already has evidence"):
        compact.compact_apply(config, run / "plan.json")


def test_plan_drift_never_starts_migration(legacy):
    config, root, run = legacy
    compact.compact_plan(config, run)
    (root / "receipts/changed").write_text("different")
    with pytest.raises(ValueError, match="changed since plan"):
        compact.compact_apply(config, run / "plan.json")
    assert not FilesystemArchiveBackend(root).maintenance_path.exists()


def test_corrupt_source_retains_source_candidate_and_persistent_fence(legacy):
    config, root, run = legacy
    compact.compact_plan(config, run)
    original = compact._repack

    def corrupt(backend, index, writer, progress):
        path = next((backend.root / "objects").glob("*.zst"))
        path.write_bytes(path.read_bytes() + b"corruption")
        return original(backend, index, writer, progress)

    from unittest.mock import patch
    with patch.object(compact, "_repack", corrupt), pytest.raises(ValueError, match="SHA-256 mismatch"):
        compact.compact_apply(config, run / "plan.json")
    backend = FilesystemArchiveBackend(root)
    assert backend.maintenance_path.exists()
    assert root.exists()
    status = compact.compact_status(config, run / "plan.json")
    assert status["progress"]["phase"] == "stopped" and status["candidate_exists"]
    assert archive_cycle(config).status == "busy"
    for operation in (lambda: init_backend(config), lambda: build_snapshot(config),
                      lambda: build_prune_plan(config), lambda: apply_prune_plan(config, None),
                      lambda: restore_plan(config, None)):
        with pytest.raises(RuntimeError, match="maintenance in progress"):
            operation()


def test_failed_switch_rolls_back_once_and_keeps_evidence(legacy, monkeypatch):
    config, root, run = legacy
    compact.compact_plan(config, run)
    original = Path.rename

    def fail_candidate(self, target):
        if self.name == "candidate" and target == root:
            raise OSError("simulated cutover failure")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", fail_candidate)
    with pytest.raises(OSError, match="cutover failure"):
        compact.compact_apply(config, run / "plan.json")
    assert root.is_dir()
    assert "storage_generation" not in FilesystemArchiveBackend(root).ensure_ready()
    status = compact.compact_status(config, run / "plan.json")
    assert status["candidate_exists"] and not status["retired_exists"]
    assert status["maintenance_fence"]


def test_unknown_original_material_is_not_dropped(legacy):
    config, root, run = legacy
    (root / "unknown-record").write_text("preserve")
    with pytest.raises(ValueError, match="unknown archive root content"):
        compact.compact_plan(config, run)


def test_plan_digest_tampering_is_rejected(legacy):
    config, _, run = legacy
    compact.compact_plan(config, run)
    plan = json.loads((run / "plan.json").read_text())
    plan["chunk_logical_bytes"] = 1
    (run / "plan.json").write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="digest/schema"):
        compact.compact_apply(config, run / "plan.json")


def test_candidate_corruption_never_switches_or_deletes_source(legacy, monkeypatch):
    config, root, run = legacy
    compact.compact_plan(config, run)
    original = compact._verify_candidate

    def corrupt(candidate, plan, progress):
        path = next((candidate.root / "objects").glob("*.zst"))
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        return original(candidate, plan, progress)

    monkeypatch.setattr(compact, "_verify_candidate", corrupt)
    with pytest.raises(ValueError, match="object/member mismatch"):
        compact.compact_apply(config, run / "plan.json")
    assert "storage_generation" not in FilesystemArchiveBackend(root).ensure_ready()
    assert len(list((root / "objects").glob("*.zst"))) == 3


def test_retired_drift_stops_before_deletion_and_keeps_new_live_library(legacy, monkeypatch):
    config, root, run = legacy
    compact.compact_plan(config, run)
    original = compact._retire_exact

    def drift(retired, inventory, progress):
        (retired / "unexpected").write_text("retain")
        return original(retired, inventory, progress)

    monkeypatch.setattr(compact, "_retire_exact", drift)
    with pytest.raises(ValueError, match="changed since plan"):
        compact.compact_apply(config, run / "plan.json")
    assert FilesystemArchiveBackend(root).ensure_ready()["storage_generation"] == run.name
    status = compact.compact_status(config, run / "plan.json")
    assert status["retired_exists"] and status["maintenance_fence"]
    assert status["status"] == "stopped"


def test_real_legacy_appledouble_is_excluded_but_original_bytes_stay(legacy):
    config, root, run = legacy
    backend = FilesystemArchiveBackend(root)
    old = backend.load_verified_metadata(backend.iter_snapshot_dirs()[0])
    manifest = old.manifests[0]
    bundle = manifest.bundles[0]
    raw = subprocess.check_output(["zstd", "-dc", "--", str(root / bundle.object_path)])
    body = (struct.pack(">II", 0x00051607, 0x00020000) + b"Mac OS X        " + struct.pack(">H", 2)
            + struct.pack(">III", 9, 50, 113) + struct.pack(">III", 2, 163, 0) + b"\0" * 113)
    member = manifest.files[0].member
    sidecar = str(Path(member).with_name("._" + Path(member).name))
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as target, tarfile.open(fileobj=io.BytesIO(raw)) as source:
        for entry in source:
            target.addfile(entry, source.extractfile(entry) if entry.isreg() else None)
        entry = tarfile.TarInfo(sidecar)
        entry.size = len(body)
        target.addfile(entry, io.BytesIO(body))
    package = run / "legacy-with-appledouble.tar.zst"
    with package.open("wb") as handle:
        subprocess.run(["zstd", "-q", "-c"], input=output.getvalue(), stdout=handle, check=True)
    digest = sha256_file(package)
    replacement = replace(bundle, bundle_id=f"bundle-{digest}", sha256=digest,
                          object_path=f"objects/{digest}.tar.zst", bytes=package.stat().st_size)
    extra = replace(manifest, snapshot_id=manifest.snapshot_id + "-noise", bundles=(replacement,),
                    files=tuple(replace(record, bundle_id=replacement.bundle_id) for record in manifest.files))
    snapshot = replace(old.snapshot, snapshot_id=extra.snapshot_id)
    write_catalog_segment(backend, backend.publish_snapshot(snapshot, (extra,), {replacement.bundle_id: package}))
    before = compact._logical_digest(backend)
    compact.compact_plan(config, run)
    result = compact.compact_apply(config, run / "plan.json")
    assert result["excluded_appledouble_members"] == 1
    assert result["excluded_appledouble_logical_bytes"] == 163
    assert compact._logical_digest(backend) == before
    assert compact._appledouble_counterpart(sidecar, body) == member
    assert compact._appledouble_counterpart(sidecar, b"real user file") is None
    with_resource = bytearray(body)
    with_resource[46:50] = struct.pack(">I", 1)
    assert compact._appledouble_counterpart(sidecar, with_resource) is None


def _stage_evidence(run, name, code, payload):
    (run / f"{name}.launch.json").write_text(json.dumps({"command": []}))
    (run / f"{name}.exit_code").write_text(str(code) + "\n")
    (run / f"{name}.stdout.json").write_text(json.dumps(payload))


def test_known_failed_build_recovers_verified_packs_without_reissuing_apply(legacy, monkeypatch):
    config, root, run = legacy
    compact.compact_plan(config, run)
    original = compact._repack
    error = "unindexed source content requires reconciliation: legacy.tar.zst"

    def stop_after_build(backend, index, writer, progress):
        original(backend, index, writer, progress)
        (writer.candidate / "objects/.pack-99.pending").write_bytes(b"incomplete derived staging")
        raise ValueError(error)

    monkeypatch.setattr(compact, "_repack", stop_after_build)
    with pytest.raises(ValueError, match="unindexed source"):
        compact.compact_apply(config, run / "plan.json")
    _stage_evidence(run, "compact-apply", 2, {"status": "failed", "error": "ValueError: " + error})
    recovery = compact.compact_recovery_plan(config, run / "plan.json")
    assert recovery["reusable_contents"] == 2
    _stage_evidence(run, "compact-recovery-plan", 0, recovery)
    monkeypatch.setattr(compact, "_repack", original)
    result = compact.compact_apply(config, run / "plan.json", recovery_path=run / "recovery-plan.json")
    assert result["status"] == "verified" and result["reused_candidate_objects"] > 0
    assert result["removed_failed_fragment_bytes"] == len(b"incomplete derived staging")
    assert not FilesystemArchiveBackend(root).maintenance_path.exists()
    with pytest.raises((ValueError, OSError)):
        compact.compact_apply(config, run / "plan.json", recovery_path=run / "recovery-plan.json")


def test_unknown_original_exit_cannot_authorize_recovery(legacy):
    config, _, run = legacy
    compact.compact_plan(config, run)
    with pytest.raises(ValueError, match="original known"):
        compact.compact_recovery_plan(config, run / "plan.json")


@pytest.mark.skipif(os.name != "posix" or os.uname().sysname != "Darwin", reason="macOS extended attributes")
def test_explicit_file_pack_excludes_real_xattrs_and_implicit_children(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    file = source / "one.jsonl"
    file.write_bytes(b"actual conversation bytes")
    subprocess.run(["/usr/bin/xattr", "-w", "com.example.agent-session-vault-test", "metadata noise", str(file)], check=True)
    directory = source / "directory"
    directory.mkdir()
    (directory / "not-selected.jsonl").write_bytes(b"do not implicitly include")
    package = tmp_path / "selection.tar.zst"
    pack_paths(source, [file.name, directory.name], package)
    raw = subprocess.check_output(["zstd", "-dc", "--", str(package)])
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        regulars = {entry.name for entry in archive if entry.isreg()}
    assert regulars == {file.name}

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil

import pytest

from agent_session_vault import archive_ops
from agent_session_vault.archive_backend import FilesystemArchiveBackend
from agent_session_vault.archive_content import ArchiveContentIndex
from agent_session_vault.archive_ops import archive_cycle, build_snapshot, init_backend, publish_snapshot, verify_snapshot
from agent_session_vault.archive_restore import build_restore_plan, restore_plan
from agent_session_vault.archive import pack_paths, restore_bundle_member, sha256_file
from agent_session_vault.archive_sources import scan_codex_sources
from agent_session_vault.config import load_config
from test_archive_vault import _config, _write_session


def _publish(config):
    built = build_snapshot(config, machine_id="machine-test")
    publish_snapshot(config, built.staging_root, verify_staged=True)
    for snapshot in built.snapshots:
        assert verify_snapshot(config, snapshot.snapshot_id, deep=True)["status"] == "verified"
    return built


def test_rename_and_repeat_reuse_old_member_without_new_objects(tmp_path):
    path, source, nas = _config(tmp_path)
    config = load_config(path)
    live = source / "sessions/2026/08/19/one.jsonl"
    _write_session(live, "session-one", "same bytes")
    expected = live.read_bytes()
    init_backend(config)
    first = _publish(config)
    before = sorted(p.name for p in (nas / "objects").iterdir())
    live.rename(source / "archived_sessions/one.jsonl")
    second = _publish(config)
    third = _publish(config)
    for built in (second, third):
        assert built.staged_objects == {}
        assert built.dedup["new_content_bytes"] == 0
        assert built.dedup["new_archive_bytes"] == 0
        assert built.dedup["historical_reused_files"] == 1
        record = built.manifests[0].files[0]
        assert record.path == ".codex/archived_sessions/one.jsonl"
        assert record.member == "sessions/2026/08/19/one.jsonl"
        assert record.bundle_id == first.manifests[0].files[0].bundle_id
    assert sorted(p.name for p in (nas / "objects").iterdir()) == before
    plan = build_restore_plan(config, destination=tmp_path / "restored", session_id="session-one")
    result = restore_plan(config, plan)
    assert result["restored_files"] == 2
    assert (tmp_path / "restored/.codex/archived_sessions/one.jsonl").read_bytes() == expected
    assert (tmp_path / "restored/.codex/sessions/2026/08/19/one.jsonl").read_bytes() == expected


def test_equal_independent_files_share_one_member_and_restore_both_paths(tmp_path):
    path, source, _ = _config(tmp_path)
    config = load_config(path)
    one = source / "archived_sessions/one.jsonl"
    two = source / "archived_sessions/copy.jsonl"
    _write_session(one, "session-one", "identical copy")
    shutil.copyfile(one, two)
    assert one.stat().st_ino != two.stat().st_ino
    init_backend(config)
    built = _publish(config)
    assert built.dedup["files_seen"] == 2
    assert built.dedup["new_content_files"] == 1
    assert built.dedup["same_cycle_reused_files"] == 1
    assert len({(r.bundle_id, r.member) for r in built.manifests[0].files}) == 1
    verified = verify_snapshot(config, built.snapshots[0].snapshot_id, deep=True)
    assert verified["checked_files"] == 2 and verified["checked_members"] == 1
    result = restore_plan(config, build_restore_plan(config, destination=tmp_path / "restore"))
    assert result["restored_files"] == 2
    assert (tmp_path / "restore/.codex/archived_sessions/copy.jsonl").read_bytes() == one.read_bytes()


def test_old_content_reversion_reuses_history_and_omits_unused_bundles(tmp_path):
    path, source, nas = _config(tmp_path)
    config = load_config(path)
    session = source / "archived_sessions/one.jsonl"
    _write_session(session, "session-one", "version one")
    original = session.read_bytes()
    init_backend(config)
    first = _publish(config)
    _write_session(session, "session-one", "version two")
    second = _publish(config)
    assert len(second.manifests[0].bundles) == 1
    assert first.manifests[0].bundles[0].bundle_id != second.manifests[0].bundles[0].bundle_id
    session.write_bytes(original)
    third = _publish(config)
    assert not third.staged_objects and third.dedup["new_content_files"] == 0
    assert third.manifests[0].files[0].bundle_id == first.manifests[0].files[0].bundle_id
    assert len(third.manifests[0].bundles) == 1
    assert len(list((nas / "objects").glob("*.tar.zst"))) == 2
    for built in (first, second, third):
        assert verify_snapshot(config, built.snapshots[0].snapshot_id, deep=True)["status"] == "verified"


def test_same_session_id_with_different_contents_preserves_both(tmp_path):
    path, source, _ = _config(tmp_path)
    config = load_config(path)
    _write_session(source / "archived_sessions/one.jsonl", "same-id", "first version")
    _write_session(source / "archived_sessions/two.jsonl", "same-id", "different branch")
    init_backend(config)
    built = _publish(config)
    assert built.dedup["new_content_files"] == 2
    assert len({record.sha256 for record in built.manifests[0].files}) == 2


def test_cross_source_reuse_publishes_all_objects_before_any_snapshot(tmp_path, monkeypatch):
    path, _, _ = _config(tmp_path)
    config = load_config(path)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "first.bin").write_bytes(b"same full-fidelity payload")
    (two / "second.bin").write_bytes((one / "first.bin").read_bytes())
    from agent_session_vault.config import ArchiveSourceConfig
    configs = (ArchiveSourceConfig(str(one), "file_tree", "one"), ArchiveSourceConfig(str(two), "file_tree", "two"))
    config = replace(config, archive=replace(config.archive, source_paths=configs))
    scan = scan_codex_sources(config, machine_id="machine-test")
    ids = {item.source.root_path: item.source.source_id for item in scan.sources}
    # The first packed source sorts last at publish time. Its alias must not
    # commit a snapshot before that shared new object exists on the backend.
    configs = tuple(sorted(configs, key=lambda item: ids[item.path], reverse=True))
    config = replace(config, archive=replace(config.archive, source_paths=configs))
    init_backend(config)
    real = FilesystemArchiveBackend.publish_snapshot
    calls = []
    def guarded(backend, snapshot, manifests, objects):
        calls.append(snapshot.source.source_id)
        assert all((backend.root / bundle.object_path).is_file() for m in manifests for bundle in m.bundles)
        return real(backend, snapshot, manifests, objects)
    monkeypatch.setattr(FilesystemArchiveBackend, "publish_snapshot", guarded)
    built = _publish(config)
    assert len(calls) == 2 and len(built.staged_objects) == 1
    assert built.dedup["same_cycle_reused_files"] == 1
    assert built.dedup["new_content_files"] == 1
    restored = restore_plan(config, build_restore_plan(config, destination=tmp_path / "restored", client="files"))
    assert restored["restored_files"] == 2


def test_bad_historical_metadata_or_missing_object_blocks_content_reuse(tmp_path):
    path, source, nas = _config(tmp_path)
    config = load_config(path)
    _write_session(source / "archived_sessions/one.jsonl", "session-one", "one")
    init_backend(config)
    built = _publish(config)
    manifest_path = next((nas / "snapshots").rglob("manifest.json"))
    original = manifest_path.read_bytes()
    manifest_path.write_bytes(original.replace(b'session-one', b'session-bad'))
    with pytest.raises(RuntimeError, match="metadata failed"):
        build_snapshot(config, machine_id="machine-test")
    manifest_path.write_bytes(original)
    bundle = built.manifests[0].bundles[0]
    (nas / bundle.object_path).unlink()
    with pytest.raises(RuntimeError, match="object missing or changed"):
        build_snapshot(config, machine_id="machine-test")


def test_uncommitted_staging_and_catalog_are_not_content_authority(tmp_path):
    path, source, nas = _config(tmp_path)
    config = load_config(path)
    _write_session(source / "archived_sessions/one.jsonl", "session-one", "one")
    init_backend(config)
    staged = build_snapshot(config, machine_id="machine-test")
    assert staged.staged_objects
    fake = nas / "catalog/segments/forged.json"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text(json.dumps({"entries": [{"sha256": staged.manifests[0].files[0].sha256}]}))
    assert ArchiveContentIndex.load(FilesystemArchiveBackend(nas)).report()["unique_file_contents"] == 0
    again = build_snapshot(config, machine_id="machine-test")
    assert again.dedup["new_content_files"] == 1


def test_source_change_cannot_turn_alias_into_another_files_bytes(tmp_path, monkeypatch):
    path, source, nas = _config(tmp_path)
    config = load_config(path)
    one, two = source / "archived_sessions/a.jsonl", source / "archived_sessions/b.jsonl"
    _write_session(one, "session-one", "original")
    shutil.copyfile(one, two)
    init_backend(config)
    real = archive_ops.pack_paths
    def mutate(root, paths, bundle):
        with one.open("ab") as output:
            output.write(b'\n{"new":"write"}\n')
        real(root, paths, bundle)
    monkeypatch.setattr(archive_ops, "pack_paths", mutate)
    result = archive_cycle(config, machine_id="machine-test", due_only=False)
    assert result.status == "failed" and "shared source content changed" in result.error
    assert not list((nas / "objects").glob("*.tar.zst"))
    assert not list((nas / "snapshots").rglob("COMMITTED"))
    assert b'new' not in two.read_bytes()


def test_cycle_receipt_reports_unique_growth(tmp_path):
    path, source, _ = _config(tmp_path)
    config = load_config(path)
    _write_session(source / "archived_sessions/a.jsonl", "session-one", "original")
    init_backend(config)
    first = archive_cycle(config, machine_id="machine-test", due_only=False)
    second = archive_cycle(config, machine_id="machine-test", due_only=False)
    assert first.status == second.status == "verified"
    assert first.dedup["new_content_files"] == 1
    assert second.dedup["new_archive_bytes"] == 0
    assert json.loads(Path(second.receipt_path).read_text())["details"]["dedup"] == second.dedup


def test_legacy_hardlink_member_remains_restorable_after_content_reuse(tmp_path):
    import os
    from agent_session_vault.archive_model import ArchiveBundle
    from agent_session_vault.archive_catalog import write_catalog_segment
    path, source, nas = _config(tmp_path)
    config = load_config(path)
    live = source / "archived_sessions/new.jsonl"
    _write_session(live, "session-one", "legacy hardlink contents")
    payload = live.read_bytes()
    init_backend(config)
    draft = build_snapshot(config, machine_id="machine-test")
    legacy_source = tmp_path / "legacy"
    legacy_source.mkdir()
    (legacy_source / "z-original").write_bytes(payload)
    os.link(legacy_source / "z-original", legacy_source / "a-alias")
    package = tmp_path / "legacy.tar.zst"
    pack_paths(legacy_source, ["z-original", "a-alias"], package)
    digest = sha256_file(package)
    bundle = ArchiveBundle(f"bundle-{digest}", f"objects/{digest}.tar.zst", digest, package.stat().st_size, len(payload)*2, 2)
    original = draft.manifests[0].files[0]
    records = tuple(replace(original, path=f".codex/archived_sessions/{name}.jsonl", member=name, bundle_id=bundle.bundle_id)
                    for name in ("z-original", "a-alias"))
    manifest = replace(draft.manifests[0], files=records, bundles=(bundle,))
    backend = FilesystemArchiveBackend(nas)
    published = backend.publish_snapshot(draft.snapshots[0], (manifest,), {bundle.bundle_id: package})
    write_catalog_segment(backend, published)
    # A lexically preferred legacy alias may refer backwards to its data.
    assert restore_bundle_member(nas / bundle.object_path, "a-alias", tmp_path / "probe")[0] == len(payload)
    assert (tmp_path / "probe").read_bytes() == payload
    built = _publish(config)
    assert not built.staged_objects
    assert built.manifests[0].files[0].member == "a-alias"
    restore_plan(config, build_restore_plan(config, destination=tmp_path / "restored", session_id="session-one"))
    assert (tmp_path / "restored/.codex/archived_sessions/new.jsonl").read_bytes() == payload

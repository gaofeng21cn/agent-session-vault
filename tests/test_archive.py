from pathlib import Path

from agent_session_vault.archive import inventory_bundles, offload_tree, pack_tree, restore_bundle


def test_pack_and_restore_bundle_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "b.txt").write_text("beta", encoding="utf-8")

    output_dir = tmp_path / "bundles"
    bundle = pack_tree(source=source, output_dir=output_dir, bundle_name="sample")

    assert bundle.bundle_path.exists()
    assert bundle.manifest_path.exists()

    restore_dir = tmp_path / "restore"
    restore_bundle(bundle.bundle_path, restore_dir)

    assert (restore_dir / "a.txt").read_text(encoding="utf-8") == "alpha"
    assert (restore_dir / "nested" / "b.txt").read_text(encoding="utf-8") == "beta"


def test_offload_tree_can_remove_source_and_inventory_sees_bundle(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")

    archive_root = tmp_path / "archive"
    result = offload_tree(source=source, archive_root=archive_root, bundle_name="offload-demo", remove_source=True)

    assert result.bundle_path.exists()
    assert not source.exists()

    inventory = inventory_bundles(archive_root)
    bundle_paths = {item.bundle_path for item in inventory}
    assert result.bundle_path in bundle_paths

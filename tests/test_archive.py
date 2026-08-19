from pathlib import Path

from agent_session_vault.archive import pack_paths, restore_bundle


def test_pack_paths_round_trip_preserves_selected_relative_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "b.txt").write_text("beta", encoding="utf-8")
    (source / "ignored.txt").write_text("ignored", encoding="utf-8")

    bundle_path = tmp_path / "bundle.tar.zst"
    pack_paths(source, ["a.txt", "nested/b.txt"], bundle_path)
    restore_dir = tmp_path / "restore"
    restore_bundle(bundle_path, restore_dir)

    assert (restore_dir / "a.txt").read_text(encoding="utf-8") == "alpha"
    assert (restore_dir / "nested" / "b.txt").read_text(encoding="utf-8") == "beta"
    assert not (restore_dir / "ignored.txt").exists()

from pathlib import Path
import subprocess
import tarfile

import pytest

from agent_session_vault.archive import restore_bundle_member, sha256_file


def test_sha256_file_returns_hex_digest(tmp_path: Path) -> None:
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"hello restic vault\n")
    digest = sha256_file(sample)
    assert len(digest) == 64
    assert digest == sha256_file(sample)


@pytest.mark.parametrize("target", ["./alias", "../external", "/external"])
def test_member_restore_refuses_cyclic_or_escaping_hardlinks(tmp_path, target):
    plain, compressed = tmp_path / "bad.tar", tmp_path / "bad.tar.zst"
    with tarfile.open(plain, "w") as archive:
        entry = tarfile.TarInfo("alias")
        entry.type, entry.linkname = tarfile.LNKTYPE, target
        archive.addfile(entry)
    with compressed.open("wb") as output:
        subprocess.run(["zstd", "-cq", str(plain)], stdout=output, check=True)
    with pytest.raises(ValueError, match="unsafe or cyclic"):
        restore_bundle_member(compressed, "alias", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()

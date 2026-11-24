"""File hashing and single-member TAR extraction for auxiliary cold packages."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import posixpath
import subprocess
import tarfile


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_sha256_file = sha256_file


def restore_bundle_member(
    bundle_path: Path,
    member: str,
    destination: Path,
    *,
    expected: tuple[int, str] | None = None,
) -> tuple[int, str]:
    """Stream one member from an auxiliary `.tar.zst` package using stdlib tarfile."""
    if not bundle_path.is_file():
        raise FileNotFoundError(f"bundle not found: {bundle_path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.restore")
    try:
        wanted = member
        seen: set[str] = set()
        while True:
            if not wanted or wanted.startswith("/") or ".." in wanted.split("/"):
                raise ValueError(f"unsafe or cyclic archive member: {wanted}")
            wanted = posixpath.normpath(wanted)
            if wanted == "." or wanted in seen:
                raise ValueError(f"unsafe or cyclic archive member: {wanted}")
            seen.add(wanted)
            process = subprocess.Popen(
                ["zstd", "-dc", "--", str(bundle_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            assert process.stdout is not None
            next_member = None
            result = None
            stop_early = False
            try:
                with tarfile.open(fileobj=process.stdout, mode="r|", bufsize=1024 * 1024) as archive:
                    for entry in archive:
                        if posixpath.normpath(entry.name) != wanted:
                            continue
                        if entry.islnk():
                            next_member = entry.linkname
                        elif entry.isreg():
                            source = archive.extractfile(entry)
                            assert source is not None
                            hasher, total = hashlib.sha256(), 0
                            with temporary.open("wb") as output:
                                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                    total += len(chunk)
                                    hasher.update(chunk)
                                    output.write(chunk)
                            result = total, hasher.hexdigest()
                        else:
                            raise ValueError(f"archive member is not a regular file or hardlink: {wanted}")
                        break
                if expected is not None:
                    if result is not None and result != expected:
                        raise ValueError(f"restored member checksum mismatch: {member}")
                    stop_early = result is not None or next_member is not None
                if not stop_early:
                    for _ in iter(lambda: process.stdout.read(1024 * 1024), b""):
                        pass
            finally:
                process.stdout.close()
                if stop_early and process.poll() is None:
                    process.terminate()
                code = process.wait()
            if code != 0 and not stop_early:
                raise RuntimeError(f"cannot decode archive member: {wanted}")
            if result is not None:
                temporary.replace(destination)
                return result
            if next_member is None:
                raise FileNotFoundError(f"archive member not found: {wanted}")
            wanted = next_member
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

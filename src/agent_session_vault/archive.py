from __future__ import annotations

import os
from pathlib import Path
import subprocess
import hashlib
import tempfile
import posixpath


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_sha256_file = sha256_file


def _pack_to_bundle_path(source: Path, bundle_path: Path) -> None:
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    if bundle_path.exists():
        bundle_path.unlink()
    _run(["bsdtar", "--zstd", "-cf", str(bundle_path), "-C", str(source), "."])


def pack_paths(source: Path, relative_paths: list[str], bundle_path: Path) -> None:
    if not source.is_dir():
        raise FileNotFoundError(f"source tree not found: {source}")
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    if bundle_path.exists():
        bundle_path.unlink()

    list_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="asv-pack-", suffix=".list", delete=False) as handle:
            list_path = Path(handle.name)
            for relative in relative_paths:
                handle.write(relative.encode("utf-8", errors="surrogateescape"))
                handle.write(b"\0")
        _run(
            [
                "bsdtar",
                "--zstd",
                "-cf",
                str(bundle_path),
                "-C",
                str(source),
                "--null",
                "-T",
                str(list_path),
            ]
        )
    finally:
        if list_path is not None:
            list_path.unlink(missing_ok=True)


def restore_bundle(bundle_path: Path, destination: Path) -> None:
    if not bundle_path.is_file():
        raise FileNotFoundError(f"bundle not found: {bundle_path}")
    destination.mkdir(parents=True, exist_ok=True)
    _run(["bsdtar", "-xf", str(bundle_path), "-C", str(destination)])


def restore_bundle_member(bundle_path: Path, member: str, destination: Path) -> tuple[int, str]:
    """Restore one regular archive member without materializing its bundle."""
    if not bundle_path.is_file():
        raise FileNotFoundError(f"bundle not found: {bundle_path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.restore")
    process = subprocess.Popen(
        ["bsdtar", "-xOf", str(bundle_path), "--", member],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    hasher = hashlib.sha256()
    total = 0
    try:
        with temporary.open("wb") as output:
            for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
                total += len(chunk)
                hasher.update(chunk)
                output.write(chunk)
        process.stdout.close()
        if process.wait() != 0:
            raise RuntimeError(f"cannot restore archive member: {member}")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        process.stdout.close()
        process.wait()
        raise
    return total, hasher.hexdigest()


def _tar_text(value: bytes) -> str:
    return value.split(b"\0", 1)[0].decode("utf-8", errors="surrogateescape")


def _tar_size(value: bytes) -> int:
    if value and value[0] & 0x80:
        return int.from_bytes(value, "big") & ((1 << (8 * len(value) - 1)) - 1)
    text = value.strip(b"\0 ")
    return int(text or b"0", 8)


def _read_exact(stream, size: int) -> bytes:
    parts: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise RuntimeError("truncated tar stream")
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _read_member_data(stream, size: int, *, digest: bool) -> tuple[int, str] | bytes:
    if digest:
        hasher = hashlib.sha256()
        total = 0
        remaining = size
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise RuntimeError("truncated tar member")
            hasher.update(chunk)
            total += len(chunk)
            remaining -= len(chunk)
        result: tuple[int, str] | bytes = (total, hasher.hexdigest())
    else:
        result = _read_exact(stream, size)
    padding = (-size) % 512
    if padding:
        _read_exact(stream, padding)
    return result


def _pax_attributes(data: bytes) -> dict[str, str]:
    attributes: dict[str, str] = {}
    while data:
        separator = data.find(b" ")
        if separator < 1:
            break
        try:
            record_size = int(data[:separator])
        except ValueError:
            break
        record = data[:record_size]
        data = data[record_size:]
        body = record[separator + 1 :].rstrip(b"\n")
        if b"=" not in body:
            continue
        key, value = body.split(b"=", 1)
        attributes[key.decode("utf-8", errors="replace")] = value.decode("utf-8", errors="surrogateescape")
    return attributes


def _resolve_hardlinks(
    digests: dict[str, tuple[int, str]],
    links: dict[str, str],
) -> None:
    def resolve(name: str, seen: set[str]) -> tuple[int, str] | None:
        if name in digests:
            return digests[name]
        if name in seen:
            return None
        target = links.get(name)
        if target is None:
            return None
        seen.add(name)
        candidates = (target, posixpath.normpath(posixpath.join(posixpath.dirname(name), target)))
        for candidate in candidates:
            resolved = resolve(candidate, seen)
            if resolved is not None:
                return resolved
        return None

    for name in links:
        resolved = resolve(name, set())
        if resolved is not None:
            digests[name] = resolved


def bundle_member_digests(bundle_path: Path, names: list[str] | None = None) -> dict[str, tuple[int, str]]:
    """Hash a zstd tar bundle in one pass without extracting its files.

    The raw tar reader deliberately resolves hardlink records itself. NAS SMB
    shares do not support recreating those links, while the archive still needs
    every referenced conversation to be verified against its manifest.
    """
    if not bundle_path.is_file():
        raise FileNotFoundError(f"bundle not found: {bundle_path}")
    process = subprocess.Popen(
        ["zstd", "-dc", "--", str(bundle_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    digests: dict[str, tuple[int, str]] = {}
    hardlinks: dict[str, str] = {}
    global_pax: dict[str, str] = {}
    local_pax: dict[str, str] = {}
    long_name: str | None = None
    long_link: str | None = None
    try:
        while True:
            header = _read_exact(process.stdout, 512)
            if not header.strip(b"\0"):
                _read_exact(process.stdout, 512)
                break
            size = _tar_size(header[124:136])
            typeflag = header[156:157] or b"0"
            name = _tar_text(header[0:100])
            prefix = _tar_text(header[345:500])
            if prefix:
                name = f"{prefix}/{name}"
            link_name = _tar_text(header[157:257])
            if typeflag == b"x":
                local_pax = _pax_attributes(_read_member_data(process.stdout, size, digest=False))
                continue
            if typeflag == b"g":
                global_pax.update(_pax_attributes(_read_member_data(process.stdout, size, digest=False)))
                continue
            if typeflag == b"L":
                long_name = _tar_text(_read_member_data(process.stdout, size, digest=False))
                continue
            if typeflag == b"K":
                long_link = _tar_text(_read_member_data(process.stdout, size, digest=False))
                continue
            attributes = {**global_pax, **local_pax}
            local_pax = {}
            member_name = attributes.get("path", long_name or name)
            member_link = attributes.get("linkpath", long_link or link_name)
            long_name = None
            long_link = None
            if typeflag in (b"0", b"\0", b"7"):
                value = _read_member_data(process.stdout, size, digest=True)
                assert isinstance(value, tuple)
                digests[member_name] = value
            else:
                _read_member_data(process.stdout, size, digest=False)
                if typeflag == b"1":
                    hardlinks[member_name] = member_link
    finally:
        process.stdout.close()
        if process.wait() != 0:
            raise RuntimeError("zstd failed while reading archive")
    _resolve_hardlinks(digests, hardlinks)
    return digests if names is None else {name: digests[name] for name in names if name in digests}


def verify_bundle_members(
    bundle_path: Path,
    expected: dict[str, tuple[int, str]],
) -> tuple[int, list[str]]:
    """Verify manifest members by streaming the compressed bundle once."""
    if not bundle_path.is_file():
        return 0, [f"missing_bundle:{bundle_path}"]
    try:
        actual = bundle_member_digests(bundle_path)
    except Exception as exc:
        return 0, [f"bundle_decode_failed:{type(exc).__name__}"]
    checked = 0
    failures: list[str] = []
    for name, expected_item in expected.items():
        actual_item = actual.get(name)
        if actual_item is None:
            failures.append(f"missing_member:{name}")
            continue
        checked += 1
        if actual_item != expected_item:
            failures.append(f"file_hash_mismatch:{name}")
    return checked, failures

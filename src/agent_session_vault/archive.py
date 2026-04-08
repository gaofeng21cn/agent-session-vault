from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess
import hashlib


@dataclass(frozen=True)
class PackedBundle:
    bundle_path: Path
    manifest_path: Path


@dataclass(frozen=True)
class BundleInventoryItem:
    bundle_path: Path
    manifest_path: Path
    payload: dict


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pack_to_bundle_path(source: Path, bundle_path: Path) -> None:
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    if bundle_path.exists():
        bundle_path.unlink()
    _run(["bsdtar", "--zstd", "-cf", str(bundle_path), "-C", str(source), "."])


def pack_tree(source: Path, output_dir: Path, bundle_name: str) -> PackedBundle:
    if not source.is_dir():
        raise FileNotFoundError(f"source tree not found: {source}")

    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = output_dir / f"{bundle_name}.tar.zst"
    manifest_path = output_dir / f"{bundle_name}.manifest.json"

    if bundle_path.exists():
        bundle_path.unlink()
    if manifest_path.exists():
        manifest_path.unlink()

    _pack_to_bundle_path(source, bundle_path)

    file_count = sum(1 for path in source.rglob("*") if path.is_file())
    payload = {
        "bundle_name": bundle_name,
        "source": str(source),
        "bundle_path": str(bundle_path),
        "created_at": datetime.now(UTC).isoformat(),
        "file_count": file_count,
    }
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return PackedBundle(bundle_path=bundle_path, manifest_path=manifest_path)


def restore_bundle(bundle_path: Path, destination: Path) -> None:
    if not bundle_path.is_file():
        raise FileNotFoundError(f"bundle not found: {bundle_path}")
    destination.mkdir(parents=True, exist_ok=True)
    _run(["bsdtar", "-xf", str(bundle_path), "-C", str(destination)])


def offload_tree(source: Path, archive_root: Path, bundle_name: str, remove_source: bool = False) -> PackedBundle:
    archive_root.mkdir(parents=True, exist_ok=True)
    bundle = pack_tree(source=source, output_dir=archive_root, bundle_name=bundle_name)
    if remove_source:
        shutil.rmtree(source)
    return bundle


def inventory_bundles(archive_root: Path) -> list[BundleInventoryItem]:
    if not archive_root.exists():
        return []
    items: list[BundleInventoryItem] = []
    for manifest_path in sorted(archive_root.rglob("*.manifest.json")):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        bundle_path = manifest_path.with_suffix("").with_suffix(".tar.zst")
        if bundle_path.exists():
            payload.setdefault("sha256", _sha256_file(bundle_path))
            items.append(BundleInventoryItem(bundle_path=bundle_path, manifest_path=manifest_path, payload=payload))
    return items

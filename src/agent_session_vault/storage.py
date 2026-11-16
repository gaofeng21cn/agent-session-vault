from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .config import VaultConfig
from .views import discover_home_project_codex_roots, discover_project_codex_roots


def filesystem_capacity(path: Path) -> dict[str, int | str]:
    """Darwin statvfs can wrap SMB counters at 2**32 KiB; use native df there."""
    if sys.platform != "darwin":
        usage = shutil.disk_usage(path)
        return {"method": "disk_usage", "total_bytes": usage.total, "used_bytes": usage.used,
                "available_bytes": usage.free}
    result = subprocess.run(["/bin/df", "-kP", str(path)], capture_output=True, text=True,
                            timeout=30, env={**os.environ, "LC_ALL": "C"})
    if result.returncode != 0:
        # SMB mount source can contain temporary credentials; never echo df output.
        raise ValueError(f"native filesystem capacity failed (exit {result.returncode})")
    rows = result.stdout.splitlines()
    match = re.fullmatch(r".+?\s+(\d+)\s+(\d+)\s+(\d+)\s+\d+%\s+.+", rows[-1]) if rows else None
    if match is None:
        raise ValueError("native filesystem capacity has no valid numeric row")
    total, used, available = (int(value) * 1024 for value in match.groups())
    if not total or min(used, available) < 0 or available > total:
        raise ValueError("native filesystem capacity counters are invalid")
    return {"method": "df-kP", "total_bytes": total, "used_bytes": used, "available_bytes": available}


@dataclass(frozen=True)
class StorageItem:
    label: str
    path: Path
    size_bytes: int


@dataclass(frozen=True)
class StorageSummary:
    items: list[StorageItem]
    total_bytes: int


def _directory_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _home_project_label(home_root: Path, root: Path) -> str:
    projects_root = home_root / ".codex" / "projects"
    try:
        relative = root.relative_to(projects_root)
    except ValueError:
        return root.name
    parts = relative.parts
    if len(parts) >= 4 and parts[1] == "archive":
        return f"{parts[0]}:{parts[2]}"
    return root.name


def summarize_storage(config: VaultConfig) -> StorageSummary:
    items: list[StorageItem] = []

    live_roots = {
        "live:codex": config.paths.home / ".codex",
        "live:gemini": config.paths.home / ".gemini",
        "live:openclaw": config.paths.home / ".openclaw",
    }
    for label, path in live_roots.items():
        if path.exists():
            items.append(StorageItem(label=label, path=path, size_bytes=_directory_size(path)))

    for root in discover_project_codex_roots(config.paths.workspace_root):
        items.append(
            StorageItem(
                label=f"live:workspace_codex:{root.parent.name}",
                path=root,
                size_bytes=_directory_size(root),
            )
        )
    for root in discover_home_project_codex_roots(config.paths.home):
        items.append(
            StorageItem(
                label=f"live:home_project_codex:{_home_project_label(config.paths.home, root)}",
                path=root,
                size_bytes=_directory_size(root),
            )
        )

    if config.paths.import_root.is_dir():
        for machine_root in sorted(path for path in config.paths.import_root.iterdir() if path.is_dir()):
            raw_root = machine_root / ".raw"
            if not raw_root.is_dir():
                continue
            for client_root in sorted(path for path in raw_root.iterdir() if path.is_dir()):
                items.append(
                    StorageItem(
                        label=f"projection:{machine_root.name}:{client_root.name}",
                        path=client_root,
                        size_bytes=_directory_size(client_root),
                    )
                )

    if config.paths.local_workspace_extras.exists():
        items.append(
            StorageItem(
                label="projection:managed_extras",
                path=config.paths.local_workspace_extras,
                size_bytes=_directory_size(config.paths.local_workspace_extras),
            )
        )
    for label, path in (
        ("stable:analytics", config.paths.stable_root),
        ("archive:full_fidelity", config.archive.root),
    ):
        if path.exists():
            items.append(StorageItem(label=label, path=path, size_bytes=_directory_size(path)))

    total = sum(item.size_bytes for item in items)
    return StorageSummary(items=items, total_bytes=total)

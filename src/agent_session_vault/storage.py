from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import VaultConfig
from .views import discover_project_codex_roots


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

    for machine in config.machines.values():
        for client in machine.clients:
            raw_root = config.paths.import_root / machine.import_name / ".raw" / client
            if raw_root.exists():
                items.append(
                    StorageItem(
                        label=f"imports_raw:{machine.import_name}:{client}",
                        path=raw_root,
                        size_bytes=_directory_size(raw_root),
                    )
                )
            canonical_root = config.paths.import_root / machine.import_name / client
            if canonical_root.exists():
                items.append(
                    StorageItem(
                        label=f"canonical:{machine.import_name}:{client}",
                        path=canonical_root,
                        size_bytes=_directory_size(canonical_root),
                    )
                )

    if config.paths.shadow_home.exists():
        items.append(
            StorageItem(
                label="canonical:shadow_home",
                path=config.paths.shadow_home,
                size_bytes=_directory_size(config.paths.shadow_home),
            )
        )
    if config.paths.local_workspace_extras.exists():
        items.append(
            StorageItem(
                label="canonical:local_workspace_extras",
                path=config.paths.local_workspace_extras,
                size_bytes=_directory_size(config.paths.local_workspace_extras),
            )
        )

    total = sum(item.size_bytes for item in items)
    return StorageSummary(items=items, total_bytes=total)

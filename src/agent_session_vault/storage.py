from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import VaultConfig
from .views import discover_home_project_codex_roots, discover_project_codex_roots


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

    import_names = {machine.import_name for machine in config.machines.values()}
    if config.paths.import_root.is_dir():
        import_names.update(
            path.name
            for path in config.paths.import_root.iterdir()
            if path.is_dir() and path.name != "local-home"
        )
    for import_name in sorted(import_names):
        machine = next(
            (item for item in config.machines.values() if item.import_name == import_name),
            None,
        )
        clients = machine.clients if machine else ("codex", "gemini", "openclaw")
        for client in clients:
            raw_root = config.paths.import_root / import_name / ".raw" / client
            if raw_root.exists():
                items.append(
                    StorageItem(
                        label=f"imports_raw:{import_name}:{client}",
                        path=raw_root,
                        size_bytes=_directory_size(raw_root),
                    )
                )
            canonical_root = config.paths.import_root / import_name / client
            if canonical_root.exists():
                items.append(
                    StorageItem(
                        label=f"canonical:{import_name}:{client}",
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

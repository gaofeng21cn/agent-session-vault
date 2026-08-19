from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import VaultConfig
from .projection import LOCAL_HOME_CLIENTS, local_home_projection_root


@dataclass(frozen=True)
class TokscaleView:
    home: Path
    extra_dirs: list[tuple[str, Path]]

    def tokscale_extra_dirs(self) -> str:
        return ",".join(f"{client}:{path}" for client, path in self.extra_dirs)


def discover_project_codex_roots(workspace_root: Path) -> list[Path]:
    if not workspace_root.exists():
        return []
    roots: list[Path] = []
    for child in sorted(workspace_root.iterdir()):
        candidate = child / ".codex"
        if child.is_dir() and candidate.is_dir():
            roots.append(candidate)
    return roots


def discover_home_project_codex_roots(home_root: Path) -> list[Path]:
    projects_root = home_root / ".codex" / "projects"
    if not projects_root.exists():
        return []
    roots: list[Path] = []
    for project_root in sorted(projects_root.iterdir()):
        archive_root = project_root / "archive"
        if not archive_root.is_dir():
            continue
        for codex_root in sorted(archive_root.glob("*/codex")):
            if not codex_root.is_dir():
                continue
            if (codex_root / "sessions").is_dir() or (codex_root / "archived_sessions").is_dir():
                roots.append(codex_root)
    return roots


def discover_local_workspace_extra_codex_roots(extras_root: Path, *, managed_only: bool = False) -> list[Path]:
    if not extras_root.exists():
        return []
    roots: list[Path] = []
    for child in sorted(extras_root.iterdir()):
        if managed_only and not (child / "sync-state.json").is_file():
            continue
        candidate = child / "codex"
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def _append_unique_root(extra_dirs: list[tuple[str, Path]], seen: set[tuple[str, Path]], client: str, root: Path) -> None:
    if not root.exists():
        return
    key = (client, root.resolve())
    if key in seen:
        return
    seen.add(key)
    extra_dirs.append((client, root))


def build_tokscale_view(config: VaultConfig) -> TokscaleView:
    extra_dirs: list[tuple[str, Path]] = []
    seen: set[tuple[str, Path]] = set()
    local_machine_root = local_home_projection_root(config)
    for client in LOCAL_HOME_CLIENTS:
        _append_unique_root(extra_dirs, seen, client, local_machine_root / ".raw" / client)
    if config.paths.import_root.is_dir():
        for machine_root in sorted(config.paths.import_root.iterdir()):
            if not machine_root.is_dir() or machine_root.name == "local-home":
                continue
            for client in LOCAL_HOME_CLIENTS:
                _append_unique_root(extra_dirs, seen, client, machine_root / ".raw" / client)
    for root in discover_local_workspace_extra_codex_roots(config.paths.local_workspace_extras, managed_only=True):
        _append_unique_root(extra_dirs, seen, "codex", root)
    return TokscaleView(home=config.paths.projection_home, extra_dirs=extra_dirs)

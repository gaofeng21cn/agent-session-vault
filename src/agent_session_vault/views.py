from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import VaultConfig


@dataclass(frozen=True)
class View:
    mode: str
    home: Path
    extra_dirs: list[tuple[str, Path]]
    omx_replay_dedupe: str

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


def build_view(config: VaultConfig, mode: str, omx_replay_dedupe: str = "off") -> View:
    if mode not in {"raw", "canonical"}:
        raise ValueError(f"unsupported mode: {mode}")
    if omx_replay_dedupe not in {"off", "strict"}:
        raise ValueError(f"unsupported omx replay dedupe mode: {omx_replay_dedupe}")
    if mode == "canonical" and omx_replay_dedupe != "strict":
        raise ValueError("canonical mode requires --omx-replay-dedupe strict")

    extra_dirs: list[tuple[str, Path]] = []
    if mode == "raw":
        home = config.paths.home
        for machine in config.machines.values():
            for client in machine.clients:
                root = config.paths.import_root / machine.import_name / ".raw" / client
                if root.exists():
                    extra_dirs.append((client, root))
        for root in discover_project_codex_roots(config.paths.workspace_root):
            extra_dirs.append(("codex", root))
        return View(mode=mode, home=home, extra_dirs=extra_dirs, omx_replay_dedupe=omx_replay_dedupe)

    home = config.paths.shadow_home
    for machine in config.machines.values():
        for client in machine.clients:
            root = config.paths.import_root / machine.import_name / client
            if root.exists():
                extra_dirs.append((client, root))
    extras_root = config.paths.local_workspace_extras
    if extras_root.exists():
        for child in sorted(extras_root.iterdir()):
            candidate = child / "codex"
            if candidate.is_dir():
                extra_dirs.append(("codex", candidate))
    return View(mode=mode, home=home, extra_dirs=extra_dirs, omx_replay_dedupe=omx_replay_dedupe)

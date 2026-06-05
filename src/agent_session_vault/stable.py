from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess
import time

from .config import VaultConfig


@dataclass(frozen=True)
class StableMirrorItem:
    label: str
    kind: str
    source: Path
    destination: Path


@dataclass(frozen=True)
class StableMirrorItemResult:
    label: str
    kind: str
    source: Path
    destination: Path
    source_exists: bool
    source_bytes: int
    source_files: int
    status: str
    duration_seconds: float
    command: list[str] | None
    exit_code: int | None
    stderr: str | None


@dataclass(frozen=True)
class StableMirrorResult:
    stable_root: Path
    manifest_path: Path | None
    dry_run: bool
    mirrored_at: str
    items: list[StableMirrorItemResult]


def default_stable_root(config: VaultConfig) -> Path:
    return config.paths.archive_root.expanduser().parent / "stable"


def stable_mirror_items(config: VaultConfig, stable_root: Path | None = None) -> list[StableMirrorItem]:
    root = (stable_root or default_stable_root(config)).expanduser()
    return [
        StableMirrorItem(
            label="imports",
            kind="directory",
            source=config.paths.import_root.expanduser(),
            destination=root / "tokscale" / "imports",
        ),
        StableMirrorItem(
            label="local_workspace_extras",
            kind="directory",
            source=config.paths.local_workspace_extras.expanduser(),
            destination=root / "tokscale" / "local-workspace-extras",
        ),
        StableMirrorItem(
            label="config",
            kind="file",
            source=config.config_path.expanduser(),
            destination=root / "config" / "config.toml",
        ),
    ]


def _tree_stats(path: Path) -> tuple[int, int]:
    if path.is_file():
        return path.stat().st_size, 1
    total_bytes = 0
    total_files = 0
    for child in path.rglob("*"):
        if child.is_file():
            total_bytes += child.stat().st_size
            total_files += 1
    return total_bytes, total_files


def _rsync_source(path: Path) -> str:
    return f"{path}/"


def mirror_stable_layer(
    config: VaultConfig,
    *,
    stable_root: Path | None = None,
    dry_run: bool = False,
) -> StableMirrorResult:
    root = (stable_root or default_stable_root(config)).expanduser()
    mirrored_at = datetime.now(UTC).isoformat()
    results: list[StableMirrorItemResult] = []

    for item in stable_mirror_items(config, root):
        source = item.source.expanduser()
        destination = item.destination.expanduser()
        exists = source.exists()
        source_bytes, source_files = _tree_stats(source) if exists else (0, 0)
        started = time.monotonic()

        if not exists:
            results.append(
                StableMirrorItemResult(
                    label=item.label,
                    kind=item.kind,
                    source=source,
                    destination=destination,
                    source_exists=False,
                    source_bytes=0,
                    source_files=0,
                    status="skipped_missing",
                    duration_seconds=0.0,
                    command=None,
                    exit_code=None,
                    stderr=None,
                )
            )
            continue

        if item.kind == "directory":
            command = ["rsync", "-a", _rsync_source(source), _rsync_source(destination)]
            if dry_run:
                status = "planned"
                exit_code = None
                stderr = None
            else:
                destination.mkdir(parents=True, exist_ok=True)
                completed = subprocess.run(command, text=True, capture_output=True)
                status = "mirrored" if completed.returncode == 0 else "failed"
                exit_code = completed.returncode
                stderr = completed.stderr.strip() or None
                if completed.returncode != 0:
                    raise RuntimeError(f"stable mirror failed for {item.label}: {stderr or completed.returncode}")
        elif item.kind == "file":
            command = None
            if dry_run:
                status = "planned"
                exit_code = None
                stderr = None
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                status = "mirrored"
                exit_code = 0
                stderr = None
        else:
            raise ValueError(f"unsupported stable mirror item kind: {item.kind}")

        results.append(
            StableMirrorItemResult(
                label=item.label,
                kind=item.kind,
                source=source,
                destination=destination,
                source_exists=True,
                source_bytes=source_bytes,
                source_files=source_files,
                status=status,
                duration_seconds=round(time.monotonic() - started, 3),
                command=command,
                exit_code=exit_code,
                stderr=stderr,
            )
        )

    manifest_path = None if dry_run else root / "stable-layer-manifest.json"
    result = StableMirrorResult(
        stable_root=root,
        manifest_path=manifest_path,
        dry_run=dry_run,
        mirrored_at=mirrored_at,
        items=results,
    )
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(stable_mirror_payload(result), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def stable_mirror_payload(result: StableMirrorResult) -> dict[str, object]:
    return {
        "stable_root": str(result.stable_root),
        "manifest_path": str(result.manifest_path) if result.manifest_path else None,
        "dry_run": result.dry_run,
        "mirrored_at": result.mirrored_at,
        "items": [
            {
                "label": item.label,
                "kind": item.kind,
                "source": str(item.source),
                "destination": str(item.destination),
                "source_exists": item.source_exists,
                "source_bytes": item.source_bytes,
                "source_files": item.source_files,
                "status": item.status,
                "duration_seconds": item.duration_seconds,
                "command": item.command,
                "exit_code": item.exit_code,
                "stderr": item.stderr,
            }
            for item in result.items
        ],
    }

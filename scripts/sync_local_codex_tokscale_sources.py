#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_session_vault.config import load_config  # noqa: E402
from agent_session_vault.local_codex import sync_local_codex_sources  # noqa: E402


PRUNE_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
}


def discover_runtime_roots(workspace_root: Path) -> list[Path]:
    workspace_root = workspace_root.expanduser().resolve()
    if not workspace_root.exists():
        return []

    roots: set[Path] = set()
    for current, dir_names, _ in os.walk(workspace_root):
        dir_names[:] = [name for name in dir_names if name not in PRUNE_DIR_NAMES]
        current_path = Path(current)
        if current_path.name != ".ds":
            continue

        if (current_path / "codex_homes").is_dir() or (
            current_path / "cold_archive" / "codex_sessions"
        ).is_dir():
            roots.add(current_path.parent.resolve())

        dir_names[:] = []

    return sorted(roots, key=str)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync volatile local Codex runtime homes into Tokscale extras")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--workspace-root", type=Path, default=None)
    parser.add_argument("--source", action="append", type=Path, default=[])
    parser.add_argument("--namespace", default="volatile-codex-homes")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    workspace_root = (args.workspace_root or config.paths.workspace_root).expanduser()

    discovered = discover_runtime_roots(workspace_root)
    explicit_sources = [source.expanduser().resolve() for source in args.source]
    sources = sorted({*discovered, *explicit_sources}, key=str)

    result = sync_local_codex_sources(
        config,
        sources=sources,
        namespace=args.namespace,
        dry_run=args.dry_run,
    )
    payload = {
        "workspace_root": str(workspace_root),
        "discovered_sources": [str(source) for source in discovered],
        "explicit_sources": [str(source) for source in explicit_sources],
        "namespace": result.namespace,
        "codex_root": str(result.codex_root),
        "state_path": str(result.state_path),
        "files_seen": result.files_seen,
        "files_written": result.files_written,
        "files_skipped": result.files_skipped,
        "source_bytes_total": result.source_bytes_total,
        "dest_bytes_total": result.dest_bytes_total,
        "token_events_total": result.token_events_total,
        "missing_sources": [str(path) for path in result.missing_sources],
        "dry_run": args.dry_run,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

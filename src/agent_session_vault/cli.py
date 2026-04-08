from __future__ import annotations

import argparse
from datetime import datetime, UTC
import json
from pathlib import Path
import subprocess
import sys

from .adapters import build_canonicalize_machine_command, build_direct_sync_command
from .archive import inventory_bundles, offload_tree, pack_tree, restore_bundle
from .config import load_config
from .projection import (
    expected_local_projection_bundle_dir,
    export_machine_projection,
    export_machine_projection_ssh,
    fetch_projection_bundle_ssh,
    import_machine_projection,
    pending_projection_bundle_dirs,
)
from .relay import (
    export_machine_delta,
    export_machine_delta_ssh,
    import_machine_delta,
    inspect_machine_delta_ssh,
    pending_relay_bundle_dirs,
)
from .retention import apply_archive_plan, build_archive_plan
from .storage import summarize_storage
from .syncing import choose_projection_transport, choose_sync_strategy, expected_local_bundle_dir
from .tokscale import build_tokscale_invocation
from .views import build_view


def _json_dump(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _run_subprocess(command: list[str], env: dict[str, str] | None = None, dry_run: bool = False) -> int:
    if dry_run:
        print(" ".join(command))
        return 0
    completed = subprocess.run(command, env=env)
    return completed.returncode


def _load_projection_base_snapshot_id(machine_root: Path) -> str | None:
    state_path = machine_root / ".projection-state.json"
    if not state_path.is_file():
        return None
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    snapshot_id = payload.get("current_snapshot_id") if isinstance(payload, dict) else None
    return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-session-vault")
    parser.add_argument("--config", type=Path, default=None)

    subparsers = parser.add_subparsers(dest="command", required=True)

    config_parser = subparsers.add_parser("config", help="Inspect loaded configuration")
    config_parser.add_argument("--json", action="store_true")

    storage_parser = subparsers.add_parser("storage", help="Storage utilities")
    storage_sub = storage_parser.add_subparsers(dest="storage_command", required=True)
    storage_summary = storage_sub.add_parser("summary", help="Show storage summary")
    storage_summary.add_argument("--json", action="store_true")

    tokscale_parser = subparsers.add_parser("tokscale", help="Tokscale exporter")
    tokscale_sub = tokscale_parser.add_subparsers(dest="tokscale_command", required=True)
    tokscale_env = tokscale_sub.add_parser("env", help="Show computed Tokscale environment")
    tokscale_env.add_argument("--mode", choices=["raw", "canonical"], default="raw")
    tokscale_env.add_argument("--omx-replay-dedupe", choices=["off", "strict"], default="off")
    tokscale_env.add_argument("--json", action="store_true")

    tokscale_exec = tokscale_sub.add_parser("exec", help="Run official Tokscale with computed environment")
    tokscale_exec.add_argument("--mode", choices=["raw", "canonical"], default="raw")
    tokscale_exec.add_argument("--omx-replay-dedupe", choices=["off", "strict"], default="off")
    tokscale_exec.add_argument("--dry-run", action="store_true")
    tokscale_exec.add_argument("tokscale_args", nargs=argparse.REMAINDER)

    sync_parser = subparsers.add_parser("sync", help="Sync helpers")
    sync_sub = sync_parser.add_subparsers(dest="sync_command", required=True)
    sync_direct = sync_sub.add_parser("direct", help="Run configured direct sync adapter")
    sync_direct.add_argument("machine")
    sync_direct.add_argument("--dry-run", action="store_true")
    sync_canonicalize = sync_sub.add_parser("canonicalize-machine", help="Rebuild canonical tree for one machine")
    sync_canonicalize.add_argument("machine")
    sync_canonicalize.add_argument("--dry-run", action="store_true")
    sync_relay_export = sync_sub.add_parser("relay-export", help="Export one machine delta bundle into a relay directory")
    sync_relay_export.add_argument("machine")
    sync_relay_export.add_argument("--source-home", type=Path, required=True)
    sync_relay_export.add_argument("--relay-root", type=Path, default=None)
    sync_relay_export.add_argument("--json", action="store_true")
    sync_relay_export_ssh = sync_sub.add_parser(
        "relay-export-ssh",
        help="Export one machine delta bundle on the remote machine via SSH",
    )
    sync_relay_export_ssh.add_argument("machine")
    sync_relay_export_ssh.add_argument("--json", action="store_true")
    sync_inspect = sync_sub.add_parser("inspect", help="Inspect remote delta stats for one machine")
    sync_inspect.add_argument("machine")
    sync_inspect.add_argument("--json", action="store_true")
    sync_relay_import = sync_sub.add_parser("relay-import", help="Import one relay delta bundle into local imports")
    sync_relay_import.add_argument("machine")
    sync_relay_import.add_argument("--bundle-dir", type=Path, required=True)
    sync_relay_import.add_argument("--canonicalize-command", default="tokscale-canonicalize-import-machine")
    sync_relay_import.add_argument("--json", action="store_true")
    sync_projection_export = sync_sub.add_parser("projection-export", help="Export one full projection bundle locally")
    sync_projection_export.add_argument("machine")
    sync_projection_export.add_argument("--source-home", type=Path, required=True)
    sync_projection_export.add_argument("--relay-root", type=Path, default=None)
    sync_projection_export.add_argument("--json", action="store_true")
    sync_projection_export_ssh = sync_sub.add_parser(
        "projection-export-ssh",
        help="Export one full projection bundle on the remote machine via SSH",
    )
    sync_projection_export_ssh.add_argument("machine")
    sync_projection_export_ssh.add_argument("--json", action="store_true")
    sync_projection_import = sync_sub.add_parser("projection-import", help="Import one projection bundle into local imports")
    sync_projection_import.add_argument("machine")
    sync_projection_import.add_argument("--bundle-dir", type=Path, required=True)
    sync_projection_import.add_argument("--canonicalize-command", default="tokscale-canonicalize-import-machine")
    sync_projection_import.add_argument("--json", action="store_true")
    sync_auto = sync_sub.add_parser("auto", help="Run projection-first sync with ssh/relay transport selection")
    sync_auto.add_argument("machine")
    sync_auto.add_argument("--canonicalize-command", default="tokscale-canonicalize-import-machine")
    sync_auto.add_argument("--transport", choices=["auto", "ssh", "relay"], default=None)
    sync_auto.add_argument("--dry-run", action="store_true")
    sync_auto.add_argument("--json", action="store_true")

    archive_parser = subparsers.add_parser("archive", help="Archive helpers")
    archive_sub = archive_parser.add_subparsers(dest="archive_command", required=True)
    archive_pack = archive_sub.add_parser("pack-tree", help="Pack one directory tree into a tar.zst bundle")
    archive_pack.add_argument("--source", required=True, type=Path)
    archive_pack.add_argument("--output-dir", required=True, type=Path)
    archive_pack.add_argument("--bundle-name", required=True)
    archive_pack.add_argument("--json", action="store_true")
    archive_offload = archive_sub.add_parser("offload-tree", help="Pack a tree into archive root and optionally remove local source")
    archive_offload.add_argument("--source", required=True, type=Path)
    archive_offload.add_argument("--bundle-name", required=True)
    archive_offload.add_argument("--archive-root", type=Path, default=None)
    archive_offload.add_argument("--remove-source", action="store_true")
    archive_offload.add_argument("--json", action="store_true")
    archive_restore = archive_sub.add_parser("restore", help="Restore one bundle into a directory")
    archive_restore.add_argument("--bundle", required=True, type=Path)
    archive_restore.add_argument("--dest", required=True, type=Path)
    archive_plan = archive_sub.add_parser("plan", help="Plan which trees should be offloaded to archive")
    archive_plan.add_argument("--rule", action="append", default=[])
    archive_plan.add_argument("--json", action="store_true")
    archive_apply = archive_sub.add_parser("apply", help="Apply archive offload rules")
    archive_apply.add_argument("--rule", action="append", default=[])
    archive_apply.add_argument("--dry-run", action="store_true")
    archive_apply.add_argument("--json", action="store_true")
    archive_inventory = archive_sub.add_parser("inventory", help="List archived bundles")
    archive_inventory.add_argument("--archive-root", type=Path, default=None)
    archive_inventory.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.command == "config":
        payload = {
            "config_path": str(config.config_path),
            "paths": {
                "home": str(config.paths.home),
                "workspace_root": str(config.paths.workspace_root),
                "import_root": str(config.paths.import_root),
                "shadow_home": str(config.paths.shadow_home),
                "local_workspace_extras": str(config.paths.local_workspace_extras),
                "archive_root": str(config.paths.archive_root),
                "relay_root": str(config.paths.relay_root),
            },
            "sync": {
                "default_strategy": config.sync.default_strategy,
                "direct_max_delta_files": config.sync.direct_max_delta_files,
                "direct_max_delta_bytes": config.sync.direct_max_delta_bytes,
                "projection_transport": config.sync.projection_transport,
                "projection_direct_max_bundle_bytes": config.sync.projection_direct_max_bundle_bytes,
            },
            "machines": {
                name: {
                    "import_name": machine.import_name,
                    "ssh_target": machine.ssh_target,
                    "source_home": str(machine.source_home) if machine.source_home else None,
                    "remote_relay_root": str(machine.remote_relay_root) if machine.remote_relay_root else None,
                    "remote_state_root": str(machine.remote_state_root) if machine.remote_state_root else None,
                    "sync_strategy": machine.sync_strategy,
                    "direct_max_delta_files": machine.direct_max_delta_files,
                    "direct_max_delta_bytes": machine.direct_max_delta_bytes,
                    "clients": list(machine.clients),
                    "roots": [
                        {
                            "client": rule.client,
                            "path": rule.path,
                            "glob": rule.glob,
                            "label": rule.label,
                            "kind": rule.kind,
                        }
                        for rule in machine.roots
                    ],
                    "root_globs": [
                        {
                            "client": rule.client,
                            "path": rule.path,
                            "glob": rule.glob,
                            "label": rule.label,
                            "kind": rule.kind,
                        }
                        for rule in machine.root_globs
                    ],
                }
                for name, machine in config.machines.items()
            },
            "retention_rules": [
                {
                    "name": rule.name,
                    "layer": rule.layer,
                    "machine": rule.machine,
                    "client": rule.client,
                    "workspace": rule.workspace,
                    "max_age_days": rule.max_age_days,
                    "min_size_bytes": rule.min_size_bytes,
                    "archive_subdir": rule.archive_subdir,
                    "remove_source": rule.remove_source,
                }
                for rule in config.retention_rules
            ],
        }
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "storage" and args.storage_command == "summary":
        summary = summarize_storage(config)
        payload = {
            "total_bytes": summary.total_bytes,
            "items": [
                {"label": item.label, "path": str(item.path), "size_bytes": item.size_bytes}
                for item in summary.items
            ],
        }
        if args.json:
            _json_dump(payload)
        else:
            for item in payload["items"]:
                print(f'{item["label"]}\t{item["size_bytes"]}\t{item["path"]}')
            print(f"total_bytes\t{summary.total_bytes}")
        return 0

    if args.command == "tokscale" and args.tokscale_command == "env":
        view = build_view(config, mode=args.mode, omx_replay_dedupe=args.omx_replay_dedupe)
        payload = {
            "mode": view.mode,
            "home": str(view.home),
            "omx_replay_dedupe": view.omx_replay_dedupe,
            "extra_dirs": [{"client": client, "path": str(path)} for client, path in view.extra_dirs],
            "env": {
                "HOME": str(view.home),
                "TOKSCALE_EXTRA_DIRS": view.tokscale_extra_dirs(),
                "NPM_CONFIG_CACHE": str(config.paths.home / ".npm"),
            },
        }
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "tokscale" and args.tokscale_command == "exec":
        tokscale_args = list(args.tokscale_args)
        if tokscale_args and tokscale_args[0] == "--":
            tokscale_args = tokscale_args[1:]
        invocation = build_tokscale_invocation(
            config,
            mode=args.mode,
            omx_replay_dedupe=args.omx_replay_dedupe,
            args=tokscale_args,
        )
        return _run_subprocess(invocation.command, env=invocation.env, dry_run=args.dry_run)

    if args.command == "sync" and args.sync_command == "direct":
        command = build_direct_sync_command(config, args.machine)
        return _run_subprocess(command, dry_run=args.dry_run)

    if args.command == "sync" and args.sync_command == "canonicalize-machine":
        command = build_canonicalize_machine_command(config, args.machine)
        return _run_subprocess(command, dry_run=args.dry_run)

    if args.command == "sync" and args.sync_command == "relay-export":
        relay_root = (args.relay_root or config.paths.relay_root).expanduser()
        state_root = config.paths.home / ".config" / "agent-session-vault" / "relay-state"
        bundle = export_machine_delta(
            machine_name=args.machine,
            source_home=args.source_home,
            relay_root=relay_root,
            state_root=state_root,
        )
        payload = {
            "machine": bundle.machine_name,
            "snapshot_id": bundle.snapshot_id,
            "previous_snapshot_id": bundle.previous_snapshot_id,
            "bundle_dir": str(bundle.bundle_dir),
            "manifest_path": str(bundle.manifest_path),
            "bundle_path": str(bundle.bundle_path),
        }
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "sync" and args.sync_command == "relay-export-ssh":
        machine = config.machines[args.machine]
        if not machine.ssh_target or not machine.source_home or not machine.remote_relay_root or not machine.remote_state_root:
            raise ValueError(f"machine '{args.machine}' is missing ssh relay configuration")
        bundle = export_machine_delta_ssh(
            machine_name=args.machine,
            source_home=machine.source_home,
            relay_root=machine.remote_relay_root,
            state_root=machine.remote_state_root,
            ssh_target=machine.ssh_target,
        )
        local_bundle_dir = expected_local_bundle_dir(config, args.machine, bundle.snapshot_id)
        payload = {
            "machine": bundle.machine_name,
            "snapshot_id": bundle.snapshot_id,
            "previous_snapshot_id": bundle.previous_snapshot_id,
            "remote_bundle_dir": str(bundle.bundle_dir),
            "remote_manifest_path": str(bundle.manifest_path),
            "remote_bundle_path": str(bundle.bundle_path),
            "expected_local_bundle_dir": str(local_bundle_dir),
        }
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "sync" and args.sync_command == "inspect":
        machine = config.machines[args.machine]
        if not machine.ssh_target or not machine.source_home or not machine.remote_relay_root or not machine.remote_state_root:
            raise ValueError(f"machine '{args.machine}' is missing ssh relay configuration")
        pending_bundle_dirs = pending_relay_bundle_dirs(config, args.machine)
        stats = inspect_machine_delta_ssh(
            machine_name=args.machine,
            source_home=machine.source_home,
            relay_root=machine.remote_relay_root,
            state_root=machine.remote_state_root,
            ssh_target=machine.ssh_target,
        )
        decision = choose_sync_strategy(config, args.machine, stats)
        payload = {
            "machine": stats.machine_name,
            "changed_files": stats.changed_files,
            "changed_bytes": stats.changed_bytes,
            "total_files": stats.total_files,
            "total_bytes": stats.total_bytes,
            "previous_snapshot_id": stats.previous_snapshot_id,
            "next_snapshot_id": stats.next_snapshot_id,
            "pending_local_bundle_dirs": [str(path) for path in pending_bundle_dirs],
            "decision": {
                "strategy": decision.strategy,
                "reason": decision.reason,
                "direct_max_delta_files": decision.direct_max_delta_files,
                "direct_max_delta_bytes": decision.direct_max_delta_bytes,
            },
        }
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "sync" and args.sync_command == "relay-import":
        bundle = import_machine_delta(
            config=config,
            machine_name=args.machine,
            bundle_dir=args.bundle_dir,
            canonicalize_command=args.canonicalize_command,
        )
        payload = {
            "machine": bundle.machine_name,
            "snapshot_id": bundle.snapshot_id,
            "previous_snapshot_id": bundle.previous_snapshot_id,
            "bundle_dir": str(bundle.bundle_dir),
        }
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "sync" and args.sync_command == "projection-export":
        relay_root = (args.relay_root or config.paths.relay_root).expanduser()
        machine = config.machines[args.machine]
        bundle = export_machine_projection(
            machine=machine,
            source_home=args.source_home,
            relay_root=relay_root,
            machine_root=config.paths.import_root / machine.import_name,
        )
        payload = {
            "machine": bundle.machine_name,
            "snapshot_id": bundle.snapshot_id,
            "bundle_dir": str(bundle.bundle_dir),
            "manifest_path": str(bundle.manifest_path),
            "bundle_path": str(bundle.bundle_path),
            "roots_manifest_path": str(bundle.roots_manifest_path),
            "inventory_path": str(bundle.inventory_path) if bundle.inventory_path else None,
            "bundle_bytes": bundle.bundle_bytes,
            "mode": bundle.mode,
            "base_snapshot_id": bundle.base_snapshot_id,
            "fallback_reason": bundle.fallback_reason,
        }
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "sync" and args.sync_command == "projection-export-ssh":
        machine = config.machines[args.machine]
        if not machine.ssh_target or not machine.source_home or not machine.remote_relay_root:
            raise ValueError(f"machine '{args.machine}' is missing ssh projection configuration")
        machine_root = config.paths.import_root / machine.import_name
        bundle = export_machine_projection_ssh(
            machine=machine,
            source_home=machine.source_home,
            relay_root=machine.remote_relay_root,
            ssh_target=machine.ssh_target,
            base_snapshot_id=_load_projection_base_snapshot_id(machine_root),
        )
        local_bundle_dir = expected_local_projection_bundle_dir(config, args.machine, bundle.snapshot_id)
        payload = {
            "machine": bundle.machine_name,
            "snapshot_id": bundle.snapshot_id,
            "remote_bundle_dir": str(bundle.bundle_dir),
            "remote_manifest_path": str(bundle.manifest_path),
            "remote_bundle_path": str(bundle.bundle_path),
            "expected_local_bundle_dir": str(local_bundle_dir),
            "bundle_bytes": bundle.bundle_bytes,
            "mode": bundle.mode,
            "base_snapshot_id": bundle.base_snapshot_id,
            "fallback_reason": bundle.fallback_reason,
        }
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "sync" and args.sync_command == "projection-import":
        bundle = import_machine_projection(
            config=config,
            machine_name=args.machine,
            bundle_dir=args.bundle_dir,
            canonicalize_command=args.canonicalize_command,
        )
        payload = {
            "machine": bundle.machine_name,
            "snapshot_id": bundle.snapshot_id,
            "bundle_dir": str(bundle.bundle_dir),
            "roots_manifest_path": str(bundle.roots_manifest_path),
            "inventory_path": str(bundle.inventory_path) if bundle.inventory_path else None,
            "bundle_bytes": bundle.bundle_bytes,
            "mode": bundle.mode,
            "base_snapshot_id": bundle.base_snapshot_id,
            "fallback_reason": bundle.fallback_reason,
        }
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "sync" and args.sync_command == "auto":
        machine = config.machines[args.machine]
        machine_root = config.paths.import_root / machine.import_name
        if not machine.ssh_target or not machine.source_home or not machine.remote_relay_root:
            raise ValueError(f"machine '{args.machine}' is missing ssh projection configuration")
        pending_bundle_dirs = pending_projection_bundle_dirs(config, args.machine)
        if pending_bundle_dirs:
            payload: dict[str, object] = {
                "machine": args.machine,
                "status": "pending_local_projection_import",
                "pending_local_bundle_dirs": [str(path) for path in pending_bundle_dirs],
            }
            if not args.dry_run:
                imported_dirs: list[str] = []
                for bundle_dir in pending_bundle_dirs:
                    imported = import_machine_projection(
                        config=config,
                        machine_name=args.machine,
                        bundle_dir=bundle_dir,
                        canonicalize_command=args.canonicalize_command,
                    )
                    imported_dirs.append(str(imported.bundle_dir))
                payload["status"] = "projection_imported_pending"
                payload["imported_bundle_dirs"] = imported_dirs
            if args.json:
                _json_dump(payload)
            else:
                print(payload)
            return 0

        bundle = export_machine_projection_ssh(
            machine=machine,
            source_home=machine.source_home,
            relay_root=machine.remote_relay_root,
            ssh_target=machine.ssh_target,
            base_snapshot_id=_load_projection_base_snapshot_id(machine_root),
        )
        transport = choose_projection_transport(
            config,
            args.machine,
            bundle_bytes=bundle.bundle_bytes,
            requested_transport=args.transport,
        )
        payload: dict[str, object] = {
            "machine": args.machine,
            "snapshot_id": bundle.snapshot_id,
            "bundle": {
                "remote_bundle_dir": str(bundle.bundle_dir),
                "remote_manifest_path": str(bundle.manifest_path),
                "remote_bundle_path": str(bundle.bundle_path),
                "bytes": bundle.bundle_bytes,
                "mode": bundle.mode,
                "base_snapshot_id": bundle.base_snapshot_id,
                "fallback_reason": bundle.fallback_reason,
            },
            "decision": {
                "transport": transport.transport,
                "reason": transport.reason,
                "direct_max_bundle_bytes": transport.direct_max_bundle_bytes,
            },
        }

        local_bundle_dir = expected_local_projection_bundle_dir(config, args.machine, bundle.snapshot_id)
        payload["expected_local_bundle_dir"] = str(local_bundle_dir)

        if args.dry_run:
            payload["status"] = "projection_exported"
            if args.json:
                _json_dump(payload)
            else:
                print(payload)
            return 0

        if transport.transport == "ssh":
            fetched_dir = fetch_projection_bundle_ssh(
                ssh_target=machine.ssh_target,
                remote_bundle_dir=bundle.bundle_dir,
                local_bundle_dir=local_bundle_dir,
            )
            imported = import_machine_projection(
                config=config,
                machine_name=args.machine,
                bundle_dir=fetched_dir,
                canonicalize_command=args.canonicalize_command,
            )
            payload["status"] = "projection_imported"
            payload["imported_bundle_dir"] = str(imported.bundle_dir)
            payload["roots_manifest_path"] = str(imported.roots_manifest_path)
        else:
            payload["status"] = "projection_exported_waiting_for_delivery"
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "archive" and args.archive_command == "pack-tree":
        bundle = pack_tree(args.source, args.output_dir, args.bundle_name)
        payload = {"bundle_path": str(bundle.bundle_path), "manifest_path": str(bundle.manifest_path)}
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "archive" and args.archive_command == "offload-tree":
        archive_root = (args.archive_root or config.paths.archive_root).expanduser()
        bundle = offload_tree(args.source, archive_root, args.bundle_name, remove_source=args.remove_source)
        payload = {"bundle_path": str(bundle.bundle_path), "manifest_path": str(bundle.manifest_path)}
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "archive" and args.archive_command == "restore":
        restore_bundle(args.bundle, args.dest)
        print(str(args.dest))
        return 0

    if args.command == "archive" and args.archive_command == "plan":
        rule_names = set(args.rule) if args.rule else None
        plan = build_archive_plan(config, now=datetime.now(UTC), rule_names=rule_names)
        payload = [
            {
                "rule_name": candidate.rule_name,
                "layer": candidate.layer,
                "source": str(candidate.source),
                "archive_dir": str(candidate.archive_dir),
                "bundle_name": candidate.bundle_name,
                "size_bytes": candidate.size_bytes,
                "file_count": candidate.file_count,
                "age_days": candidate.age_days,
                "remove_source": candidate.remove_source,
            }
            for candidate in plan
        ]
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    if args.command == "archive" and args.archive_command == "apply":
        rule_names = set(args.rule) if args.rule else None
        plan = build_archive_plan(config, now=datetime.now(UTC), rule_names=rule_names)
        payload = [
            {
                "rule_name": candidate.rule_name,
                "source": str(candidate.source),
                "archive_dir": str(candidate.archive_dir),
                "bundle_name": candidate.bundle_name,
                "size_bytes": candidate.size_bytes,
                "remove_source": candidate.remove_source,
            }
            for candidate in plan
        ]
        if args.dry_run:
            if args.json:
                _json_dump(payload)
            else:
                print(payload)
            return 0
        bundles = apply_archive_plan(config, plan)
        result_payload = [
            {
                "bundle_path": str(bundle.bundle_path),
                "manifest_path": str(bundle.manifest_path),
            }
            for bundle in bundles
        ]
        if args.json:
            _json_dump({"plan": payload, "results": result_payload})
        else:
            print({"plan": payload, "results": result_payload})
        return 0

    if args.command == "archive" and args.archive_command == "inventory":
        archive_root = (args.archive_root or config.paths.archive_root).expanduser()
        items = inventory_bundles(archive_root)
        payload = [
            {
                "bundle_path": str(item.bundle_path),
                "manifest_path": str(item.manifest_path),
                "payload": item.payload,
            }
            for item in items
        ]
        if args.json:
            _json_dump(payload)
        else:
            print(payload)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

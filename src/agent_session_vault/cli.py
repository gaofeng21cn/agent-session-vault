from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .archive_catalog import query_catalog, rebuild_catalog
from .archive_ops import (
    archive_backend,
    archive_cycle,
    build_snapshot,
    init_backend,
    publish_snapshot,
    verify_snapshot,
)
from .archive_prune import apply_prune_plan, build_prune_plan, load_prune_plan, prune_plan_payload, write_prune_plan
from .archive_restore import build_restore_plan, load_restore_plan, restore_plan, write_restore_plan
from .config import load_config
from .daily_ops import DEFAULT_CLIENTS, run_daily_tokscale
from .fleet import sync_fleet
from .local_codex import sync_local_codex_sources
from .projection import local_home_projection_payload, refresh_local_home_projection
from .stable import (
    default_stable_root,
    migration_plan_payload,
    mirror_stable_layer,
    restore_stable_layer,
    stable_mirror_payload,
)
from .stable_pack import DEFAULT_SHARD_TARGET_BYTES
from .storage import summarize_storage
from .tokscale import build_tokscale_invocation
from .views import build_tokscale_view


def _json_dump(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _run_subprocess(command: list[str], env: dict[str, str] | None = None, dry_run: bool = False) -> int:
    if dry_run:
        print(" ".join(command))
        return 0
    completed = subprocess.run(command, env=env)
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-session-vault")
    parser.add_argument("--config", type=Path, default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    config_parser = subparsers.add_parser("config", help="Inspect loaded configuration")
    config_parser.add_argument("--json", action="store_true")

    storage_parser = subparsers.add_parser("storage", help="Inspect and restore analytics storage")
    storage_sub = storage_parser.add_subparsers(dest="storage_command", required=True)
    storage_summary = storage_sub.add_parser("summary", help="Show storage summary")
    storage_summary.add_argument("--json", action="store_true")
    storage_mirror = storage_sub.add_parser("mirror-stable", help="Mirror the Tokscale analytics layer")
    storage_mirror.add_argument("--dest-root", type=Path, default=None)
    storage_mirror.add_argument("--include-live-sessions", action="store_true")
    storage_mirror.add_argument("--dry-run", action="store_true")
    storage_mirror.add_argument(
        "--shard-target-mib",
        type=int,
        default=DEFAULT_SHARD_TARGET_BYTES // (1024 * 1024),
    )
    storage_mirror.add_argument("--json", action="store_true")
    storage_restore = storage_sub.add_parser("restore-stable", help="Restore a verified stable layer")
    storage_restore.add_argument("--stable-root", type=Path, default=None)
    storage_restore.add_argument("--dest-root", type=Path, required=True)
    storage_restore.add_argument("--label", action="append", dest="labels", default=[])
    storage_restore.add_argument("--json", action="store_true")
    storage_plan = storage_sub.add_parser("migration-plan", help="Inspect analytics and optional live-session coverage")
    storage_plan.add_argument("--stable-root", type=Path, default=None)
    storage_plan.add_argument("--json", action="store_true")

    tokscale_parser = subparsers.add_parser("tokscale", help="Run Tokscale against the managed projection")
    tokscale_sub = tokscale_parser.add_subparsers(dest="tokscale_command", required=True)
    tokscale_env = tokscale_sub.add_parser("env", help="Show the computed Tokscale environment")
    tokscale_env.add_argument("--json", action="store_true")
    tokscale_exec = tokscale_sub.add_parser("exec", help="Run official Tokscale with the managed projection")
    tokscale_exec.add_argument("--dry-run", action="store_true", help="Print the command without running it")
    tokscale_exec.add_argument("tokscale_args", nargs=argparse.REMAINDER)

    ops_parser = subparsers.add_parser("ops", help="Run complete operational workflows")
    ops_sub = ops_parser.add_subparsers(dest="ops_command", required=True)
    daily = ops_sub.add_parser("daily-tokscale", help="Sync Fleet projections and submit Tokscale once")
    daily.add_argument("--fleet-command", default="opl-fleet")
    daily.add_argument("--fleet-instance", type=Path, default=None)
    daily.add_argument("--clients", default=",".join(DEFAULT_CLIENTS))
    daily.add_argument("--run-root", type=Path, default=None)
    daily.add_argument("--sync-timeout-seconds", type=float, default=1800)
    daily.add_argument("--submit-timeout-seconds", type=float, default=3600)
    daily.add_argument("--force-contract-check", action="store_true")
    daily.add_argument("--mirror-stable", action="store_true")
    daily.add_argument("--stable-root", type=Path, default=None)
    daily.add_argument("--json", action="store_true")
    archive_cycle_parser = ops_sub.add_parser(
        "archive-cycle",
        help="Run one due full-fidelity archive cycle without pruning local sources",
    )
    archive_cycle_parser.add_argument("--machine-id", default=None)
    archive_cycle_parser.add_argument("--due-only", action=argparse.BooleanOptionalAction, default=True)
    archive_cycle_parser.add_argument("--deep", action=argparse.BooleanOptionalAction, default=True)
    archive_cycle_parser.add_argument("--json", action="store_true")

    sync_parser = subparsers.add_parser("sync", help="Refresh managed projections")
    sync_sub = sync_parser.add_subparsers(dest="sync_command", required=True)
    local_codex = sync_sub.add_parser("local-codex", help="Import an explicit volatile Codex source")
    local_codex.add_argument("--source", action="append", type=Path, default=[])
    local_codex.add_argument("--source-glob", action="append", default=[])
    local_codex.add_argument("--namespace", default="volatile-codex-homes")
    local_codex.add_argument("--dry-run", action="store_true")
    local_codex.add_argument("--json", action="store_true")
    local_home = sync_sub.add_parser("local-home-projection", help="Refresh the current HOME projection")
    local_home.add_argument("--dry-run", action="store_true")
    local_home.add_argument("--json", action="store_true")
    fleet = sync_sub.add_parser("fleet", help="Refresh projections from every approved Fleet node")
    fleet.add_argument("--fleet-command", default="opl-fleet")
    fleet.add_argument("--fleet-instance", type=Path, default=None)
    fleet.add_argument("--timeout-seconds", type=float, default=1800)
    fleet.add_argument("--json", action="store_true")

    archive_parser = subparsers.add_parser("archive", help="Manage full-fidelity Codex archives")
    archive_sub = archive_parser.add_subparsers(dest="archive_command", required=True)
    archive_init = archive_sub.add_parser("init", help="Initialize the archive root")
    archive_init.add_argument("--json", action="store_true")
    archive_snapshot = archive_sub.add_parser("snapshot", help="Build a local staging snapshot")
    archive_snapshot.add_argument("--machine-id", default=None)
    archive_snapshot.add_argument("--staging-root", type=Path, default=None)
    archive_snapshot.add_argument("--json", action="store_true")
    archive_publish = archive_sub.add_parser("publish", help="Publish a staged snapshot")
    archive_publish.add_argument("--staging-root", type=Path, required=True)
    archive_publish.add_argument("--verify-staged", action="store_true")
    archive_publish.add_argument("--json", action="store_true")
    archive_verify = archive_sub.add_parser("verify", help="Verify a committed snapshot")
    archive_verify.add_argument("--snapshot", required=True)
    archive_verify.add_argument("--deep", action="store_true")
    archive_verify.add_argument("--json", action="store_true")
    archive_list = archive_sub.add_parser("list", help="Query the committed archive catalog")
    archive_list.add_argument("--from", dest="from_at", default=None)
    archive_list.add_argument("--to", dest="to_at", default=None)
    archive_list.add_argument("--machine-id", default=None)
    archive_list.add_argument("--client", default="codex")
    archive_list.add_argument("--session-id", default=None)
    archive_list.add_argument("--source-id", default=None)
    archive_list.add_argument("--json", action="store_true")
    archive_catalog = archive_sub.add_parser("catalog-rebuild", help="Rebuild catalog segments")
    archive_catalog.add_argument("--machine-id", default=None)
    archive_catalog.add_argument("--json", action="store_true")
    archive_plan_restore = archive_sub.add_parser("plan-restore", help="Create a staging restore plan")
    archive_plan_restore.add_argument("--destination", required=True, type=Path)
    archive_plan_restore.add_argument("--from", dest="from_at", default=None)
    archive_plan_restore.add_argument("--to", dest="to_at", default=None)
    archive_plan_restore.add_argument("--machine-id", default=None)
    archive_plan_restore.add_argument("--client", default="codex")
    archive_plan_restore.add_argument("--session-id", default=None)
    archive_plan_restore.add_argument("--source-id", default=None)
    archive_plan_restore.add_argument("--collision-policy", choices=["error", "overwrite"], default="error")
    archive_plan_restore.add_argument("--plan-path", type=Path, default=None)
    archive_plan_restore.add_argument("--json", action="store_true")
    archive_restore = archive_sub.add_parser("restore", help="Apply a staging restore plan")
    archive_restore.add_argument("--plan", type=Path, required=True)
    archive_restore.add_argument("--json", action="store_true")
    archive_prune_plan = archive_sub.add_parser("prune-plan", help="Build a verified local prune plan")
    archive_prune_plan.add_argument("--plan-path", required=True, type=Path)
    archive_prune_plan.add_argument("--json", action="store_true")
    archive_prune_apply = archive_sub.add_parser("prune-apply", help="Apply a verified local prune plan")
    archive_prune_apply.add_argument("--plan", required=True, type=Path)
    archive_prune_apply.add_argument("--json", action="store_true")
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
                "projection_home": str(config.paths.projection_home),
                "local_workspace_extras": str(config.paths.local_workspace_extras),
                "stable_root": str(config.paths.stable_root),
            },
            "archive": {
                "root": str(config.archive.root),
                "cadence_days": config.archive.cadence_days,
                "cold_age_days": config.archive.cold_age_days,
                "staging_root": str(config.archive.staging_root),
                "machine_id_path": str(config.archive.machine_id_path),
                "source_paths": [
                    {"path": item.path, "kind": item.kind, "label": item.label}
                    for item in config.archive.source_paths
                ],
                "require_quiescent_for_prune": config.archive.require_quiescent_for_prune,
            },
        }
        _json_dump(payload) if args.json else print(payload)
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

    if args.command == "storage" and args.storage_command == "mirror-stable":
        result = mirror_stable_layer(
            config,
            stable_root=args.dest_root,
            dry_run=args.dry_run,
            include_live_sessions=args.include_live_sessions,
            shard_target_bytes=args.shard_target_mib * 1024 * 1024,
        )
        payload = stable_mirror_payload(result)
        _json_dump(payload) if args.json else print(payload)
        return 0 if result.status in {"planned", "verified"} else 1

    if args.command == "storage" and args.storage_command == "restore-stable":
        payload = restore_stable_layer(
            args.stable_root or default_stable_root(config),
            args.dest_root,
            labels=set(args.labels) if args.labels else None,
        )
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "storage" and args.storage_command == "migration-plan":
        payload = migration_plan_payload(config, stable_root=args.stable_root)
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "tokscale" and args.tokscale_command == "env":
        view = build_tokscale_view(config)
        payload = {
            "input_policy": "projection-only",
            "home": str(view.home),
            "source_home_excluded": view.home != config.paths.home,
            "extra_dirs": [{"client": client, "path": str(path)} for client, path in view.extra_dirs],
            "env": {
                "HOME": str(view.home),
                "TOKSCALE_EXTRA_DIRS": view.tokscale_extra_dirs(),
                "NPM_CONFIG_CACHE": str(config.paths.home / ".npm"),
            },
        }
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "tokscale" and args.tokscale_command == "exec":
        tokscale_args = list(args.tokscale_args)
        if tokscale_args and tokscale_args[0] == "--":
            tokscale_args = tokscale_args[1:]
        if not args.dry_run:
            refresh_local_home_projection(config)
        invocation = build_tokscale_invocation(config, args=tokscale_args)
        return _run_subprocess(invocation.command, env=invocation.env, dry_run=args.dry_run)

    if args.command == "ops" and args.ops_command == "daily-tokscale":
        clients = tuple(client.strip() for client in args.clients.split(",") if client.strip())
        result = run_daily_tokscale(
            config,
            clients=clients,
            run_root=args.run_root,
            sync_timeout_seconds=args.sync_timeout_seconds,
            submit_timeout_seconds=args.submit_timeout_seconds,
            force_contract_check=args.force_contract_check,
            mirror_stable=args.mirror_stable,
            stable_root=args.stable_root,
            fleet_command=args.fleet_command,
            fleet_instance=args.fleet_instance,
        )
        _json_dump(result.payload) if args.json else print(result.payload)
        return result.exit_code

    if args.command == "ops" and args.ops_command == "archive-cycle":
        result = archive_cycle(config, machine_id=args.machine_id, due_only=args.due_only, deep=args.deep)
        payload = result.payload()
        _json_dump(payload) if args.json else print(payload)
        return 0 if result.status in {"verified", "not_due"} else 2

    if args.command == "sync" and args.sync_command == "local-codex":
        result = sync_local_codex_sources(
            config,
            sources=list(args.source),
            source_globs=list(args.source_glob),
            namespace=args.namespace,
            dry_run=args.dry_run,
        )
        payload = {
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
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "sync" and args.sync_command == "local-home-projection":
        result = refresh_local_home_projection(config, dry_run=args.dry_run)
        payload = {**local_home_projection_payload(result), "dry_run": args.dry_run}
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "sync" and args.sync_command == "fleet":
        local = refresh_local_home_projection(config)
        result = sync_fleet(
            config,
            fleet_command=args.fleet_command,
            instance=args.fleet_instance,
            timeout_seconds=args.timeout_seconds,
        )
        payload = {
            "status": "completed",
            "local_projection": local_home_projection_payload(local),
            "nodes": [{"node_id": node.node_id, "local": node.local} for node in result.nodes],
            "results": [
                {
                    "node_id": item.node_id,
                    "import_name": item.import_name,
                    "status": item.status,
                    "fleet": item.payload,
                    "bundle": (
                        {
                            "snapshot_id": item.bundle.snapshot_id,
                            "mode": item.bundle.mode,
                            "bundle_bytes": item.bundle.bundle_bytes,
                        }
                        if item.bundle
                        else None
                    ),
                }
                for item in result.results
            ],
        }
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "archive" and args.archive_command == "init":
        payload = init_backend(config)
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "archive" and args.archive_command == "snapshot":
        result = build_snapshot(config, machine_id=args.machine_id, staging_root=args.staging_root)
        payload = result.payload()
        _json_dump(payload) if args.json else print(payload)
        return 0 if all(snapshot.status == "staged" for snapshot in result.snapshots) else 2

    if args.command == "archive" and args.archive_command == "publish":
        published = publish_snapshot(config, args.staging_root.expanduser(), verify_staged=args.verify_staged)
        payload = {
            "status": "published",
            "backend_root": str(archive_backend(config).root),
            "snapshots": [
                {
                    "snapshot_id": item.snapshot.snapshot_id,
                    "source_id": item.snapshot.source.source_id,
                    "snapshot_dir": str(item.snapshot_dir),
                    "manifest_sha256": item.snapshot.manifest_sha256,
                }
                for item in published
            ],
        }
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "archive" and args.archive_command == "verify":
        payload = verify_snapshot(config, args.snapshot, deep=args.deep)
        _json_dump(payload) if args.json else print(payload)
        return 0 if payload["status"] == "verified" else 2

    if args.command == "archive" and args.archive_command == "list":
        entries = query_catalog(
            config,
            from_at=args.from_at,
            to_at=args.to_at,
            machine_id=args.machine_id,
            client=args.client,
            session_id=args.session_id,
            source_id=args.source_id,
        )
        payload = [entry.to_payload() for entry in entries]
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "archive" and args.archive_command == "catalog-rebuild":
        backend = archive_backend(config)
        payload = {
            "status": "rebuilt",
            "segments": rebuild_catalog(backend, machine_id=args.machine_id),
            "backend_root": str(backend.root),
        }
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "archive" and args.archive_command == "plan-restore":
        plan = build_restore_plan(
            config,
            destination=args.destination,
            from_at=args.from_at,
            to_at=args.to_at,
            machine_id=args.machine_id,
            client=args.client,
            session_id=args.session_id,
            source_id=args.source_id,
            collision_policy=args.collision_policy,
        )
        plan_path = write_restore_plan(plan, args.plan_path) if args.plan_path else None
        payload = {**plan.to_payload(), "plan_path": str(plan_path) if plan_path else None}
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "archive" and args.archive_command == "restore":
        payload = restore_plan(config, load_restore_plan(args.plan))
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "archive" and args.archive_command == "prune-plan":
        plan = build_prune_plan(config)
        plan_path = write_prune_plan(plan, args.plan_path)
        payload = prune_plan_payload(plan, plan_path=plan_path)
        _json_dump(payload) if args.json else print(payload)
        return 0

    if args.command == "archive" and args.archive_command == "prune-apply":
        payload = apply_prune_plan(config, load_prune_plan(args.plan))
        _json_dump(payload) if args.json else print(payload)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

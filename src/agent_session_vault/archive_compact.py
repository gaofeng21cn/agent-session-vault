"""Restic-native repository compaction, retention pruning, and deep verification."""
from __future__ import annotations

from .archive_ops import archive_backend, receipt
from .archive_sources import load_or_create_machine_id
from .config import VaultConfig


def compact_archive(
    config: VaultConfig,
    *,
    keep_last: int | None = None,
    keep_daily: int | None = None,
    keep_weekly: int | None = None,
    keep_monthly: int | None = None,
    dry_run: bool = False,
    verify: bool = True,
    repack_small: bool = False,
    machine_id: str | None = None,
) -> dict[str, object]:
    """Compact/prune the restic archive repository and optionally run ``restic check --read-data``."""
    for name, value in (
        ("keep_last", keep_last),
        ("keep_daily", keep_daily),
        ("keep_weekly", keep_weekly),
        ("keep_monthly", keep_monthly),
    ):
        if value is not None and value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    backend = archive_backend(config)
    backend.ensure_writable()
    backend.ensure_ready()
    repo = backend.restic()

    before_stats = repo.stats(mode="raw-data")
    has_retention = any(item is not None for item in (keep_last, keep_daily, keep_weekly, keep_monthly))
    if has_retention:
        source_tags = sorted(
            {
                tag
                for snap in repo.snapshots(tags=("vault:archive",))
                for tag in snap.tags
                if tag.startswith("source:")
            }
        )
        if source_tags:
            per_source = {
                tag: repo.forget(
                    keep_last=keep_last,
                    keep_daily=keep_daily,
                    keep_weekly=keep_weekly,
                    keep_monthly=keep_monthly,
                    prune=False,
                    dry_run=dry_run,
                    tags=(tag,),
                    group_by="host",
                )
                for tag in source_tags
            }
            prune_summary = repo.prune(dry_run=dry_run, repack_small=repack_small)
            operation_summary: dict[str, object] = {"sources": per_source, "prune": prune_summary}
        else:
            operation_summary = repo.forget(
                keep_last=keep_last,
                keep_daily=keep_daily,
                keep_weekly=keep_weekly,
                keep_monthly=keep_monthly,
                prune=True,
                dry_run=dry_run,
                group_by="host",
                repack_small=repack_small,
            )
        mode = "forget_prune"
    else:
        operation_summary = repo.prune(dry_run=dry_run, repack_small=repack_small)
        mode = "prune"

    check_payload: dict[str, object] | None = None
    if not dry_run and verify:
        check_result = repo.check(read_data=True)
        check_payload = check_result.to_payload()
        if not check_result.ok:
            raise RuntimeError(f"restic check --read-data failed after compaction: {check_payload}")

    after_stats = repo.stats(mode="raw-data")
    status = "planned" if dry_run else "verified"
    receipt_path: str | None = None
    if not dry_run:
        resolved_machine_id = machine_id or load_or_create_machine_id(config)
        recorded = receipt(
            config,
            "archive-compact",
            status,
            machine_id=resolved_machine_id,
            details={
                "mode": mode,
                "retention": {
                    "keep_last": keep_last,
                    "keep_daily": keep_daily,
                    "keep_weekly": keep_weekly,
                    "keep_monthly": keep_monthly,
                },
                "before_raw_bytes": before_stats.total_size,
                "after_raw_bytes": after_stats.total_size,
                "net_reclaimed_bytes": before_stats.total_size - after_stats.total_size,
                "repo_check": check_payload,
            },
        )
        receipt_path = str(backend.root / "receipts" / resolved_machine_id / f"{recorded.operation_id}.json")

    return {
        "status": status,
        "mode": mode,
        "dry_run": dry_run,
        "backend_root": str(backend.root),
        "repo_path": str(backend.repo_path),
        "before": before_stats.to_payload(),
        "after": after_stats.to_payload(),
        "net_reclaimed_bytes": before_stats.total_size - after_stats.total_size,
        "operation": operation_summary,
        "repo_check": check_payload,
        "receipt_path": receipt_path,
    }

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .archive import PackedBundle, offload_tree
from .config import RetentionRuleConfig, VaultConfig


@dataclass(frozen=True)
class ArchiveCandidate:
    rule_name: str
    layer: str
    source: Path
    archive_dir: Path
    bundle_name: str
    size_bytes: int
    file_count: int
    newest_mtime: float
    age_days: int
    remove_source: bool


def _directory_stats(path: Path) -> tuple[int, int, float] | None:
    if not path.is_dir():
        return None
    size_bytes = 0
    file_count = 0
    newest_mtime = 0.0
    for child in path.rglob("*"):
        if not child.is_file():
            continue
        stat = child.stat()
        size_bytes += stat.st_size
        file_count += 1
        if stat.st_mtime > newest_mtime:
            newest_mtime = stat.st_mtime
    if file_count == 0:
        return None
    return size_bytes, file_count, newest_mtime


def _resolve_rule_source(config: VaultConfig, rule: RetentionRuleConfig) -> Path:
    if rule.layer == "imports_raw":
        if rule.machine is None or rule.client is None:
            raise ValueError(f"retention rule {rule.name} requires machine and client for imports_raw")
        machine = config.machines[rule.machine]
        return config.paths.import_root / machine.import_name / ".raw" / rule.client

    if rule.layer == "imports_canonical":
        if rule.machine is None or rule.client is None:
            raise ValueError(f"retention rule {rule.name} requires machine and client for imports_canonical")
        machine = config.machines[rule.machine]
        return config.paths.import_root / machine.import_name / rule.client

    if rule.layer == "live_home_client":
        if rule.client is None:
            raise ValueError(f"retention rule {rule.name} requires client for live_home_client")
        return config.paths.home / f".{rule.client}"

    if rule.layer == "workspace_codex":
        if rule.workspace is None:
            raise ValueError(f"retention rule {rule.name} requires workspace for workspace_codex")
        return config.paths.workspace_root / rule.workspace / ".codex"

    raise ValueError(f"unsupported retention layer: {rule.layer}")


def build_archive_plan(
    config: VaultConfig,
    now: datetime | None = None,
    rule_names: set[str] | None = None,
) -> list[ArchiveCandidate]:
    now = now or datetime.now(UTC)
    now_ts = now.timestamp()
    candidates: list[ArchiveCandidate] = []
    for rule in config.retention_rules:
        if rule_names and rule.name not in rule_names:
            continue
        source = _resolve_rule_source(config, rule)
        stats = _directory_stats(source)
        if stats is None:
            continue
        size_bytes, file_count, newest_mtime = stats
        age_days = int(max(0.0, now_ts - newest_mtime) // 86400)
        if age_days < rule.max_age_days:
            continue
        if size_bytes < rule.min_size_bytes:
            continue
        archive_dir = config.paths.archive_root / (rule.archive_subdir or rule.name)
        bundle_name = f"{rule.name}-{datetime.fromtimestamp(newest_mtime, UTC).strftime('%Y%m%dT%H%M%SZ')}"
        candidates.append(
            ArchiveCandidate(
                rule_name=rule.name,
                layer=rule.layer,
                source=source,
                archive_dir=archive_dir,
                bundle_name=bundle_name,
                size_bytes=size_bytes,
                file_count=file_count,
                newest_mtime=newest_mtime,
                age_days=age_days,
                remove_source=rule.remove_source,
            )
        )
    return candidates


def apply_archive_plan(config: VaultConfig, candidates: list[ArchiveCandidate]) -> list[PackedBundle]:
    _ = config
    results: list[PackedBundle] = []
    for candidate in candidates:
        results.append(
            offload_tree(
                source=candidate.source,
                archive_root=candidate.archive_dir,
                bundle_name=candidate.bundle_name,
                remove_source=candidate.remove_source,
            )
        )
    return results

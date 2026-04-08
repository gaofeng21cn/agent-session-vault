from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import VaultConfig


@dataclass(frozen=True)
class DeltaStats:
    machine_name: str
    changed_files: int
    changed_bytes: int
    previous_snapshot_id: str | None
    next_snapshot_id: str | None
    total_files: int = 0
    total_bytes: int = 0


@dataclass(frozen=True)
class SyncDecision:
    strategy: str
    reason: str
    changed_files: int
    changed_bytes: int
    direct_max_delta_files: int
    direct_max_delta_bytes: int


@dataclass(frozen=True)
class ProjectionTransportDecision:
    transport: str
    reason: str
    bundle_bytes: int
    direct_max_bundle_bytes: int


def _resolve_strategy(config: VaultConfig, machine_name: str) -> str:
    machine = config.machines[machine_name]
    return machine.sync_strategy or config.sync.default_strategy


def _resolve_thresholds(config: VaultConfig, machine_name: str) -> tuple[int, int]:
    machine = config.machines[machine_name]
    return (
        machine.direct_max_delta_files
        if machine.direct_max_delta_files is not None
        else config.sync.direct_max_delta_files,
        machine.direct_max_delta_bytes
        if machine.direct_max_delta_bytes is not None
        else config.sync.direct_max_delta_bytes,
    )


def has_relay_prerequisites(config: VaultConfig, machine_name: str) -> bool:
    machine = config.machines[machine_name]
    return bool(machine.ssh_target and machine.source_home and machine.remote_relay_root and machine.remote_state_root)


def choose_sync_strategy(config: VaultConfig, machine_name: str, stats: DeltaStats) -> SyncDecision:
    requested = _resolve_strategy(config, machine_name)
    direct_max_delta_files, direct_max_delta_bytes = _resolve_thresholds(config, machine_name)
    if requested == "direct":
        strategy = "direct"
        reason = "forced_by_config"
    elif requested == "relay":
        strategy = "relay"
        reason = "forced_by_config"
    elif not has_relay_prerequisites(config, machine_name):
        strategy = "direct"
        reason = "relay_prerequisites_missing"
    elif stats.changed_files <= direct_max_delta_files and stats.changed_bytes <= direct_max_delta_bytes:
        strategy = "direct"
        reason = "delta_within_direct_threshold"
    else:
        strategy = "relay"
        reason = "delta_exceeds_direct_threshold"
    return SyncDecision(
        strategy=strategy,
        reason=reason,
        changed_files=stats.changed_files,
        changed_bytes=stats.changed_bytes,
        direct_max_delta_files=direct_max_delta_files,
        direct_max_delta_bytes=direct_max_delta_bytes,
    )


def expected_local_bundle_dir(config: VaultConfig, machine_name: str, snapshot_id: str) -> Path:
    return config.paths.relay_root / machine_name / snapshot_id


def choose_projection_transport(
    config: VaultConfig,
    machine_name: str,
    bundle_bytes: int,
    requested_transport: str | None = None,
) -> ProjectionTransportDecision:
    requested = requested_transport or config.sync.projection_transport
    threshold = config.sync.projection_direct_max_bundle_bytes
    if requested == "ssh":
        return ProjectionTransportDecision(
            transport="ssh",
            reason="forced_by_config",
            bundle_bytes=bundle_bytes,
            direct_max_bundle_bytes=threshold,
        )
    if requested == "relay":
        return ProjectionTransportDecision(
            transport="relay",
            reason="forced_by_config",
            bundle_bytes=bundle_bytes,
            direct_max_bundle_bytes=threshold,
        )
    if bundle_bytes <= threshold:
        return ProjectionTransportDecision(
            transport="ssh",
            reason="bundle_within_direct_threshold",
            bundle_bytes=bundle_bytes,
            direct_max_bundle_bytes=threshold,
        )
    return ProjectionTransportDecision(
        transport="relay",
        reason="bundle_exceeds_direct_threshold",
        bundle_bytes=bundle_bytes,
        direct_max_bundle_bytes=threshold,
    )

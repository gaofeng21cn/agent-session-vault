from __future__ import annotations

from .config import VaultConfig


def build_direct_sync_command(config: VaultConfig, machine_name: str) -> list[str]:
    machine = config.machines[machine_name]
    if not machine.ssh_target:
        raise ValueError(f"machine '{machine_name}' has no ssh_target")
    return ["tokscale-pull-remote", machine.import_name, machine.ssh_target]


def build_canonicalize_machine_command(config: VaultConfig, machine_name: str) -> list[str]:
    machine = config.machines[machine_name]
    return ["tokscale-canonicalize-import-machine", machine.import_name]

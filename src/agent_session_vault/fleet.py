from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from .config import VaultConfig
from .projection import (
    ProjectionBundle,
    fleet_projection_request,
    import_machine_projection,
)


@dataclass(frozen=True)
class FleetNode:
    node_id: str
    local: bool


@dataclass(frozen=True)
class FleetSyncResult:
    node_id: str
    import_name: str
    status: str
    payload: dict[str, object]
    bundle: ProjectionBundle | None


@dataclass(frozen=True)
class FleetSyncSummary:
    nodes: tuple[FleetNode, ...]
    results: tuple[FleetSyncResult, ...]


def _run_json(command: list[str], *, timeout_seconds: float) -> dict[str, object]:
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    text = completed.stdout.strip()
    if not text:
        raise RuntimeError(completed.stderr.strip() or f"command returned exit {completed.returncode}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command did not return JSON: {text[-500:]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("command JSON must be an object")
    if completed.returncode not in {0, 2}:
        raise RuntimeError(completed.stderr.strip() or f"command returned exit {completed.returncode}")
    return payload


def discover_fleet_nodes(
    fleet_command: str,
    *,
    instance: Path | None,
    timeout_seconds: float = 60,
) -> list[FleetNode]:
    command = [fleet_command]
    if instance is not None:
        command.extend(["--instance", str(instance)])
    command.extend(["nodes", "--json"])
    payload = _run_json(command, timeout_seconds=timeout_seconds)
    entries = payload.get("nodes")
    if not isinstance(entries, list):
        raise RuntimeError("Fleet nodes payload is missing nodes")
    controller = payload.get("controller")
    if not isinstance(controller, str) or not controller:
        raise RuntimeError("Fleet nodes payload is missing controller")
    nodes: list[FleetNode] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("node_id"), str):
            continue
        policy = entry.get("policy") or {}
        if not isinstance(policy, dict) or policy.get("approved") is not True:
            continue
        route_local = entry.get("node_id") == controller
        nodes.append(FleetNode(node_id=str(entry["node_id"]), local=route_local))
    return nodes


def _snapshot_id(node_id: str) -> str:
    return f"{node_id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"


def fleet_local_bundle_dir(config: VaultConfig, node_id: str, snapshot_id: str) -> Path:
    return config.config_path.parent / "fleet-bundles" / "projection" / node_id / snapshot_id


def _base_snapshot_id(config: VaultConfig, import_name: str) -> str | None:
    state_path = config.paths.import_root / import_name / ".projection-state.json"
    if not state_path.is_file():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    snapshot_id = payload.get("current_snapshot_id") if isinstance(payload, dict) else None
    return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None


def sync_fleet_node(
    config: VaultConfig,
    node_id: str,
    *,
    fleet_command: str,
    instance: Path | None,
    timeout_seconds: float,
) -> FleetSyncResult:
    snapshot_id = _snapshot_id(node_id)
    import_name = node_id
    script, artifact_path = fleet_projection_request(
        node_id,
        snapshot_id=snapshot_id,
        base_snapshot_id=_base_snapshot_id(config, import_name),
    )
    local_bundle_dir = fleet_local_bundle_dir(config, node_id, snapshot_id)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".py") as handle:
        handle.write(script)
        handle.flush()
        command = [fleet_command]
        if instance is not None:
            command.extend(["--instance", str(instance)])
        command.extend(
            [
                "data-job",
                "run",
                node_id,
                "--argv-json",
                json.dumps(["python3", "-"]),
                "--stdin-file",
                handle.name,
                "--timeout-seconds",
                str(int(timeout_seconds)),
                "--artifact-path",
                artifact_path,
                "--artifact-destination",
                str(local_bundle_dir),
            ]
        )
        payload = _run_json(command, timeout_seconds=timeout_seconds + 30)
    status = str(payload.get("status") or "unknown")
    if status != "completed":
        return FleetSyncResult(
            node_id=node_id,
            import_name=import_name,
            status=status,
            payload=payload,
            bundle=None,
        )
    bundle = import_machine_projection(
        config,
        node_id,
        local_bundle_dir,
        import_name=import_name,
    )
    for sibling in local_bundle_dir.parent.iterdir():
        if sibling.is_dir() and sibling != local_bundle_dir:
            shutil.rmtree(sibling)
    return FleetSyncResult(
        node_id=node_id,
        import_name=import_name,
        status="synced",
        payload=payload,
        bundle=bundle,
    )


def sync_fleet(
    config: VaultConfig,
    *,
    fleet_command: str,
    instance: Path | None,
    timeout_seconds: float,
    max_workers: int = 8,
) -> FleetSyncSummary:
    nodes = discover_fleet_nodes(
        fleet_command,
        instance=instance,
        timeout_seconds=min(timeout_seconds, 60),
    )
    remote_nodes = [node for node in nodes if not node.local]
    results_by_node: dict[str, FleetSyncResult] = {}
    if remote_nodes:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(remote_nodes))) as executor:
            futures = {
                executor.submit(
                    sync_fleet_node,
                    config,
                    node.node_id,
                    fleet_command=fleet_command,
                    instance=instance,
                    timeout_seconds=timeout_seconds,
                ): node.node_id
                for node in remote_nodes
            }
            for future in as_completed(futures):
                node_id = futures[future]
                try:
                    results_by_node[node_id] = future.result()
                except Exception as exc:  # noqa: BLE001 - preserve per-node Fleet fan-out results
                    results_by_node[node_id] = FleetSyncResult(
                        node_id=node_id,
                        import_name=node_id,
                        status="sync_failed",
                        payload={"error": f"{type(exc).__name__}: {exc}"},
                        bundle=None,
                    )
    results = tuple(
        FleetSyncResult(
            node_id=node.node_id,
            import_name=node.node_id,
            status="local_projection" if node.local else results_by_node[node.node_id].status,
            payload={} if node.local else results_by_node[node.node_id].payload,
            bundle=None if node.local else results_by_node[node.node_id].bundle,
        )
        for node in nodes
    )
    return FleetSyncSummary(nodes=tuple(nodes), results=results)

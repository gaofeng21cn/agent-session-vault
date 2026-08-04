from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import pytest

from agent_session_vault.config import load_config
from agent_session_vault.fleet import (
    FleetNode,
    FleetSyncResult,
    discover_fleet_nodes,
    fleet_import_name,
    fleet_local_bundle_dir,
    sync_fleet,
)
from agent_session_vault.projection import ProjectionBundle, fleet_projection_request


def _config(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
import_root = "{tmp_path / 'imports'}"
projection_home = "{tmp_path / 'projection-home'}"
local_workspace_extras = "{tmp_path / 'extras'}"
relay_root = "{tmp_path / 'relay'}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return load_config(config_path)


def _bundle(node_id: str, root: Path) -> ProjectionBundle:
    return ProjectionBundle(
        machine_name=node_id,
        snapshot_id=f"{node_id}-snapshot",
        bundle_dir=root,
        manifest_path=root / "manifest.json",
        bundle_path=root / "payload.tar.zst",
        roots_manifest_path=root / "roots-manifest.json",
        inventory_path=root / "inventory.json",
        bundle_bytes=123,
    )


def test_fleet_projection_request_is_self_contained_and_predictable() -> None:
    script, artifact_path = fleet_projection_request(
        "node-a",
        snapshot_id="node-a-snapshot",
        import_name="legacy-node-a",
        base_snapshot_id="node-a-previous",
    )
    compile(script, "<fleet-projection>", "exec")
    assert "ASV_REQUEST_B64" in script
    assert artifact_path == (
        ".local/state/agent-session-vault/fleet-jobs/"
        "projection/node-a/node-a-snapshot"
    )


def test_fleet_import_name_reuses_existing_ssh_target_alias(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[machines.imac]
import_name = "imac"
ssh_target = "gaofeng-imac"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert fleet_import_name(config, "gaofeng-imac") == "imac"
    assert fleet_import_name(config, "gaofeng-new") == "gaofeng-new"
    assert fleet_local_bundle_dir(config, "gaofeng-new", "snapshot") == (
        config.config_path.parent
        / "fleet-bundles"
        / "projection"
        / "gaofeng-new"
        / "snapshot"
    )


def test_discover_fleet_nodes_uses_controller_and_approved_nodes(monkeypatch) -> None:
    payload = {
        "controller": "node-controller",
        "nodes": [
            {"node_id": "node-controller", "policy": {"approved": True}},
            {"node_id": "node-a", "policy": {"approved": True}},
            {"node_id": "node-disabled", "policy": {"approved": False}},
        ],
    }

    class Completed:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ""

    monkeypatch.setattr("agent_session_vault.fleet.subprocess.run", lambda *args, **kwargs: Completed())
    nodes = discover_fleet_nodes("opl-fleet", instance=Path("/instance"))
    assert nodes == [
        FleetNode(node_id="node-controller", local=True),
        FleetNode(node_id="node-a", local=False),
    ]


def test_discover_fleet_nodes_fails_closed_when_controller_is_missing(monkeypatch) -> None:
    class Completed:
        returncode = 0
        stdout = json.dumps({"nodes": []})
        stderr = ""

    monkeypatch.setattr("agent_session_vault.fleet.subprocess.run", lambda *args, **kwargs: Completed())
    with pytest.raises(RuntimeError, match="missing controller"):
        discover_fleet_nodes("opl-fleet", instance=Path("/instance"))


def test_sync_fleet_runs_remote_nodes_concurrently_and_preserves_order(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    nodes = [
        FleetNode(node_id="controller", local=True),
        FleetNode(node_id="node-a", local=False),
        FleetNode(node_id="node-b", local=False),
    ]
    monkeypatch.setattr("agent_session_vault.fleet.discover_fleet_nodes", lambda *args, **kwargs: nodes)
    active = 0
    maximum = 0
    lock = threading.Lock()

    def fake_sync(config, node_id, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return FleetSyncResult(
            node_id=node_id,
            import_name=node_id,
            status="synced",
            payload={"status": "completed"},
            bundle=_bundle(node_id, tmp_path / node_id),
        )

    monkeypatch.setattr("agent_session_vault.fleet.sync_fleet_node", fake_sync)
    result = sync_fleet(
        config,
        fleet_command="opl-fleet",
        instance=None,
        timeout_seconds=60,
    )
    assert maximum == 2
    assert [item.node_id for item in result.results] == ["controller", "node-a", "node-b"]
    assert [item.import_name for item in result.results] == ["controller", "node-a", "node-b"]
    assert [item.status for item in result.results] == ["local_projection", "synced", "synced"]

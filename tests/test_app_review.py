from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_session_vault.app_review import REF, REQUEST_SCHEMA, RESPONSE_SCHEMA, STATUS_REF, read_contribution, review_archive
from agent_session_vault.archive_ops import build_snapshot, init_backend, publish_snapshot, receipt
from agent_session_vault.archive_prune import apply_prune_plan, build_prune_plan, write_prune_plan
from agent_session_vault.archive_restore import build_restore_plan, write_restore_plan
from agent_session_vault.config import load_config
from test_archive_vault import _config, _current_machine_id, _prepare_projection_and_stable, _preview, _write_session


def _fixture(tmp_path):
    config_path, source, backend = _config(tmp_path)
    config = load_config(config_path)
    old = source / "archived_sessions" / "old.jsonl"
    _write_session(old, "session-old", "synthetic source")
    init_backend(config)
    built = build_snapshot(config, machine_id=_current_machine_id(config))
    publish_snapshot(config, built.staging_root)
    _prepare_projection_and_stable(config, old)
    plan = build_prune_plan(config, now=datetime(2026, 9, 20, tzinfo=UTC), preview_runner=_preview)
    path = write_prune_plan(plan, tmp_path / "plan.json")
    return config, old, backend, plan, path


def _tree_bytes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()}


def test_plan_review_is_same_owner_and_never_writes_on_reopen(tmp_path):
    config, old, _, plan, path = _fixture(tmp_path)
    before = _tree_bytes(tmp_path)
    request = {"schema_version": REQUEST_SCHEMA, "operation": "read", "ref": REF,
               "input": {"config_path": str(config.config_path), "plan_path": str(path), "expected_revision": plan.plan_digest}}
    response = read_contribution(request)
    read_contribution(request)
    item = response["result"]["data"]["items"][1]
    assert item["id"] == plan.plan_id
    assert item["revision"] == plan.plan_digest
    assert item["status"] == "reviewable"
    assert item["sources"][0]["revision"] == plan.entries[0].sha256
    assert item["checks"]["snapshots"][0]["status"] == "verified"
    assert item["checks"]["tokscale_preview"] == "rechecked_by_apply"
    assert not item["execution_available"]
    assert old.exists()
    assert _tree_bytes(tmp_path) == before


def test_stale_source_detected_and_existing_apply_refuses(tmp_path):
    config, old, _, plan, path = _fixture(tmp_path)
    old.write_text("changed synthetic source")
    item = review_archive(config, plan_path=path)["data"]["items"][1]
    assert item["status"] == "stale"
    assert item["sources"][0]["status"] == "changed_or_missing"
    with pytest.raises(ValueError, match="source metadata changed"):
        apply_prune_plan(config, plan, preview_runner=_preview)
    assert old.exists()


def test_revision_and_tampered_plan_rejected(tmp_path):
    config, _, _, _, path = _fixture(tmp_path)
    with pytest.raises(ValueError, match="revision changed"):
        review_archive(config, plan_path=path, expected_revision="different")
    value = json.loads(path.read_text())
    value["cold_age_days"] += 1
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="digest mismatch"):
        review_archive(config, plan_path=path)


@pytest.mark.parametrize("outcome", ["partial", "failed", "unknown"])
def test_partial_receipt_is_reconciled_without_retry_or_inferred_deletion(tmp_path, outcome):
    config, old, backend, plan, path = _fixture(tmp_path)
    partial = receipt(config, "archive-prune", outcome, machine_id=plan.machine_id,
                      details={"plan_id": plan.plan_id, "plan_digest": plan.plan_digest, "deleted_file_count": 1})
    before = _tree_bytes(tmp_path)
    result = review_archive(config, plan_path=path)
    plan_item, receipt_item = result["data"]["items"][1:]
    assert plan_item["sources"][0]["status"] == "current"
    assert receipt_item["status"] == outcome
    assert receipt_item["id"] == partial.operation_id
    assert not receipt_item["retry_available"]
    assert old.exists()
    assert _tree_bytes(tmp_path) == before
    receipt_path = backend / "receipts" / plan.machine_id / f"{partial.operation_id}.json"
    raw = json.loads(receipt_path.read_text())
    raw["details"]["plan_digest"] = "other"
    receipt_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="does not belong"):
        review_archive(config, plan_path=path, receipt_path=receipt_path)


def test_restore_review_preserves_staging_destination_and_cli_json(tmp_path, capsys):
    from agent_session_vault.cli import main
    config, _, _, _, _ = _fixture(tmp_path)
    plan = build_restore_plan(config, destination=tmp_path / "restored")
    path = write_restore_plan(plan, tmp_path / "restore.json")
    before = _tree_bytes(tmp_path)
    assert main(["--config", str(config.config_path), "archive", "review", "--plan", str(path), "--json"]) == 0
    item = json.loads(capsys.readouterr().out)["data"]["items"][1]
    assert item["revision"] == plan.plan_digest
    assert item["plan"]["destination"] == str(tmp_path / "restored")
    assert _tree_bytes(tmp_path) == before
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("ref", [STATUS_REF, REF])
def test_manifest_contribution_handler_runs_read_only_owner(tmp_path, monkeypatch, ref):
    config_path, _, _ = _config(tmp_path)
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "opl-package.json").read_text())
    handler = manifest["provides"]["contribution_handler"]
    request = {
        "schema_version": REQUEST_SCHEMA,
        "package_id": manifest["identity"]["id"],
        "operation": "read",
        "ref": ref,
        "input": {"config_path": str(config_path)} if ref == REF else {},
    }
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent), prepend=os.pathsep)
    completed = subprocess.run(
        handler["argv"], cwd=root, input=json.dumps(request), capture_output=True, text=True, check=True,
    )
    response = json.loads(completed.stdout)
    assert response["schema_version"] == RESPONSE_SCHEMA
    assert response["ok"]
    assert response["operation"] == "read"
    assert response["ref"] == ref
    assert response["result"]["data"]["read_only"]
    with pytest.raises(ValueError, match="unsupported"):
        read_contribution({**request, "operation": "execute"})


def test_v2_manifest_declares_vault_resources_and_read_only_views():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "opl-package.json").read_text())
    assert manifest["schema_version"] == "opl.package.v2"
    identity = manifest["identity"]
    assert identity["id"] == "agent-session-vault"
    assert identity["intent"] == "extension"
    assert (root / identity["icon"]).is_file()
    assert manifest["dependencies"]["opl_base"] == "^2.0.0"
    assert manifest["distribution"] == {
        "oci": "ghcr.io/gaofeng21cn/one-person-lab-packages/agent-session-vault",
        "visibility": "public",
    }
    assert manifest["host_hints"]["codex"]["sync_enabled"] is False

    provides = manifest["provides"]
    assert provides["module_ids"] == ["agent.session.vault.v1"]
    assert provides["contribution_handler"] == {
        "kind": "cli",
        "argv": ["python3", "./app_contribution.py"],
        "transport": "stdin_json_stdout_json",
    }
    assert provides["skills"] == [{
        "id": "agent-session-vault",
        "path": "./skills/agent-session-vault",
        "exposure": "default",
    }]
    assert (root / provides["skills"][0]["path"] / "SKILL.md").is_file()

    declarative = manifest["ui"]["declarative"]
    assert not declarative.get("settings_forms")
    views = declarative["views"]
    assert [(view["id"], view["view_type"], view["data_ref"], view["sort_order"]) for view in views] == [
        ("vault.runtime.status", "service_status", STATUS_REF, 690),
        ("vault.archive.review", "list_detail", REF, 700),
    ]
    for view in views:
        assert view["slot"] == "settings.section"
        assert view["settings_destination"] == "services"
        assert not view.get("actions")


def test_installed_package_cli_does_not_require_a_separate_python_tool(tmp_path):
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run([sys.executable, str(root / "vault.py"), "--help"],
                               cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "agent-session-vault" in completed.stdout
    assert "archive" in completed.stdout
    assert "sync" in completed.stdout


def test_runtime_status_separates_feature_dependencies_without_user_data(monkeypatch):
    import agent_session_vault.app_review as owner
    monkeypatch.setattr(owner.shutil, "which", lambda command: None)
    monkeypatch.setattr(owner, "load_config", lambda *_: pytest.fail("status must not read configuration"))
    request = {"schema_version": REQUEST_SCHEMA, "operation": "read", "ref": owner.STATUS_REF}
    response = read_contribution(request)
    data = response["result"]["data"]
    assert data["runtime_ready"]
    assert data["business_configuration"] == "not_checked"
    assert {item["id"]: item["status"] for item in data["items"]} == {
        "python": "ready", "fleet": "missing", "tokscale": "missing",
    }
    assert response["result"]["state"] == "attention"
    with pytest.raises(ValueError, match="no input"):
        read_contribution({**request, "input": {"config_path": "/private/ignored"}})

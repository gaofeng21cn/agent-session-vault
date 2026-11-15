from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agent_session_vault import archive_maintenance as maintenance
from agent_session_vault.archive_prune import PrunePlan, _plan_digest, prune_plan_payload, write_prune_plan


def stage(run_dir, name, payload, code=0):
    maintenance.write_json(run_dir / f"{name}.launch.json", {"command": [sys.executable, "-c", "pass"]})
    maintenance.write_json(run_dir / f"{name}.stdout.json", payload)
    (run_dir / f"{name}.exit_code").write_text(f"{code}\n")


def empty_plan(now=None):
    draft = PrunePlan(plan_id="test-plan", created_at=(now or datetime.now(UTC)).isoformat(),
                      machine_id="test-machine", cold_age_days=30, stable_root="/stable",
                      imports_fingerprint="fingerprint", tokscale_preview={"tokens": 42},
                      entries=(), skipped={}, plan_digest="")
    return replace(draft, plan_digest=_plan_digest(draft))


def test_sessions_create_new_immutable_plan_and_skip_only_empty_apply(environment, monkeypatch):
    config, run_dir = environment
    monkeypatch.setattr(maintenance, "preflight", lambda _: {"status": "ready"})
    calls = []
    def execute(_, directory, name, arguments):
        calls.append((name, arguments))
        if name == "archive-cycle":
            payload = {"status": "verified", "snapshot_ids": ["fresh-snapshot"]}
        elif name == "prune-plan":
            plan = empty_plan()
            write_prune_plan(plan, Path(arguments[-1]))
            payload = prune_plan_payload(plan)
        else:
            pytest.fail("empty plan must not launch prune-apply")
        stage(directory, name, payload)
        return maintenance.stage_status(directory, name)
    monkeypatch.setattr(maintenance, "_stage", execute)
    result = maintenance.run_sessions(config, run_dir)
    assert [name for name, _ in calls] == ["archive-cycle", "prune-plan"]
    assert "--no-due-only" in calls[0][1] and "--deep" in calls[0][1]
    assert result["status"] == "session_verified"
    assert result["eligible_file_count"] == 0 and result["prune"] is None
    assert result["tokscale_after"] is None
    assert (run_dir / "prune-plan.json").is_file()
    # A second execution of this run cannot launch another archive command.
    with pytest.raises(FileExistsError):
        maintenance.run_sessions(config, run_dir)
    assert len(calls) == 2


@pytest.mark.parametrize("status,code", [("failed", 1), ("partial", 0), ("pending", 0), ("busy", 0), ("not_due", 0)])
def test_sessions_stop_before_plan_for_any_unverified_cycle(environment, monkeypatch, status, code):
    config, run_dir = environment
    monkeypatch.setattr(maintenance, "preflight", lambda _: {})
    calls = []
    def execute(_, directory, name, arguments):
        calls.append(name)
        stage(directory, name, {"status": status}, code)
        return maintenance.stage_status(directory, name)
    monkeypatch.setattr(maintenance, "_stage", execute)
    with pytest.raises(ValueError):
        maintenance.run_sessions(config, run_dir)
    assert calls == ["archive-cycle"]


def test_stale_prune_plan_is_never_applied(environment, monkeypatch):
    config, run_dir = environment
    monkeypatch.setattr(maintenance, "preflight", lambda _: {})
    def execute(_, directory, name, arguments):
        if name == "archive-cycle":
            payload = {"status": "verified"}
        else:
            assert name == "prune-plan"
            plan = empty_plan(datetime.now(UTC) - timedelta(days=1))
            write_prune_plan(plan, Path(arguments[-1]))
            payload = prune_plan_payload(plan)
        stage(directory, name, payload)
        return maintenance.stage_status(directory, name)
    monkeypatch.setattr(maintenance, "_stage", execute)
    with pytest.raises(ValueError, match="stale"):
        maintenance.run_sessions(config, run_dir)
    assert not (run_dir / "prune-plan.json").exists()


def test_missing_exit_code_cannot_be_inferred_from_receipt(environment):
    config, run_dir = environment
    maintenance.write_json(run_dir / "archive-cycle.launch.json", {"command": ["/nonexistent"]})
    maintenance.write_json(run_dir / "archive-cycle.stdout.json", {"status": "verified"})
    maintenance.write_json(run_dir / "session-result.json", {"status": "session_verified"})
    with pytest.raises(ValueError, match="real exit code"):
        maintenance.require_stage(run_dir, "archive-cycle", "verified")
    assert maintenance.maintenance_status(config, run_dir)["stages"][1]["exit_code"] is None


def test_unknown_launch_reservation_blocks_second_maintenance(environment, monkeypatch):
    config, run_dir = environment
    monkeypatch.setattr(maintenance, "preflight", lambda _: {})
    maintenance.write_json(run_dir.parent / "latest-launch.json", {"status": "starting", "run_dir": str(run_dir)})
    assert maintenance.maintenance_status(config)["status"] == "unknown"
    with pytest.raises(ValueError, match="incomplete"):
        maintenance.start_maintenance(config)
    assert not (run_dir / "archive-maintenance.launch.json").exists()


def test_preflight_failure_precedes_launch_state_writes(environment, monkeypatch):
    config, run_dir = environment
    def blocked(_):
        raise ValueError("NAS unavailable")
    monkeypatch.setattr(maintenance, "preflight", blocked)
    with pytest.raises(ValueError, match="NAS unavailable"):
        maintenance.start_maintenance(config)
    assert not (run_dir.parent / "latest-launch.json").exists()
    assert not (run_dir.parent / "launch.lock").exists()


def test_status_does_not_write_or_launch(environment, monkeypatch):
    config, run_dir = environment
    def forbidden(*args, **kwargs):
        pytest.fail("status must not write or run NAS preflight")
    monkeypatch.setattr(maintenance, "preflight", forbidden)
    monkeypatch.setattr(maintenance, "write_json", forbidden)
    assert maintenance.maintenance_status(config)["status"] == "no_launch_record"
    assert maintenance.maintenance_status(config, run_dir)["status"] == "not_started"


def test_detached_launcher_returns_early_runs_once_and_records_actual_exit(tmp_path):
    launcher = Path(__file__).resolve().parents[1] / "scripts/run_detached.py"
    run_dir = tmp_path / "detached"
    marker = tmp_path / "calls"
    program = f"import time,json; from pathlib import Path; Path({str(marker)!r}).open('a').write('once\\n'); time.sleep(0.5); print(json.dumps({{'status':'test'}})); raise SystemExit(7)"
    command = [sys.executable, str(launcher), "--log-dir", str(run_dir), "--name", "child", "--", sys.executable, "-c", program]
    launch = subprocess.run(command, capture_output=True, text=True, timeout=5)
    assert launch.returncode == 0 and json.loads(launch.stdout)["status"] == "started"
    assert not (run_dir / "child.exit_code").exists()
    deadline = time.monotonic() + 5
    while not (run_dir / "child.exit_code").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (run_dir / "child.exit_code").read_text().strip() == "7"
    duplicate = subprocess.run(command, capture_output=True, text=True, timeout=5)
    assert duplicate.returncode == 75
    assert marker.read_text() == "once\n"


def test_supervisor_start_failure_does_not_fabricate_business_exit(tmp_path):
    launcher = Path(__file__).resolve().parents[1] / "scripts/run_detached.py"
    result = subprocess.run([sys.executable, str(launcher), "--log-dir", str(tmp_path), "--name", "child", "--", "/nonexistent-business-command"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert not (tmp_path / "child.exit_code").exists()
    assert json.loads((tmp_path / "child.supervisor.json").read_text())["status"] == "unknown"


def test_detached_maintenance_reconciles_original_real_stage_exit_codes(environment):
    """Exercise the actual CLI/launcher/worker boundary with tiny fake business stages."""
    config, unused = environment
    repository = Path(__file__).resolve().parents[1]
    local = maintenance.local_root(config)
    binaries = config.paths.home / ".local/bin"
    binaries.mkdir(parents=True)
    (binaries / "python3").symlink_to(sys.executable)
    (local / "run-detached.py").write_bytes((repository / "scripts/run_detached.py").read_bytes())
    (local / "nas-preflight.py").write_text("import json; print(json.dumps(" + repr(
        {"status": "ready", "root": str(config.archive.root.parent)}) + "))\n")
    cli = binaries / "agent-session-vault"
    cli.write_text(f'''#!{sys.executable}
import sys, json
from pathlib import Path
sys.path.insert(0, {str(repository / 'src')!r})
from datetime import UTC, datetime
from dataclasses import replace
from agent_session_vault.archive_prune import PrunePlan, _plan_digest, write_prune_plan, prune_plan_payload
if "archive-cycle" in sys.argv:
    print(json.dumps({{"status":"verified","snapshot_ids":["sandbox-snapshot"]}}))
elif "prune-plan" in sys.argv:
    plan=PrunePlan("sandbox-plan",datetime.now(UTC).isoformat(),"sandbox-machine",30,"/stable","fingerprint",{{"tokens":42}},(),{{}},"")
    plan=replace(plan,plan_digest=_plan_digest(plan))
    write_prune_plan(plan,Path(sys.argv[sys.argv.index("--plan-path")+1]))
    print(json.dumps(prune_plan_payload(plan)))
else:
    from agent_session_vault.cli import main
    raise SystemExit(main())
''')
    cli.chmod(0o700)
    launched = maintenance.start_maintenance(config)
    assert launched["status"] == "started"
    run_dir = Path(launched["run_dir"])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = maintenance.maintenance_status(config)
        if status["stages"][0]["exit_code"] is not None:
            break
        time.sleep(0.1)
    assert status["status"] == "awaiting_cold_stages", status
    assert [item["exit_code"] for item in status["stages"][:3]] == [0, 0, 0]
    assert status["stages"][3]["status"] == "not_started"
    assert maintenance.session_gate(config, run_dir)["plan_id"] == "sandbox-plan"
    with pytest.raises(ValueError, match="incomplete"):
        maintenance.start_maintenance(config)
    assert len(list(run_dir.glob("archive-cycle.launch.json"))) == 1

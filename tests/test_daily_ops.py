from __future__ import annotations

import fcntl
import json
from pathlib import Path

import pytest

from agent_session_vault.cli import main
from agent_session_vault.config import load_config
from agent_session_vault.daily_ops import CommandResult, DailyTokscaleResult, run_daily_tokscale
from agent_session_vault.fleet import FleetNode, FleetSyncResult, FleetSyncSummary
from agent_session_vault.projection import ProjectionBundle


HELP_OUTPUT = """Submit usage data

Options:
  -c, --client <CLIENTS>  Filter by client(s)
      --dry-run           Show what would be submitted
"""

STATS_OUTPUT = """Tokscale - Submit Usage Data

Data to submit:
  Date range: 2026-01-13 to 2026-07-14
  Active days: 159
  Total tokens: 225,000,000,001
  Total cost: $193600.25
  Clients: codex, gemini, openclaw, antigravity, zcode
  Models: 14 models
"""


def _write_config(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    session = home / ".codex" / "sessions" / "local.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text('{"type":"event_msg","payload":{"type":"token_count","total":1}}\n', encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
import_root = "{tmp_path / 'imports'}"
projection_home = "{tmp_path / 'projection-home'}"
local_workspace_extras = "{tmp_path / 'extras'}"
stable_root = "{tmp_path / 'stable'}"

[archive]
root = "{tmp_path / 'archive'}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _bundle(tmp_path: Path) -> ProjectionBundle:
    bundle_dir = tmp_path / "bundle"
    return ProjectionBundle(
        machine_name="node-a",
        snapshot_id="node-a-snapshot",
        bundle_dir=bundle_dir,
        manifest_path=bundle_dir / "manifest.json",
        bundle_path=bundle_dir / "payload.tar.zst",
        roots_manifest_path=bundle_dir / "roots-manifest.json",
        inventory_path=bundle_dir / "inventory.json",
        bundle_bytes=1234,
        mode="projection_delta",
        base_snapshot_id="node-a-previous",
        state_status="incremental",
        files_seen=12,
        files_projected=2,
        files_reused=10,
    )


def _install_fleet_fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, create_remote_root: bool = True) -> None:
    if create_remote_root:
        (tmp_path / "imports" / "node-a" / ".raw" / "codex").mkdir(parents=True)
    summary = FleetSyncSummary(
        nodes=(FleetNode(node_id="controller", local=True), FleetNode(node_id="node-a", local=False)),
        results=(
            FleetSyncResult(
                node_id="controller",
                import_name="controller",
                status="local_projection",
                payload={},
                bundle=None,
            ),
            FleetSyncResult(
                node_id="node-a",
                import_name="node-a",
                status="synced",
                payload={"status": "completed"},
                bundle=_bundle(tmp_path),
            ),
        ),
    )
    monkeypatch.setattr("agent_session_vault.daily_ops.sync_fleet", lambda *args, **kwargs: summary)


def _install_command_fake(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]]) -> None:
    def fake_run(
        command: list[str],
        *,
        env: dict[str, str] | None,
        log_path: Path,
        timeout_seconds: float | None,
        on_pid,
    ) -> CommandResult:
        calls.append(command)
        if command[:3] == ["npm", "view", "tokscale"]:
            output = "4.5.2\n"
        elif "antigravity" in command:
            output = "Antigravity sync\ndetected connections: 0\ncached sessions after sync: 0\n"
        elif "--help" in command:
            output = HELP_OUTPUT
        elif "--dry-run" in command:
            output = STATS_OUTPUT + "\nDry run - not submitting data.\n"
        else:
            output = STATS_OUTPUT + "\nSuccessfully submitted!\nView your profile: https://tokscale.ai/u/test\n"
        log_path.write_text(output, encoding="utf-8")
        on_pid(4321)
        on_pid(None)
        return CommandResult(returncode=0, output=output, duration_seconds=0.1, pid=4321)

    monkeypatch.setattr("agent_session_vault.daily_ops._run_logged_command", fake_run)


def _write_cached_contract(run_root: Path) -> None:
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "submit-contract.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tokscale_version": "4.5.2",
                "clients": ["codex", "gemini", "openclaw", "antigravity", "zcode"],
                "client_args": ["-c", "codex,gemini,openclaw,antigravity,zcode"],
                "dry_run": True,
                "verified_at": "2026-07-14T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_daily_tokscale_cached_contract_runs_one_submit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(_write_config(tmp_path))
    run_root = tmp_path / "ops"
    calls: list[list[str]] = []
    _write_cached_contract(run_root)
    _install_fleet_fake(monkeypatch, tmp_path)
    _install_command_fake(monkeypatch, calls)

    result = run_daily_tokscale(config, run_root=run_root)

    assert result.exit_code == 0
    assert result.payload["status"] == "confirmed"
    assert result.payload["machine_source"] == "fleet"
    assert result.payload["projection_env"]["status"] == "valid"
    assert result.payload["source_sync"]["antigravity"]["status"] == "skipped_unavailable"
    assert result.payload["tokscale"]["contract_checked"] is False
    submit_calls = [command for command in calls if "submit" in command]
    assert len(submit_calls) == 1
    assert "--dry-run" not in submit_calls[0]
    assert calls[0][:3] == ["npm", "view", "tokscale"]
    assert "antigravity" in calls[1]
    assert Path(result.payload["receipt_path"]).is_file()


def test_daily_tokscale_new_version_checks_help_and_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(_write_config(tmp_path))
    calls: list[list[str]] = []
    _install_fleet_fake(monkeypatch, tmp_path)
    _install_command_fake(monkeypatch, calls)

    result = run_daily_tokscale(config, run_root=tmp_path / "ops")

    assert result.exit_code == 0
    assert result.payload["tokscale"]["contract_checked"] is True
    assert result.payload["tokscale"]["preview_ran"] is True
    assert any("--help" in command for command in calls)
    assert any("--dry-run" in command for command in calls)


def test_daily_tokscale_preserves_submit_when_stable_mirror_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(_write_config(tmp_path))
    run_root = tmp_path / "ops"
    calls: list[list[str]] = []
    _write_cached_contract(run_root)
    _install_fleet_fake(monkeypatch, tmp_path)
    _install_command_fake(monkeypatch, calls)
    monkeypatch.setattr(
        "agent_session_vault.daily_ops.mirror_stable_layer",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stable root unavailable")),
    )

    result = run_daily_tokscale(config, run_root=run_root, mirror_stable=True)

    assert result.exit_code == 0
    assert result.payload["status"] == "confirmed"
    assert result.payload["stable_mirror"]["status"] == "failed"
    assert result.payload["warnings"] == ["stable_mirror_failed"]


def test_daily_tokscale_rejects_missing_synced_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(_write_config(tmp_path))
    calls: list[list[str]] = []
    _install_fleet_fake(monkeypatch, tmp_path, create_remote_root=False)
    _install_command_fake(monkeypatch, calls)

    result = run_daily_tokscale(config, run_root=tmp_path / "ops")

    assert result.exit_code == 1
    assert result.payload["error"]["phase"] == "projection_env"
    assert not any("submit" in command for command in calls)


def test_cli_daily_tokscale_passes_fleet_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    config_path = _write_config(tmp_path)
    captured: dict[str, object] = {}

    def fake_daily(config, **kwargs) -> DailyTokscaleResult:
        captured.update(kwargs)
        return DailyTokscaleResult(payload={"status": "confirmed"}, exit_code=0)

    monkeypatch.setattr("agent_session_vault.cli.run_daily_tokscale", fake_daily)
    exit_code = main(
        [
            "--config",
            str(config_path),
            "ops",
            "daily-tokscale",
            "--fleet-command",
            "opl-fleet-test",
            "--mirror-stable",
            "--json",
        ]
    )

    assert exit_code == 0
    assert captured["fleet_command"] == "opl-fleet-test"
    assert captured["mirror_stable"] is True
    assert json.loads(capsys.readouterr().out)["status"] == "confirmed"


def test_daily_tokscale_rejects_concurrent_run(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    run_root = tmp_path / "ops"
    run_root.mkdir()

    with (run_root / "run.lock").open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_daily_tokscale(config, run_root=run_root)

    assert result.exit_code == 2
    assert result.payload["status"] == "already_running"

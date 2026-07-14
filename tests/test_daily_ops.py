from __future__ import annotations

import fcntl
import json
from pathlib import Path

import pytest

from agent_session_vault.cli import main
from agent_session_vault.config import load_config
from agent_session_vault.daily_ops import CommandResult, DailyTokscaleResult, run_daily_tokscale
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
  Clients: codex, gemini, openclaw
  Models: 14 models
"""


def _write_config(tmp_path: Path, *, create_raw_roots: bool = True) -> Path:
    home = tmp_path / "home"
    imports = tmp_path / "imports"
    extras = tmp_path / "extras"
    relay = tmp_path / "relay"
    home.mkdir()
    if create_raw_roots:
        (imports / "machine-a" / ".raw" / "codex").mkdir(parents=True)
        (imports / "machine-b" / ".raw" / "codex").mkdir(parents=True)
        (extras / "managed" / "codex").mkdir(parents=True)
        (extras / "managed" / "sync-state.json").write_text("{}\n", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{home}"
workspace_root = "{tmp_path / 'workspace'}"
import_root = "{imports}"
shadow_home = "{tmp_path / 'shadow-home'}"
local_workspace_extras = "{extras}"
archive_root = "{tmp_path / 'archive'}"
relay_root = "{relay}"

[machines.machine-a]
import_name = "machine-a"
ssh_target = "session-sync-a"
source_home = "/remote/home"
remote_relay_root = "/remote/relay"
clients = ["codex"]

[machines.machine-b]
import_name = "machine-b"
ssh_target = "session-sync-b"
source_home = "/remote/home"
remote_relay_root = "/remote/relay"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _bundle(machine_name: str, snapshot_id: str, bundle_dir: Path) -> ProjectionBundle:
    return ProjectionBundle(
        machine_name=machine_name,
        snapshot_id=snapshot_id,
        bundle_dir=bundle_dir,
        manifest_path=bundle_dir / "manifest.json",
        bundle_path=bundle_dir / "payload.tar.zst",
        roots_manifest_path=bundle_dir / "roots-manifest.json",
        inventory_path=bundle_dir / "inventory.json",
        bundle_bytes=1234,
        mode="projection_delta",
        base_snapshot_id=f"{machine_name}-previous",
        state_status="incremental",
        files_seen=12,
        files_projected=2,
        files_reused=10,
    )


def _install_sync_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exported: dict[str, ProjectionBundle] = {}

    def fake_probe(machine, timeout_seconds: float) -> dict[str, object]:
        return {"status": "available", "duration_seconds": 0.01, "exit_code": 0, "reason": None}

    def fake_export(*, machine, **kwargs) -> ProjectionBundle:
        snapshot_id = f"{machine.name}-20260714T000000000000Z"
        bundle = _bundle(machine.name, snapshot_id, Path("/remote/relay") / machine.name / snapshot_id)
        exported[machine.name] = bundle
        return bundle

    def fake_fetch(*, local_bundle_dir: Path, **kwargs) -> Path:
        local_bundle_dir.mkdir(parents=True, exist_ok=True)
        return local_bundle_dir

    def fake_import(*, machine_name: str, bundle_dir: Path, **kwargs) -> ProjectionBundle:
        source = exported[machine_name]
        return _bundle(machine_name, source.snapshot_id, bundle_dir)

    monkeypatch.setattr("agent_session_vault.daily_ops._probe_machine", fake_probe)
    monkeypatch.setattr("agent_session_vault.daily_ops.export_machine_projection_ssh", fake_export)
    monkeypatch.setattr("agent_session_vault.daily_ops.fetch_projection_bundle_ssh", fake_fetch)
    monkeypatch.setattr("agent_session_vault.daily_ops.import_machine_projection", fake_import)


def _install_command_fake(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[list[str]],
) -> None:
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
                "clients": ["codex", "gemini", "openclaw"],
                "client_args": ["-c", "codex,gemini,openclaw"],
                "dry_run": True,
                "verified_at": "2026-07-14T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_daily_tokscale_cached_contract_runs_one_real_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(_write_config(tmp_path))
    run_root = tmp_path / "ops"
    calls: list[list[str]] = []
    _write_cached_contract(run_root)
    _install_sync_fakes(monkeypatch, tmp_path)
    _install_command_fake(monkeypatch, calls)

    result = run_daily_tokscale(config, run_root=run_root, canonicalize_command=None)

    assert result.exit_code == 0
    assert result.payload["status"] == "confirmed"
    assert result.payload["tokscale"]["contract_checked"] is False
    assert result.payload["tokscale"]["preview_ran"] is False
    assert result.payload["tokscale"]["statistics"]["total_tokens"] == 225_000_000_001
    submit_calls = [command for command in calls if "submit" in command]
    assert len(submit_calls) == 1
    assert "--dry-run" not in submit_calls[0]
    assert Path(result.payload["receipt_path"]).is_file()


def test_daily_tokscale_new_version_checks_help_and_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(_write_config(tmp_path))
    run_root = tmp_path / "ops"
    calls: list[list[str]] = []
    _install_sync_fakes(monkeypatch, tmp_path)
    _install_command_fake(monkeypatch, calls)

    result = run_daily_tokscale(config, run_root=run_root, canonicalize_command=None)

    assert result.exit_code == 0
    assert result.payload["tokscale"]["contract_checked"] is True
    assert result.payload["tokscale"]["preview_ran"] is True
    assert result.payload["tokscale"]["numeric_source"] == "submit"
    assert any("--help" in command for command in calls)
    assert any("--dry-run" in command for command in calls)
    contract = json.loads((run_root / "submit-contract.json").read_text(encoding="utf-8"))
    assert contract["tokscale_version"] == "4.5.2"


def test_daily_tokscale_can_mirror_analytics_state_after_confirmed_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(_write_config(tmp_path))
    run_root = tmp_path / "ops"
    calls: list[list[str]] = []
    _write_cached_contract(run_root)
    _install_sync_fakes(monkeypatch, tmp_path)
    _install_command_fake(monkeypatch, calls)

    result = run_daily_tokscale(config, run_root=run_root, mirror_stable=True)

    assert result.exit_code == 0
    assert result.payload["status"] == "confirmed"
    assert result.payload["stable_mirror"]["status"] == "verified"
    assert result.payload["stable_mirror"]["profile"] == "analytics"


def test_daily_tokscale_preserves_confirmed_submit_when_stable_mirror_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(_write_config(tmp_path))
    run_root = tmp_path / "ops"
    calls: list[list[str]] = []
    _write_cached_contract(run_root)
    _install_sync_fakes(monkeypatch, tmp_path)
    _install_command_fake(monkeypatch, calls)
    monkeypatch.setattr(
        "agent_session_vault.daily_ops.mirror_stable_layer",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cloud unavailable")),
    )

    result = run_daily_tokscale(config, run_root=run_root, mirror_stable=True)

    assert result.exit_code == 0
    assert result.payload["status"] == "confirmed"
    assert result.payload["stable_mirror"]["status"] == "failed"
    assert result.payload["warnings"] == ["stable_mirror_failed"]


def test_daily_tokscale_skips_unavailable_remote_without_blocking_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(_write_config(tmp_path))
    run_root = tmp_path / "ops"
    calls: list[list[str]] = []
    _write_cached_contract(run_root)
    _install_sync_fakes(monkeypatch, tmp_path)
    _install_command_fake(monkeypatch, calls)

    def fake_probe(machine, timeout_seconds: float) -> dict[str, object]:
        status = "skipped_unavailable" if machine.name == "machine-b" else "available"
        return {"status": status, "duration_seconds": 0.01, "reason": None}

    monkeypatch.setattr("agent_session_vault.daily_ops._probe_machine", fake_probe)

    result = run_daily_tokscale(config, run_root=run_root, canonicalize_command=None)

    assert result.exit_code == 0
    remotes = {item["machine"]: item for item in result.payload["remotes"]}
    assert remotes["machine-a"]["status"] == "synced"
    assert remotes["machine-a"]["bundle"]["projection_state"] == {
        "status": "incremental",
        "files_seen": 12,
        "files_projected": 2,
        "files_reused": 10,
    }
    assert remotes["machine-b"]["status"] == "skipped_unavailable"
    assert result.payload["tokscale"]["submit_status"] == "confirmed"


def test_daily_tokscale_invalid_raw_view_blocks_submit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(_write_config(tmp_path, create_raw_roots=False))
    calls: list[list[str]] = []

    monkeypatch.setattr(
        "agent_session_vault.daily_ops._probe_machine",
        lambda machine, timeout_seconds: {
            "status": "skipped_unavailable",
            "duration_seconds": 0.01,
            "reason": "offline",
        },
    )
    _install_command_fake(monkeypatch, calls)

    result = run_daily_tokscale(config, run_root=tmp_path / "ops", canonicalize_command=None)

    assert result.exit_code == 1
    assert result.payload["status"] == "failed"
    assert result.payload["error"]["phase"] == "raw_env"
    assert calls == []


def test_cli_daily_tokscale_emits_runner_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
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
            "--machine",
            "machine-a",
            "--mirror-stable",
            "--json",
        ]
    )

    assert exit_code == 0
    assert captured["machine_names"] == ["machine-a"]
    assert captured["canonicalize_command"] is None
    assert captured["mirror_stable"] is True
    assert json.loads(capsys.readouterr().out) == {"status": "confirmed"}


def test_npm_latest_parser_ignores_non_version_log_lines() -> None:
    from agent_session_vault.daily_ops import _parse_latest_version

    assert _parse_latest_version("npm notice before\n4.5.2\nnpm notice after\n") == "4.5.2"


def test_daily_tokscale_rejects_concurrent_run(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    run_root = tmp_path / "ops"
    run_root.mkdir()

    with (run_root / "run.lock").open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_daily_tokscale(config, run_root=run_root, canonicalize_command=None)

    assert result.exit_code == 2
    assert result.payload["status"] == "already_running"
    assert result.payload["error"]["type"] == "ConcurrentRun"

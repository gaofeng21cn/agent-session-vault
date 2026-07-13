from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Callable

from .config import MachineConfig, VaultConfig
from .projection import (
    ProjectionBundle,
    expected_local_projection_bundle_dir,
    export_machine_projection_ssh,
    fetch_projection_bundle_ssh,
    import_machine_projection,
)
from .tokscale import build_tokscale_invocation
from .views import build_view


DEFAULT_CLIENTS = ("codex", "gemini", "openclaw")
SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=8",
    "-o",
    "ConnectionAttempts=1",
    "-o",
    "ServerAliveInterval=2",
    "-o",
    "ServerAliveCountMax=1",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str
    duration_seconds: float
    pid: int | None


@dataclass(frozen=True)
class DailyTokscaleResult:
    payload: dict[str, object]
    exit_code: int


class DailyTokscaleError(RuntimeError):
    def __init__(self, phase: str, message: str) -> None:
        super().__init__(message)
        self.phase = phase


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp() -> str:
    return _utc_now().isoformat()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _terminate_process_group(process: subprocess.Popen[object]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _run_logged_command(
    command: list[str],
    *,
    env: dict[str, str] | None,
    log_path: Path,
    timeout_seconds: float | None,
    on_pid: Callable[[int | None], None],
) -> CommandResult:
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen[object] | None = None
    try:
        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                command,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            on_pid(process.pid)
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                _terminate_process_group(process)
                log_file.write(f"\nagent-session-vault: command timed out after {timeout_seconds} seconds\n")
                returncode = 124
    except FileNotFoundError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        returncode = 127
    finally:
        on_pid(None)
    return CommandResult(
        returncode=returncode,
        output=log_path.read_text(encoding="utf-8"),
        duration_seconds=round(time.monotonic() - started, 3),
        pid=process.pid if process else None,
    )


def _probe_machine(machine: MachineConfig, timeout_seconds: float) -> dict[str, object]:
    started = time.monotonic()
    if not machine.ssh_target:
        return {
            "status": "skipped_unavailable",
            "duration_seconds": 0.0,
            "reason": "missing_ssh_target",
        }
    command = ["ssh", *SSH_OPTIONS, machine.ssh_target, "printf ready"]
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        available = completed.returncode == 0 and completed.stdout.strip() == "ready"
        reason = None if available else (completed.stderr.strip() or completed.stdout.strip() or "probe_failed")
        return {
            "status": "available" if available else "skipped_unavailable",
            "duration_seconds": round(time.monotonic() - started, 3),
            "exit_code": completed.returncode,
            "reason": reason,
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "skipped_unavailable",
            "duration_seconds": round(time.monotonic() - started, 3),
            "reason": "probe_timeout",
        }
    except OSError as exc:
        return {
            "status": "skipped_unavailable",
            "duration_seconds": round(time.monotonic() - started, 3),
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _load_base_snapshot_id(machine_root: Path) -> str | None:
    payload = _read_json(machine_root / ".projection-state.json")
    snapshot_id = payload.get("current_snapshot_id") if payload else None
    return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None


def _raw_env_payload(
    config: VaultConfig,
    machine_names: list[str],
) -> dict[str, object]:
    started = time.monotonic()
    view = build_view(config, mode="raw")
    machine_root_counts: dict[str, int] = {}
    for machine_name in machine_names:
        machine = config.machines[machine_name]
        expected_root = config.paths.import_root / machine.import_name / ".raw"
        machine_root_counts[machine_name] = sum(
            1 for _, path in view.extra_dirs if path == expected_root or expected_root in path.parents
        )
    managed_extra_count = sum(
        1
        for _, path in view.extra_dirs
        if path == config.paths.local_workspace_extras or config.paths.local_workspace_extras in path.parents
    )
    home_matches = view.home == config.paths.home and view.home.is_dir()
    valid = home_matches and all(count > 0 for count in machine_root_counts.values())
    return {
        "status": "valid" if valid else "invalid",
        "duration_seconds": round(time.monotonic() - started, 3),
        "home": str(view.home),
        "extra_dirs": [{"client": client, "path": str(path)} for client, path in view.extra_dirs],
        "validation": {
            "home_matches": home_matches,
            "remote_raw_root_counts": machine_root_counts,
            "managed_local_extra_count": managed_extra_count,
        },
    }


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", value)


def _parse_tokscale_stats(output: str) -> dict[str, object] | None:
    text = _strip_ansi(output)
    patterns = {
        "date_range": r"Date range:\s*(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})",
        "active_days": r"Active days:\s*([\d,]+)",
        "total_tokens": r"Total tokens:\s*([\d,]+)",
        "cost": r"Total cost:\s*\$([\d,.]+)",
        "clients": r"Clients:\s*([^\r\n]+)",
        "models": r"Models:\s*(\d+)\s+models?",
    }
    matches = {name: re.search(pattern, text) for name, pattern in patterns.items()}
    if not all(matches.values()):
        return None
    date_match = matches["date_range"]
    assert date_match is not None
    return {
        "date_range": {"start": date_match.group(1), "end": date_match.group(2)},
        "active_days": int(matches["active_days"].group(1).replace(",", "")),  # type: ignore[union-attr]
        "total_tokens": int(matches["total_tokens"].group(1).replace(",", "")),  # type: ignore[union-attr]
        "cost": f'${matches["cost"].group(1)}',  # type: ignore[union-attr]
        "clients": [item.strip() for item in matches["clients"].group(1).split(",")],  # type: ignore[union-attr]
        "models": int(matches["models"].group(1)),  # type: ignore[union-attr]
    }


def _parse_profile_url(output: str) -> str | None:
    match = re.search(r"https://tokscale\.ai/u/[^\s]+", _strip_ansi(output))
    return match.group(0).rstrip(".,") if match else None


def _parse_latest_version(output: str) -> str:
    version_pattern = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")
    versions = [line.strip() for line in output.splitlines() if version_pattern.fullmatch(line.strip())]
    if not versions:
        raise DailyTokscaleError("npm_latest", "npm output did not contain a package version")
    return versions[-1]


def _contract_from_help(version: str, clients: tuple[str, ...], output: str) -> dict[str, object]:
    text = _strip_ansi(output)
    if "--dry-run" not in text:
        raise DailyTokscaleError("submit_help", "latest Tokscale submit help does not expose --dry-run")
    if re.search(r"-c,\s*--client\b", text):
        client_args = ["-c", ",".join(clients)]
    elif re.search(r"--client(?:\s|\s*<)", text):
        client_args = ["--client", ",".join(clients)]
    else:
        raise DailyTokscaleError("submit_help", "latest Tokscale client filter is not recognized")
    return {
        "schema_version": 1,
        "tokscale_version": version,
        "clients": list(clients),
        "client_args": client_args,
        "dry_run": True,
        "verified_at": _timestamp(),
    }


def _contract_matches(payload: dict[str, object] | None, version: str, clients: tuple[str, ...]) -> bool:
    if not payload:
        return False
    client_args = payload.get("client_args")
    return (
        payload.get("tokscale_version") == version
        and payload.get("clients") == list(clients)
        and isinstance(client_args, list)
        and all(isinstance(item, str) for item in client_args)
        and payload.get("dry_run") is True
    )


def _bundle_payload(bundle: ProjectionBundle) -> dict[str, object]:
    return {
        "snapshot_id": bundle.snapshot_id,
        "bundle_bytes": bundle.bundle_bytes,
        "mode": bundle.mode,
        "base_snapshot_id": bundle.base_snapshot_id,
        "fallback_reason": bundle.fallback_reason,
    }


def run_daily_tokscale(
    config: VaultConfig,
    *,
    machine_names: list[str] | None = None,
    clients: tuple[str, ...] = DEFAULT_CLIENTS,
    run_root: Path | None = None,
    canonicalize_command: str | None = "tokscale-canonicalize-import-machine",
    probe_timeout_seconds: float = 8,
    sync_timeout_seconds: float = 1800,
    submit_timeout_seconds: float = 3600,
    force_contract_check: bool = False,
) -> DailyTokscaleResult:
    selected_machines = list(config.machines if machine_names is None else machine_names)
    unknown_machines = [name for name in selected_machines if name not in config.machines]
    if unknown_machines:
        raise ValueError(f"unknown machines: {', '.join(unknown_machines)}")
    if not selected_machines:
        raise ValueError("at least one machine is required")
    if not clients:
        raise ValueError("at least one Tokscale client is required")

    started_monotonic = time.monotonic()
    started_at = _utc_now()
    run_id = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    root = (run_root or config.config_path.parent / "ops" / "daily-tokscale").expanduser()
    run_dir = root / "runs" / run_id
    current_path = root / "current.json"
    contract_path = root / "submit-contract.json"
    lock_path = root / "run.lock"
    receipt_path = run_dir / "receipt.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")

    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "phase": "starting",
        "started_at": started_at.isoformat(),
        "updated_at": started_at.isoformat(),
        "runner_pid": os.getpid(),
        "child_pid": None,
        "run_dir": str(run_dir),
        "current_status_path": str(current_path),
        "receipt_path": str(receipt_path),
        "remotes": [],
    }

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        payload.update(
            {
                "status": "already_running",
                "phase": "complete",
                "finished_at": _timestamp(),
                "duration_seconds": round(time.monotonic() - started_monotonic, 3),
                "error": {
                    "phase": "lock",
                    "type": "ConcurrentRun",
                    "message": f"another daily Tokscale run holds {lock_path}",
                },
            }
        )
        _write_json(receipt_path, payload)
        lock_file.close()
        return DailyTokscaleResult(payload=payload, exit_code=2)

    def update_status(phase: str, child_pid: int | None = None) -> None:
        payload["phase"] = phase
        payload["child_pid"] = child_pid
        payload["updated_at"] = _timestamp()
        _write_json(current_path, payload)

    def run_command(
        command: list[str],
        *,
        env: dict[str, str] | None,
        log_name: str,
        phase: str,
        timeout_seconds: float | None,
    ) -> CommandResult:
        update_status(phase)
        return _run_logged_command(
            command,
            env=env,
            log_path=run_dir / log_name,
            timeout_seconds=timeout_seconds,
            on_pid=lambda pid: update_status(phase, pid),
        )

    exit_code = 1
    preview_stats: dict[str, object] | None = None
    preview_ran = False
    help_seconds: float | None = None
    preview_seconds: float | None = None
    try:
        remote_results: list[dict[str, object]] = []
        for machine_name in selected_machines:
            machine_started = time.monotonic()
            machine = config.machines[machine_name]
            update_status(f"probe:{machine_name}")
            probe = _probe_machine(machine, probe_timeout_seconds)
            remote: dict[str, object] = {"machine": machine_name, "probe": probe}
            if probe["status"] != "available":
                remote["status"] = "skipped_unavailable"
                remote["duration_seconds"] = round(time.monotonic() - machine_started, 3)
                remote_results.append(remote)
                payload["remotes"] = remote_results
                update_status(f"remote_complete:{machine_name}")
                continue
            if not machine.source_home or not machine.remote_relay_root or not machine.ssh_target:
                remote["status"] = "sync_failed"
                remote["error"] = "machine projection configuration is incomplete"
                remote["duration_seconds"] = round(time.monotonic() - machine_started, 3)
                remote_results.append(remote)
                payload["remotes"] = remote_results
                continue
            try:
                export_started = time.monotonic()
                update_status(f"projection_export:{machine_name}")
                machine_root = config.paths.import_root / machine.import_name
                exported = export_machine_projection_ssh(
                    machine=machine,
                    source_home=machine.source_home,
                    relay_root=machine.remote_relay_root,
                    ssh_target=machine.ssh_target,
                    base_snapshot_id=_load_base_snapshot_id(machine_root),
                    ssh_options=list(SSH_OPTIONS),
                    timeout_seconds=sync_timeout_seconds,
                )
                remote["export_seconds"] = round(time.monotonic() - export_started, 3)

                fetch_started = time.monotonic()
                update_status(f"projection_fetch:{machine_name}")
                local_bundle_dir = expected_local_projection_bundle_dir(config, machine_name, exported.snapshot_id)
                fetched_dir = fetch_projection_bundle_ssh(
                    ssh_target=machine.ssh_target,
                    remote_bundle_dir=exported.bundle_dir,
                    local_bundle_dir=local_bundle_dir,
                    ssh_options=[
                        "-o",
                        "ConnectTimeout=8",
                        "-o",
                        "ConnectionAttempts=1",
                        "-o",
                        "ServerAliveInterval=2",
                        "-o",
                        "ServerAliveCountMax=1",
                    ],
                    timeout_seconds=sync_timeout_seconds,
                    capture_output=True,
                )
                remote["fetch_seconds"] = round(time.monotonic() - fetch_started, 3)

                import_started = time.monotonic()
                update_status(f"projection_import:{machine_name}")
                imported = import_machine_projection(
                    config=config,
                    machine_name=machine_name,
                    bundle_dir=fetched_dir,
                    canonicalize_command=None,
                )
                if canonicalize_command:
                    canonicalize = run_command(
                        [canonicalize_command, "--machine-root", str(machine_root)],
                        env=None,
                        log_name=f"{machine_name}-canonicalize.log",
                        phase=f"canonicalize:{machine_name}",
                        timeout_seconds=sync_timeout_seconds,
                    )
                    if canonicalize.returncode != 0:
                        raise RuntimeError(
                            f"canonicalize failed with exit {canonicalize.returncode}; "
                            f"see {run_dir / f'{machine_name}-canonicalize.log'}"
                        )
                remote["import_seconds"] = round(time.monotonic() - import_started, 3)
                remote["status"] = "synced"
                remote["bundle"] = _bundle_payload(imported)
            except Exception as exc:  # noqa: BLE001 - remote failures must not block submit
                remote["status"] = "sync_failed"
                remote["error"] = f"{type(exc).__name__}: {exc}"
            remote["duration_seconds"] = round(time.monotonic() - machine_started, 3)
            remote_results.append(remote)
            payload["remotes"] = remote_results
            update_status(f"remote_complete:{machine_name}")

        update_status("raw_env")
        raw_env = _raw_env_payload(config, selected_machines)
        payload["raw_env"] = raw_env
        if raw_env["status"] != "valid":
            raise DailyTokscaleError("raw_env", "raw Tokscale view is missing HOME or configured remote import roots")

        latest = run_command(
            ["npm", "view", "tokscale", "version"],
            env=None,
            log_name="npm-latest.log",
            phase="npm_latest",
            timeout_seconds=60,
        )
        if latest.returncode != 0:
            raise DailyTokscaleError("npm_latest", f"npm view failed with exit {latest.returncode}")
        version = _parse_latest_version(latest.output)
        package = f"tokscale@{version}"

        contract = _read_json(contract_path)
        contract_checked = force_contract_check or not _contract_matches(contract, version, clients)
        if contract_checked:
            help_invocation = build_tokscale_invocation(
                config,
                mode="raw",
                args=["submit", "--help"],
                package_override=package,
            )
            help_result = run_command(
                help_invocation.command,
                env=help_invocation.env,
                log_name="submit-help.log",
                phase="submit_help",
                timeout_seconds=120,
            )
            if help_result.returncode != 0:
                raise DailyTokscaleError("submit_help", f"Tokscale help failed with exit {help_result.returncode}")
            help_seconds = help_result.duration_seconds
            contract = _contract_from_help(version, clients, help_result.output)
            client_args = list(contract["client_args"])
            preview_invocation = build_tokscale_invocation(
                config,
                mode="raw",
                args=["submit", *client_args, "--dry-run"],
                package_override=package,
            )
            preview = run_command(
                preview_invocation.command,
                env=preview_invocation.env,
                log_name="preview.log",
                phase="preview",
                timeout_seconds=submit_timeout_seconds,
            )
            preview_ran = True
            preview_seconds = preview.duration_seconds
            preview_stats = _parse_tokscale_stats(preview.output)
            if (
                preview.returncode != 0
                or preview_stats is None
                or "Dry run - not submitting data." not in _strip_ansi(preview.output)
            ):
                raise DailyTokscaleError("preview", "official preview did not produce a complete terminal receipt")
            _write_json(contract_path, contract)
        else:
            assert contract is not None
            client_args = list(contract["client_args"])

        submit_invocation = build_tokscale_invocation(
            config,
            mode="raw",
            args=["submit", *client_args],
            package_override=package,
        )
        submit = run_command(
            submit_invocation.command,
            env=submit_invocation.env,
            log_name="submit.log",
            phase="submit",
            timeout_seconds=submit_timeout_seconds,
        )
        submit_stats = _parse_tokscale_stats(submit.output)
        numeric_stats = submit_stats or preview_stats
        confirmed = submit.returncode == 0 and "Successfully submitted!" in _strip_ansi(submit.output)
        receipt_complete = confirmed and numeric_stats is not None
        payload["tokscale"] = {
            "submit_status": "confirmed" if confirmed else "unconfirmed",
            "version": version,
            "npm_latest_seconds": latest.duration_seconds,
            "contract_checked": contract_checked,
            "help_seconds": help_seconds,
            "preview_ran": preview_ran,
            "preview_seconds": preview_seconds,
            "numeric_source": (
                "submit" if submit_stats is not None else "pre-submit official preview" if preview_stats else "unavailable"
            ),
            "statistics": numeric_stats,
            "profile_url": _parse_profile_url(submit.output),
            "submit_exit_code": submit.returncode,
            "submit_seconds": submit.duration_seconds,
            "submit_log": str(run_dir / "submit.log"),
            "preview_log": str(run_dir / "preview.log") if preview_ran else None,
        }
        if receipt_complete:
            payload["status"] = "confirmed"
            exit_code = 0
        elif confirmed:
            payload["status"] = "incomplete_receipt"
            payload["error"] = {
                "phase": "submit_receipt",
                "type": "MissingStatistics",
                "message": "submit succeeded but no current-run statistics were available",
            }
        else:
            payload["status"] = "unconfirmed"
            payload["error"] = {
                "phase": "submit",
                "type": "SubmitFailed",
                "message": f"Tokscale submit did not return a confirmed receipt; exit={submit.returncode}",
            }
    except DailyTokscaleError as exc:
        payload["status"] = "failed"
        payload["error"] = {"phase": exc.phase, "type": type(exc).__name__, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - always persist a terminal automation receipt
        payload["status"] = "failed"
        payload["error"] = {
            "phase": str(payload.get("phase") or "unknown"),
            "type": type(exc).__name__,
            "message": str(exc),
        }
    finally:
        payload["phase"] = "complete"
        payload["child_pid"] = None
        payload["finished_at"] = _timestamp()
        payload["updated_at"] = payload["finished_at"]
        payload["duration_seconds"] = round(time.monotonic() - started_monotonic, 3)
        _write_json(receipt_path, payload)
        _write_json(current_path, payload)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    return DailyTokscaleResult(payload=payload, exit_code=exit_code)

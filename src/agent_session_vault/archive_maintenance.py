"""Scheduled session maintenance: one launch per stage, read-only reconciliation."""
from __future__ import annotations

from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from .archive import sha256_file
from .archive_prune import load_prune_plan
from .config import VaultConfig


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: object, *, exclusive: bool = True) -> None:
    if exclusive:
        with path.open("x", encoding="utf-8") as output:
            json.dump(value, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    else:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.pending")
        write_json(temporary, value)
        temporary.replace(path)


def local_root(config: VaultConfig) -> Path:
    return config.paths.home / ".config/agent-session-vault"


def tools_for(config: VaultConfig) -> tuple[str, str, str]:
    return (str(config.paths.home / ".local/bin/python3"),
            str(config.paths.home / ".local/bin/agent-session-vault"),
            str(local_root(config) / "run-detached.py"))


def no_links(path: Path) -> None:
    if not path.is_absolute() or path != path.absolute() or ".." in path.parts:
        raise ValueError(f"path must be absolute and normalized: {path}")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError(f"symlink path is excluded: {path}")


def run_directory(config: VaultConfig, run_dir: Path) -> Path:
    expected = local_root(config) / "ops/archive-automation"
    no_links(run_dir)
    if run_dir.parent != expected or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", run_dir.name):
        raise ValueError("run directory must be an immediate child of ops/archive-automation")
    return run_dir


def preflight(config: VaultConfig) -> dict:
    python, _, _ = tools_for(config)
    result = subprocess.run([python, str(local_root(config) / "nas-preflight.py")],
                            capture_output=True, text=True, timeout=55)
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise ValueError("NAS preflight did not return terminal JSON") from exc
    if not isinstance(payload, dict) or result.returncode != 0 or payload.get("status") != "ready":
        raise ValueError(f"NAS preflight blocked ({result.returncode}): {payload}")
    if Path(str(payload.get("root", ""))) != config.archive.root.parent:
        raise ValueError("NAS preflight root differs from configured archive parent")
    return payload


def stage_status(run_dir: Path, name: str) -> dict:
    base = run_dir / name
    result = {"name": name, "status": "not_started", "exit_code": None, "runner_pid": None}
    launch = Path(str(base) + ".launch.json")
    if not launch.exists():
        return result
    record = read_json(launch)
    result["command"] = record.get("command")
    result["status"] = "unknown"
    pid_path = Path(str(base) + ".pid")
    if pid_path.exists():
        pid = int(pid_path.read_text().strip())
        result["runner_pid"] = pid
        process = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "stat=", "-o", "command="],
                                 capture_output=True, text=True, timeout=5)
        line = process.stdout.strip()
        command = record.get("command", [])
        if (process.returncode == 0 and line and not line.split()[0].startswith("Z")
                and command and Path(str(command[0])).name in line
                and all(str(arg) in line for arg in command[1:])):
            result["status"] = "running"
    exit_path = Path(str(base) + ".exit_code")
    if exit_path.exists():
        result["exit_code"] = int(exit_path.read_text().strip())
        result["status"] = "exited_without_json"
        try:
            payload = read_json(Path(str(base) + ".stdout.json"))
        except (OSError, ValueError):
            pass
        else:
            result["status"] = payload.get("status", "exited")
            result["result"] = {key: value for key, value in payload.items() if key not in {"entries", "units", "retained"}}
            if "entries" in payload:
                result["result"]["entries_count"] = len(payload["entries"])
    return result


def require_stage(run_dir: Path, name: str, expected_status: str | None) -> dict:
    stage = stage_status(run_dir, name)
    if stage["exit_code"] != 0 or stage["status"] in {"unknown", "exited_without_json", "running", "not_started"}:
        raise ValueError(f"stage lacks successful real exit code and terminal JSON: {name}: {stage['status']}")
    payload = read_json(run_dir / f"{name}.stdout.json")
    if expected_status is not None and payload.get("status") != expected_status:
        raise ValueError(f"stage {name} is {payload.get('status')}, expected {expected_status}")
    return payload


def require_dedup_report(payload: dict) -> dict:
    counters = payload.get("dedup")
    keys = ("files_seen", "file_logical_bytes", "new_content_files", "new_content_bytes", "new_objects",
            "new_archive_bytes", "reused_files", "reused_logical_bytes", "historical_reused_files", "same_cycle_reused_files")
    if not isinstance(counters, dict) or any(type(counters.get(key)) is not int or counters[key] < 0 for key in keys):
        raise ValueError("archive cycle lacks valid content-reuse counters; stop before pruning")
    if (counters["files_seen"] != counters["new_content_files"] + counters["reused_files"]
            or counters["file_logical_bytes"] != counters["new_content_bytes"] + counters["reused_logical_bytes"]
            or counters["reused_files"] != counters["historical_reused_files"] + counters["same_cycle_reused_files"]
            or (counters["new_objects"] == 0) != (counters["new_archive_bytes"] == 0)
            or (counters["new_objects"] == 0) != (counters["new_content_files"] == 0)):
        raise ValueError("archive content-reuse counters are inconsistent; stop before pruning")
    return counters


def session_gate(config: VaultConfig, run_dir: Path) -> dict:
    run_directory(config, run_dir)
    require_stage(run_dir, "archive-maintenance", "session_verified")
    require_dedup_report(require_stage(run_dir, "archive-cycle", "verified"))
    stdout = require_stage(run_dir, "prune-plan", None)
    plan_path = run_dir / "prune-plan.json"
    plan = load_prune_plan(plan_path)
    if stdout.get("plan_digest") != plan.plan_digest or stdout.get("plan_id") != plan.plan_id:
        raise ValueError("immutable run prune plan differs from original stdout")
    if plan.entries:
        applied = require_stage(run_dir, "prune-apply", "verified")
        if applied.get("plan_digest") != plan.plan_digest:
            raise ValueError("applied prune plan differs from this run")
    return stdout


def _stage(config: VaultConfig, run_dir: Path, name: str, arguments: list[str]) -> dict:
    python, cli, launcher = tools_for(config)
    nas = preflight(config)
    write_json(run_dir / f"{name}.preflight.json", nas)
    command = [python, launcher, "--log-dir", str(run_dir), "--name", name,
               "--", cli, "--config", str(config.config_path), *arguments, "--json"]
    # The launch reservation is the authority; a timeout is never retried.
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    launch = json.loads(result.stdout)
    if not isinstance(launch, dict):
        raise ValueError("launcher returned an invalid result; inspect existing evidence")
    write_json(run_dir / f"{name}.launcher-result.json", {**launch, "launcher_exit_code": result.returncode})
    if result.returncode != 0 or launch.get("status") != "started":
        raise ValueError(f"stage launch failed or unknown: {name}")
    while True:
        status = stage_status(run_dir, name)
        if status["exit_code"] is not None:
            return status
        if status["status"] != "running":
            # The child may exit just before its supervisor publishes wait().
            for _ in range(5):
                time.sleep(1)
                status = stage_status(run_dir, name)
                if status["exit_code"] is not None:
                    return status
            raise ValueError(f"stage has unknown terminal state; inspect without relaunch: {name}")
        time.sleep(2)


def run_sessions(config: VaultConfig, run_dir: Path) -> dict:
    run_directory(config, run_dir)
    preflight(config)
    write_json(run_dir / "session.intent.json", {"run_id": run_dir.name, "started_at": datetime.now(UTC).isoformat()})
    from .cold_archive import metrics
    before = metrics(config.paths.home / ".codex")
    write_json(run_dir / "space-before.json", before)
    cycle = _stage(config, run_dir, "archive-cycle", ["ops", "archive-cycle", "--no-due-only", "--deep"])
    dedup = require_dedup_report(require_stage(run_dir, "archive-cycle", "verified"))
    current_plan = local_root(config) / "prune-plans/current.json"
    current_plan.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    _stage(config, run_dir, "prune-plan", ["archive", "prune-plan", "--plan-path", str(current_plan)])
    planned = require_stage(run_dir, "prune-plan", None)
    plan = load_prune_plan(current_plan)
    if (datetime.fromisoformat(plan.created_at) < started
            or plan.plan_digest != planned.get("plan_digest") or plan.plan_id != planned.get("plan_id")):
        raise ValueError("prune plan is stale or changed; do not reuse current.json")
    immutable_plan = run_dir / "prune-plan.json"
    with immutable_plan.open("xb") as output:
        output.write(current_plan.read_bytes())
    if plan.plan_digest != load_prune_plan(immutable_plan).plan_digest:
        raise ValueError("prune plan changed while capturing this run")
    applied = None
    if plan.entries:
        _stage(config, run_dir, "prune-apply", ["archive", "prune-apply", "--plan", str(immutable_plan)])
        applied = require_stage(run_dir, "prune-apply", "verified")
        if applied.get("plan_digest") != plan.plan_digest:
            raise ValueError("prune apply returned a different plan")
    payload = {"status": "session_verified", "run_id": run_dir.name, "run_dir": str(run_dir),
               "finished_at": datetime.now(UTC).isoformat(), "cycle": cycle,
               "plan_path": str(immutable_plan), "plan_sha256": sha256_file(immutable_plan),
               "plan_id": plan.plan_id, "plan_digest": plan.plan_digest,
               "eligible_file_count": len(plan.entries), "prune": applied,
               "tokscale_before": plan.tokscale_preview,
               "tokscale_after": applied.get("tokscale_preview") if applied else None,
               "space_before": before,
               "dedup": dedup,
               "next": "cold-inventory; review whole batches, then cold-plan/cold-pack/cold-cleanup"}
    write_json(run_dir / "session-result.json", payload)
    return payload


def maintenance_status(config: VaultConfig, run_dir: Path | None = None) -> dict:
    latest = local_root(config) / "ops/archive-automation/latest-launch.json"
    launch = {}
    if run_dir is None:
        if not latest.exists():
            return {"status": "no_launch_record"}
        launch = read_json(latest)
        run_dir = Path(launch["run_dir"])
    run_directory(config, run_dir)
    names = ["archive-maintenance", "archive-cycle", "prune-plan", "prune-apply",
             "cold-inventory", "cold-plan", "cold-pack", "cold-cleanup"]
    stages = [stage_status(run_dir, name) for name in names]
    status = stages[0]["status"]
    if status == "not_started" and launch.get("status") in {"starting", "started", "launch_unknown"}:
        status = "unknown"
    if status == "session_verified" and stages[0]["exit_code"] == 0:
        status = ("completed" if stages[-1]["status"] in {"purged", "no_candidates"}
                  and stages[-1]["exit_code"] == 0 else "awaiting_cold_stages")
    return {"status": status, "run_id": run_dir.name, "run_dir": str(run_dir), "stages": stages}


def start_maintenance(config: VaultConfig) -> dict:
    preflight(config)
    root = local_root(config) / "ops/archive-automation"
    no_links(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "launch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("maintenance launch lock is held") from exc
        previous = maintenance_status(config)
        if previous["status"] in {"unknown", "awaiting_cold_stages"}:
            raise ValueError(f"original run is incomplete; use --status: {previous['run_dir']}")
        for stage in previous.get("stages", []):
            if stage["status"] in {"unknown", "running", "exited_without_json"}:
                raise ValueError(f"original run unresolved; use --status: {previous['run_dir']}")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        run_dir = root / stamp
        run_dir.mkdir()
        python, cli, launcher = tools_for(config)
        command = [python, launcher, "--log-dir", str(run_dir), "--name", "archive-maintenance", "--",
                   cli, "--config", str(config.config_path), "ops", "archive-maintenance",
                   "--run-dir", str(run_dir), "--execute", "--json"]
        launch = {"status": "starting", "run_id": stamp, "run_dir": str(run_dir),
                  "created_at": datetime.now(UTC).isoformat(), "command": command}
        write_json(root / "latest-launch.json", launch, exclusive=False)
        # Reserve latest before calling the launcher. Unknown startup is reconciled
        # through the same directory; it never creates a second business attempt.
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            launched = json.loads(result.stdout)
            if not isinstance(launched, dict):
                raise ValueError("launcher returned an invalid result")
        except (subprocess.SubprocessError, ValueError, OSError):
            launch["status"] = "launch_unknown"
            write_json(root / "latest-launch.json", launch, exclusive=False)
            raise
        launch.update(launched, launcher_exit_code=result.returncode)
        write_json(root / "latest-launch.json", launch, exclusive=False)
        return launch

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import base64
import json
from pathlib import Path
import shutil
import hashlib
import os
import subprocess

from .archive import _pack_to_bundle_path, _sha256_file
from .config import VaultConfig
from .syncing import DeltaStats


SUPPORTED_SOURCE_ROOTS: tuple[tuple[str, str], ...] = (
    (".codex/sessions", ".raw/codex/sessions"),
    (".codex/archived_sessions", ".raw/codex/archived_sessions"),
    (".gemini/tmp", ".raw/gemini"),
    (".openclaw/agents", ".raw/openclaw/agents"),
    (".clawdbot/agents", ".raw/openclaw/clawdbot"),
    (".moltbot/agents", ".raw/openclaw/moltbot"),
    (".moldbot/agents", ".raw/openclaw/moldbot"),
)


@dataclass(frozen=True)
class RelayBundle:
    machine_name: str
    snapshot_id: str
    previous_snapshot_id: str | None
    bundle_dir: Path
    manifest_path: Path
    bundle_path: Path


def _state_file(state_root: Path, machine_name: str) -> Path:
    return state_root / machine_name / "snapshot-state.json"


def _load_state(path: Path) -> dict:
    if not path.is_file():
        return {"sequence": 0, "current_snapshot_id": None, "files": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_supported_source_home(source_home: Path) -> tuple[dict[str, dict], int]:
    files: dict[str, dict] = {}
    total_bytes = 0
    for source_rel, dest_rel in SUPPORTED_SOURCE_ROOTS:
        source_root = source_home / source_rel
        if not source_root.is_dir():
            continue
        for file_path in sorted(source_root.rglob("*")):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(source_root)
            logical_path = f"{dest_rel}/{rel.as_posix()}"
            size_bytes = file_path.stat().st_size
            files[logical_path] = {
                "source_path": str(file_path),
                "sha256": _sha256_path(file_path),
                "bytes": size_bytes,
            }
            total_bytes += size_bytes
    return files, total_bytes


def _changed_files(current_files: dict[str, dict], previous_files: dict[str, str]) -> tuple[list[str], int]:
    changed_paths: list[str] = []
    changed_bytes = 0
    for logical_path, item in sorted(current_files.items()):
        if previous_files.get(logical_path) == item["sha256"]:
            continue
        changed_paths.append(logical_path)
        changed_bytes += int(item["bytes"])
    return changed_paths, changed_bytes


def inspect_machine_delta(machine_name: str, source_home: Path, state_root: Path) -> DeltaStats:
    source_home = source_home.expanduser().resolve()
    state_root = state_root.expanduser().resolve()
    state = _load_state(_state_file(state_root, machine_name))
    current_files, total_bytes = _scan_supported_source_home(source_home)
    changed_paths, changed_bytes = _changed_files(current_files, state.get("files", {}))
    next_snapshot_id = f"{machine_name}-{int(state.get('sequence', 0)) + 1:06d}"
    return DeltaStats(
        machine_name=machine_name,
        changed_files=len(changed_paths),
        changed_bytes=changed_bytes,
        previous_snapshot_id=state.get("current_snapshot_id"),
        next_snapshot_id=next_snapshot_id,
        total_files=len(current_files),
        total_bytes=total_bytes,
    )


def export_machine_delta(
    machine_name: str,
    source_home: Path,
    relay_root: Path,
    state_root: Path,
) -> RelayBundle:
    source_home = source_home.expanduser().resolve()
    relay_root = relay_root.expanduser().resolve()
    state_root = state_root.expanduser().resolve()
    state_path = _state_file(state_root, machine_name)
    state = _load_state(state_path)
    current_files, _ = _scan_supported_source_home(source_home)

    previous_files = state.get("files", {})
    changed_files, _ = _changed_files(current_files, previous_files)
    if not changed_files:
        raise ValueError(f"no changed files found for relay export: {machine_name}")

    sequence = int(state.get("sequence", 0)) + 1
    snapshot_id = f"{machine_name}-{sequence:06d}"
    previous_snapshot_id = state.get("current_snapshot_id")
    bundle_dir = relay_root / machine_name / snapshot_id
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    staging_dir = bundle_dir / ".staging"
    payload_root = staging_dir / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)

    for logical_path in changed_files:
        source_path = Path(current_files[logical_path]["source_path"])
        dest_path = payload_root / logical_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, dest_path)

    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundle_dir / "payload.tar.zst"
    manifest_path = bundle_dir / "manifest.json"
    _pack_to_bundle_path(payload_root, bundle_path)

    manifest_payload = {
        "version": 1,
        "mode": "delta",
        "machine": machine_name,
        "snapshot_id": snapshot_id,
        "previous_snapshot_id": previous_snapshot_id,
        "created_at": datetime.now(UTC).isoformat(),
        "bundle": {
            "name": bundle_path.name,
            "bytes": bundle_path.stat().st_size,
            "sha256": _sha256_file(bundle_path),
        },
        "files": {logical_path: current_files[logical_path]["sha256"] for logical_path in changed_files},
    }
    manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    shutil.rmtree(staging_dir)

    _save_state(
        state_path,
        {
            "sequence": sequence,
            "current_snapshot_id": snapshot_id,
            "files": {logical_path: item["sha256"] for logical_path, item in current_files.items()},
        },
    )
    return RelayBundle(
        machine_name=machine_name,
        snapshot_id=snapshot_id,
        previous_snapshot_id=previous_snapshot_id,
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        bundle_path=bundle_path,
    )


def _merge_tree(source_root: Path, dest_root: Path) -> None:
    for item in sorted(source_root.rglob("*")):
        if item.is_dir():
            continue
        rel = item.relative_to(source_root)
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dest)


def import_machine_delta(
    config: VaultConfig,
    machine_name: str,
    bundle_dir: Path,
    canonicalize_command: str | None = None,
) -> RelayBundle:
    bundle_dir = bundle_dir.expanduser().resolve()
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"relay manifest not found: {manifest_path}")

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle_name = payload.get("bundle", {}).get("name")
    if isinstance(bundle_name, str) and bundle_name:
        bundle_path = bundle_dir / bundle_name
    else:
        bundle_path = bundle_dir / "payload.tar.zst"
    if not bundle_path.is_file():
        raise FileNotFoundError(f"relay payload not found: {bundle_path}")
    if payload.get("machine") != machine_name:
        raise ValueError(f"bundle machine mismatch: expected {machine_name}, got {payload.get('machine')}")
    actual_sha256 = _sha256_file(bundle_path)
    expected_sha256 = payload.get("bundle", {}).get("sha256")
    if actual_sha256 != expected_sha256:
        raise ValueError(f"bundle sha256 mismatch: expected {expected_sha256}, got {actual_sha256}")

    machine = config.machines[machine_name]
    machine_root = config.paths.import_root / machine.import_name
    relay_state_path = machine_root / ".relay-state.json"
    current_state = _load_state(relay_state_path)
    previous_snapshot_id = payload.get("previous_snapshot_id")
    current_snapshot_id = current_state.get("current_snapshot_id")
    if previous_snapshot_id != current_snapshot_id:
        raise ValueError(
            f"previous_snapshot_id mismatch: bundle expects {previous_snapshot_id}, local has {current_snapshot_id}"
        )

    extract_root = bundle_dir / ".extract"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["bsdtar", "-xf", str(bundle_path), "-C", str(extract_root)], check=True)
    _merge_tree(extract_root, machine_root)
    shutil.rmtree(extract_root)

    _save_state(
        relay_state_path,
        {
            "current_snapshot_id": payload.get("snapshot_id"),
            "last_imported_at": datetime.now(UTC).isoformat(),
        },
    )

    if canonicalize_command:
        subprocess.run([canonicalize_command, "--machine-root", str(machine_root)], check=True)

    return RelayBundle(
        machine_name=machine_name,
        snapshot_id=str(payload.get("snapshot_id")),
        previous_snapshot_id=previous_snapshot_id if isinstance(previous_snapshot_id, str) else None,
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        bundle_path=bundle_path,
    )


def pending_relay_bundle_dirs(config: VaultConfig, machine_name: str) -> list[Path]:
    machine = config.machines[machine_name]
    bundle_root = config.paths.relay_root / machine_name
    if not bundle_root.is_dir():
        return []
    machine_root = config.paths.import_root / machine.import_name
    local_state = _load_state(machine_root / ".relay-state.json")
    current_snapshot_id = local_state.get("current_snapshot_id")

    by_previous: dict[str | None, tuple[str, Path]] = {}
    for manifest_path in sorted(bundle_root.glob("*/manifest.json")):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload.get("machine") != machine_name:
            continue
        snapshot_id = payload.get("snapshot_id")
        previous_snapshot_id = payload.get("previous_snapshot_id")
        if not isinstance(snapshot_id, str):
            continue
        existing = by_previous.get(previous_snapshot_id if isinstance(previous_snapshot_id, str) else None)
        candidate = (snapshot_id, manifest_path.parent)
        if existing is None or candidate[0] < existing[0]:
            by_previous[previous_snapshot_id if isinstance(previous_snapshot_id, str) else None] = candidate

    pending: list[Path] = []
    cursor = current_snapshot_id if isinstance(current_snapshot_id, str) else None
    while True:
        next_item = by_previous.get(cursor)
        if next_item is None:
            break
        snapshot_id, bundle_dir = next_item
        pending.append(bundle_dir)
        cursor = snapshot_id
    return pending


def _remote_helper_source() -> str:
    return f"""
from __future__ import annotations

from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

SUPPORTED_SOURCE_ROOTS = {SUPPORTED_SOURCE_ROOTS!r}


def _state_file(state_root: Path, machine_name: str) -> Path:
    return state_root / machine_name / "snapshot-state.json"


def _load_state(path: Path) -> dict:
    if not path.is_file():
        return {{"sequence": 0, "current_snapshot_id": None, "files": {{}}}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\\n", encoding="utf-8")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pack_to_bundle_path(source: Path, bundle_path: Path) -> None:
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    if bundle_path.exists():
        bundle_path.unlink()
    if bundle_path.suffixes[-2:] == [".tar", ".zst"]:
        subprocess.run(["bsdtar", "--zstd", "-cf", str(bundle_path), "-C", str(source), "."], check=True)
        return
    if bundle_path.suffixes[-2:] == [".tar", ".gz"]:
        with tarfile.open(bundle_path, "w:gz") as tar:
            tar.add(source, arcname=".")
        return
    with tarfile.open(bundle_path, "w") as tar:
        tar.add(source, arcname=".")


def _scan_supported_source_home(source_home: Path) -> tuple[dict[str, dict], int]:
    files: dict[str, dict] = {{}}
    total_bytes = 0
    for source_rel, dest_rel in SUPPORTED_SOURCE_ROOTS:
        source_root = source_home / source_rel
        if not source_root.is_dir():
            continue
        for file_path in sorted(source_root.rglob("*")):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(source_root)
            logical_path = f"{{dest_rel}}/{{rel.as_posix()}}"
            size_bytes = file_path.stat().st_size
            files[logical_path] = {{
                "source_path": str(file_path),
                "sha256": _sha256_path(file_path),
                "bytes": size_bytes,
            }}
            total_bytes += size_bytes
    return files, total_bytes


def _changed_files(current_files: dict[str, dict], previous_files: dict[str, str]) -> tuple[list[str], int]:
    changed_paths: list[str] = []
    changed_bytes = 0
    for logical_path, item in sorted(current_files.items()):
        if previous_files.get(logical_path) == item["sha256"]:
            continue
        changed_paths.append(logical_path)
        changed_bytes += int(item["bytes"])
    return changed_paths, changed_bytes


def main() -> int:
    encoded = os.environ.get("ASV_REQUEST_B64")
    if not encoded:
        raise RuntimeError("missing ASV_REQUEST_B64")
    request = json.loads(base64.b64decode(encoded.encode("ascii")).decode("utf-8"))
    machine_name = str(request["machine_name"])
    source_home = Path(request["source_home"]).expanduser().resolve()
    relay_root = Path(request["relay_root"]).expanduser().resolve()
    state_root = Path(request["state_root"]).expanduser().resolve()
    mode = str(request["mode"])

    state_path = _state_file(state_root, machine_name)
    state = _load_state(state_path)
    current_files, total_bytes = _scan_supported_source_home(source_home)
    changed_paths, changed_bytes = _changed_files(current_files, state.get("files", {{}}))
    sequence = int(state.get("sequence", 0)) + 1
    snapshot_id = f"{{machine_name}}-{{sequence:06d}}"
    payload = {{
        "machine_name": machine_name,
        "changed_files": len(changed_paths),
        "changed_bytes": changed_bytes,
        "total_files": len(current_files),
        "total_bytes": total_bytes,
        "previous_snapshot_id": state.get("current_snapshot_id"),
        "next_snapshot_id": snapshot_id,
    }}
    if mode == "inspect":
        print(json.dumps(payload))
        return 0

    if not changed_paths:
        print(json.dumps(payload))
        return 0

    bundle_dir = relay_root / machine_name / snapshot_id
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    staging_dir = bundle_dir / ".staging"
    payload_root = staging_dir / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)
    for logical_path in changed_paths:
        source_path = Path(current_files[logical_path]["source_path"])
        dest_path = payload_root / logical_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, dest_path)

    bundle_dir.mkdir(parents=True, exist_ok=True)
    if shutil.which("zstd"):
        bundle_path = bundle_dir / "payload.tar.zst"
    else:
        bundle_path = bundle_dir / "payload.tar.gz"
    manifest_path = bundle_dir / "manifest.json"
    _pack_to_bundle_path(payload_root, bundle_path)
    manifest_payload = {{
        "version": 1,
        "mode": "delta",
        "machine": machine_name,
        "snapshot_id": snapshot_id,
        "previous_snapshot_id": state.get("current_snapshot_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "bundle": {{
            "name": bundle_path.name,
            "bytes": bundle_path.stat().st_size,
            "sha256": _sha256_file(bundle_path),
        }},
        "files": {{logical_path: current_files[logical_path]["sha256"] for logical_path in changed_paths}},
    }}
    manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
    shutil.rmtree(staging_dir)
    _save_state(
        state_path,
        {{
            "sequence": sequence,
            "current_snapshot_id": snapshot_id,
            "files": {{logical_path: item["sha256"] for logical_path, item in current_files.items()}},
        }},
    )
    payload.update(
        {{
            "snapshot_id": snapshot_id,
            "bundle_dir": str(bundle_dir),
            "manifest_path": str(manifest_path),
            "bundle_path": str(bundle_path),
        }}
    )
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _run_remote_helper(
    request: dict[str, object],
    ssh_target: str | None = None,
    command_prefix: list[str] | None = None,
) -> dict:
    encoded = base64.b64encode(json.dumps(request, sort_keys=True).encode("utf-8")).decode("ascii")
    script = _remote_helper_source()
    base_command = command_prefix or ["python3", "-"]
    env = None
    if ssh_target:
        command = ["ssh", ssh_target, "env", f"ASV_REQUEST_B64={encoded}", *base_command]
    else:
        command = base_command
        env = dict(os.environ)
        env["ASV_REQUEST_B64"] = encoded

    completed = subprocess.run(
        command,
        input=script,
        text=True,
        capture_output=True,
        env=env,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        details = stderr or stdout or f"exit={completed.returncode}"
        raise RuntimeError(f"remote relay helper failed: {details}")
    stdout = completed.stdout.strip()
    if not stdout:
        raise ValueError("remote relay helper returned empty stdout")
    return json.loads(stdout)


def inspect_machine_delta_ssh(
    machine_name: str,
    source_home: Path,
    relay_root: Path,
    state_root: Path,
    ssh_target: str | None = None,
    command_prefix: list[str] | None = None,
) -> DeltaStats:
    payload = _run_remote_helper(
        {
            "mode": "inspect",
            "machine_name": machine_name,
            "source_home": str(source_home),
            "relay_root": str(relay_root),
            "state_root": str(state_root),
        },
        ssh_target=ssh_target,
        command_prefix=command_prefix,
    )
    return DeltaStats(
        machine_name=machine_name,
        changed_files=int(payload.get("changed_files", 0)),
        changed_bytes=int(payload.get("changed_bytes", 0)),
        previous_snapshot_id=payload.get("previous_snapshot_id"),
        next_snapshot_id=payload.get("next_snapshot_id"),
        total_files=int(payload.get("total_files", 0)),
        total_bytes=int(payload.get("total_bytes", 0)),
    )


def export_machine_delta_ssh(
    machine_name: str,
    source_home: Path,
    relay_root: Path,
    state_root: Path,
    ssh_target: str | None = None,
    command_prefix: list[str] | None = None,
) -> RelayBundle:
    payload = _run_remote_helper(
        {
            "mode": "export",
            "machine_name": machine_name,
            "source_home": str(source_home),
            "relay_root": str(relay_root),
            "state_root": str(state_root),
        },
        ssh_target=ssh_target,
        command_prefix=command_prefix,
    )
    snapshot_id = payload.get("snapshot_id")
    bundle_dir = payload.get("bundle_dir")
    manifest_path = payload.get("manifest_path")
    bundle_path = payload.get("bundle_path")
    if not snapshot_id or not bundle_dir or not manifest_path or not bundle_path:
        raise ValueError(f"no changed files found for relay export: {machine_name}")
    return RelayBundle(
        machine_name=machine_name,
        snapshot_id=str(snapshot_id),
        previous_snapshot_id=payload.get("previous_snapshot_id"),
        bundle_dir=Path(str(bundle_dir)),
        manifest_path=Path(str(manifest_path)),
        bundle_path=Path(str(bundle_path)),
    )

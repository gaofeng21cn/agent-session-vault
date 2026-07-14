from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timezone
import base64
import gzip
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from .archive import _pack_to_bundle_path, _sha256_file
from .config import MachineConfig, RootRuleConfig, VaultConfig


PROJECTION_ROOTS_MANIFEST_NAME = "roots-manifest.json"
PROJECTION_INVENTORY_NAME = "inventory.json"
LOCAL_PROJECTION_STATE_NAME = ".projection-state.json"
LOCAL_PROJECTION_ROOTS_MANIFEST_NAME = ".projection-roots-manifest.json"
LOCAL_PROJECTION_INVENTORY_NAME = ".projection-inventory.json"
PROJECTION_METADATA_NAMES = {PROJECTION_ROOTS_MANIFEST_NAME, PROJECTION_INVENTORY_NAME}
LOCAL_HOME_IMPORT_NAME = "local-home"
LOCAL_HOME_STATE_NAME = ".local-home-projection-state.json"
LOCAL_HOME_CLIENTS = ("codex", "gemini", "openclaw")
CODEX_PROJECTION_VERSION = 2
CODEX_SYSTEM_INJECTED_PREFIXES = (
    "<environment_context>",
    "<system-reminder>",
    "<user_instructions>",
)


@dataclass(frozen=True)
class DiscoveredRoot:
    client: str
    root_id: str
    source_path: Path
    label: str | None
    kind: str | None


@dataclass(frozen=True)
class ProjectionBundle:
    machine_name: str
    snapshot_id: str
    bundle_dir: Path
    manifest_path: Path
    bundle_path: Path
    roots_manifest_path: Path
    inventory_path: Path | None
    bundle_bytes: int
    mode: str = "projection_full"
    base_snapshot_id: str | None = None
    fallback_reason: str | None = None
    state_status: str | None = None
    files_seen: int = 0
    files_projected: int = 0
    files_reused: int = 0


@dataclass(frozen=True)
class LocalHomeProjectionResult:
    machine_root: Path
    projection_home: Path
    state_path: Path
    files_seen: int
    files_written: int
    files_skipped: int
    source_bytes_total: int
    dest_bytes_total: int
    token_events_total: int
    clients: tuple[str, ...]
    dry_run: bool


@dataclass(frozen=True)
class _LocalProjectionFile:
    client: str
    source_path: Path
    relative_destination: Path


def _json_load(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "root"


def _expand_user_like(raw: str, source_home: Path) -> Path:
    if raw == "~":
        return source_home
    if raw.startswith("~/"):
        return source_home / raw[2:]
    return Path(raw)


def _derive_root_label(path: Path, fallback: str) -> str:
    candidates = [path.name, path.parent.name, fallback]
    for candidate in candidates:
        cleaned = candidate.lstrip(".")
        if cleaned and cleaned not in {"sessions", "archived_sessions", "tmp", "agents"}:
            return cleaned
    return fallback


def _root_id(rule: RootRuleConfig, source_path: Path) -> str:
    base_label = rule.label or _derive_root_label(source_path, rule.client)
    digest = hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:8]
    return f"{_sanitize_slug(base_label)}-{digest}"


def discover_machine_roots(machine: MachineConfig, source_home: Path) -> list[DiscoveredRoot]:
    discovered: dict[tuple[str, str], DiscoveredRoot] = {}

    def add_root(client: str, rule: RootRuleConfig, raw_path: Path) -> None:
        if not raw_path.is_dir():
            return
        resolved = raw_path.resolve()
        key = (client, str(resolved))
        if key in discovered:
            return
        discovered[key] = DiscoveredRoot(
            client=client,
            root_id=_root_id(rule, resolved),
            source_path=resolved,
            label=rule.label,
            kind=rule.kind,
        )

    for rule in machine.roots:
        if rule.path is None:
            continue
        add_root(rule.client, rule, _expand_user_like(rule.path, source_home))

    for rule in machine.root_globs:
        if rule.glob is None:
            continue
        expanded = _expand_user_like(rule.glob, source_home)
        for match in sorted(glob.glob(str(expanded), recursive=True)):
            add_root(rule.client, rule, Path(match))

    return sorted(discovered.values(), key=lambda item: (item.client, item.root_id, str(item.source_path)))


def _copy_tree(source_root: Path, dest_root: Path) -> int:
    copied = 0
    for item in sorted(source_root.rglob("*")):
        if item.is_dir():
            continue
        rel = item.relative_to(source_root)
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dest)
        copied += 1
    return copied


def _copy_relative_file(source_root: Path, file_path: Path, dest_root: Path) -> int:
    rel = file_path.relative_to(source_root)
    dest_path = dest_root / rel
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file_path, dest_path)
    return dest_path.stat().st_size


def _selected_fields(value: dict, names: tuple[str, ...]) -> dict:
    return {name: value[name] for name in names if name in value}


def _project_codex_user_message(value: object) -> str:
    if not isinstance(value, str):
        return ""
    stripped = value.lstrip()
    for prefix in CODEX_SYSTEM_INJECTED_PREFIXES:
        if stripped.startswith(prefix):
            return prefix
    return "user"


def _project_codex_record(obj: dict) -> dict:
    projected = _selected_fields(obj, ("timestamp", "time", "created_at", "type", "model", "model_name", "usage"))
    obj_type = obj.get("type")
    payload = obj.get("payload")
    if isinstance(payload, dict):
        if obj_type == "session_meta":
            payload_fields = (
                "id",
                "forked_from_id",
                "source",
                "thread_source",
                "cwd",
                "model_provider",
                "agent_nickname",
            )
        elif obj_type == "turn_context":
            payload_fields = ("type", "model", "model_name", "model_info", "turn_id")
        else:
            payload_fields = ("type", "model", "model_name", "model_info", "info", "turn_id")
        projected_payload = _selected_fields(payload, payload_fields)
        if obj_type == "event_msg" and payload.get("type") == "user_message":
            projected_payload["message"] = _project_codex_user_message(payload.get("message"))
        projected["payload"] = projected_payload

    for container_name in ("data", "result", "response"):
        container = obj.get(container_name)
        if not isinstance(container, dict) or not isinstance(container.get("usage"), dict):
            continue
        projected[container_name] = _selected_fields(
            container,
            ("timestamp", "time", "created_at", "model", "model_name", "usage"),
        )
    return projected


def _open_codex_session_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def build_codex_projection_file(source_path: Path, dest_path: Path) -> dict[str, int]:
    lines: list[str] = []
    token_events = 0
    with _open_codex_session_text(source_path) as handle:
        for raw in handle:
            obj = _json_load(raw)
            if obj is None:
                continue
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if obj.get("type") == "event_msg" and payload.get("type") == "token_count":
                token_events += 1
            lines.append(json.dumps(_project_codex_record(obj), ensure_ascii=False, separators=(",", ":")))

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with dest_path.open("w", encoding="utf-8") as handle:
        if lines:
            handle.write("\n".join(lines))
            handle.write("\n")
    return {
        "source_bytes": source_path.stat().st_size,
        "dest_bytes": dest_path.stat().st_size,
        "token_events": token_events,
    }


def _project_codex_root(root: DiscoveredRoot, payload_root: Path) -> dict[str, int]:
    stats = {"files_written": 0, "source_bytes_total": 0, "dest_bytes_total": 0, "token_events_total": 0}

    source_shapes: list[tuple[str, Path]] = []
    if (root.source_path / "sessions").is_dir():
        source_shapes.append(("sessions", root.source_path / "sessions"))
    if (root.source_path / "archived_sessions").is_dir():
        source_shapes.append(("archived_sessions", root.source_path / "archived_sessions"))
    if root.source_path.name == "sessions":
        source_shapes.append(("sessions", root.source_path))
    if root.source_path.name == "archived_sessions":
        source_shapes.append(("archived_sessions", root.source_path))

    for bucket, source_root in source_shapes:
        for file_path in sorted(source_root.rglob("*.jsonl")):
            rel = file_path.relative_to(source_root)
            dest_path = payload_root / "codex" / bucket / root.root_id / rel
            item = build_codex_projection_file(file_path, dest_path)
            stats["files_written"] += 1
            stats["source_bytes_total"] += item["source_bytes"]
            stats["dest_bytes_total"] += item["dest_bytes"]
            stats["token_events_total"] += item["token_events"]
    return stats


def _project_gemini_root(root: DiscoveredRoot, payload_root: Path) -> dict[str, int]:
    source_root = root.source_path / "tmp" if (root.source_path / "tmp").is_dir() else root.source_path
    dest_root = payload_root / "gemini" / root.root_id
    stats = {"files_written": 0, "source_bytes_total": 0, "dest_bytes_total": 0}
    for file_path in sorted(source_root.rglob("*.json")):
        if file_path.parent.name != "chats":
            continue
        stats["files_written"] += 1
        stats["source_bytes_total"] += file_path.stat().st_size
        stats["dest_bytes_total"] += _copy_relative_file(source_root, file_path, dest_root)
    return stats


def _project_openclaw_content_item(item: object) -> object:
    if not isinstance(item, dict):
        return item

    item_type = item.get("type")
    if item_type == "text":
        projected = {"type": "text", "text": ""}
        if "textSignature" in item:
            projected["textSignature"] = item["textSignature"]
        return projected
    if item_type == "thinking":
        projected = {"type": "thinking", "thinking": ""}
        if "thinkingSignature" in item:
            projected["thinkingSignature"] = item["thinkingSignature"]
        return projected
    if item_type == "toolCall":
        projected = {"type": "toolCall"}
        for key in ("id", "name"):
            if key in item:
                projected[key] = item[key]
        if "arguments" in item:
            arguments = item["arguments"]
            projected["arguments"] = {} if isinstance(arguments, dict) else arguments
        if "partialJson" in item:
            projected["partialJson"] = ""
        return projected
    if item_type == "image":
        projected = {"type": "image"}
        if "mimeType" in item:
            projected["mimeType"] = item["mimeType"]
        if "data" in item:
            projected["data"] = ""
        return projected
    return item


def _openclaw_projected_relative_path(source_root: Path, file_path: Path) -> Path:
    rel = file_path.relative_to(source_root)
    name = rel.name
    if name.endswith(".jsonl") or ".reset." in name:
        return rel
    normalized_name = f"{name.replace('.jsonl', '__jsonl__')}.jsonl"
    return Path(*rel.parts[:-1]) / "_normalized" / normalized_name


def _project_openclaw_record(obj: dict) -> dict:
    if obj.get("type") != "message":
        return obj

    message = obj.get("message")
    if not isinstance(message, dict):
        return obj

    projected_message = {key: value for key, value in message.items() if key != "content"}
    content = message.get("content")
    if isinstance(content, list):
        projected_message["content"] = [_project_openclaw_content_item(item) for item in content]
    elif isinstance(content, str):
        projected_message["content"] = ""
    elif content is not None:
        projected_message["content"] = content

    projected = dict(obj)
    projected["message"] = projected_message
    return projected


def _build_openclaw_projection_file(source_path: Path, dest_path: Path) -> dict[str, int]:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("r", encoding="utf-8", errors="replace") as src, dest_path.open("w", encoding="utf-8") as dst:
        for raw in src:
            try:
                obj = json.loads(raw)
            except Exception:
                dst.write(raw)
                continue
            dst.write(json.dumps(_project_openclaw_record(obj), ensure_ascii=False, separators=(",", ":")))
            dst.write("\n")
    return {
        "source_bytes": source_path.stat().st_size,
        "dest_bytes": dest_path.stat().st_size,
    }


def _project_openclaw_root(root: DiscoveredRoot, payload_root: Path) -> dict[str, int]:
    source_root = root.source_path / "agents" if (root.source_path / "agents").is_dir() else root.source_path
    dest_root = payload_root / "openclaw" / root.root_id
    stats = {"files_written": 0, "source_bytes_total": 0, "dest_bytes_total": 0}
    for file_path in sorted(source_root.rglob("*")):
        if not file_path.is_file() or ".jsonl" not in file_path.name:
            continue
        dest_path = dest_root / _openclaw_projected_relative_path(source_root, file_path)
        item = _build_openclaw_projection_file(file_path, dest_path)
        stats["files_written"] += 1
        stats["source_bytes_total"] += item["source_bytes"]
        stats["dest_bytes_total"] += item["dest_bytes"]
    return stats


def _build_snapshot_id(machine_name: str) -> str:
    return f"{machine_name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"


def _projection_state_path(machine_root: Path) -> Path:
    return machine_root / LOCAL_PROJECTION_STATE_NAME


def _projection_roots_manifest_path(machine_root: Path) -> Path:
    return machine_root / LOCAL_PROJECTION_ROOTS_MANIFEST_NAME


def _projection_inventory_path(machine_root: Path) -> Path:
    return machine_root / LOCAL_PROJECTION_INVENTORY_NAME


def _load_json_file(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_file(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def local_home_projection_root(config: VaultConfig) -> Path:
    for machine in config.machines.values():
        if machine.import_name == LOCAL_HOME_IMPORT_NAME:
            raise ValueError(
                f"reserved local projection import name is already configured: {LOCAL_HOME_IMPORT_NAME}"
            )
    return config.paths.import_root / LOCAL_HOME_IMPORT_NAME


def _local_home_machine(config: VaultConfig) -> MachineConfig:
    return MachineConfig(
        name=LOCAL_HOME_IMPORT_NAME,
        import_name=LOCAL_HOME_IMPORT_NAME,
        ssh_target=None,
        source_home=config.paths.home,
        remote_relay_root=None,
        remote_state_root=None,
        sync_strategy=None,
        direct_max_delta_files=None,
        direct_max_delta_bytes=None,
        clients=LOCAL_HOME_CLIENTS,
        roots=tuple(
            RootRuleConfig(
                client=client,
                path=f"~/.{client}",
                glob=None,
                label="home",
                kind="home_root",
            )
            for client in LOCAL_HOME_CLIENTS
        ),
        root_globs=(),
    )


def _iter_local_projection_files(root: DiscoveredRoot) -> list[_LocalProjectionFile]:
    files: list[_LocalProjectionFile] = []
    if root.client == "codex":
        source_shapes: list[tuple[str, Path]] = []
        if (root.source_path / "sessions").is_dir():
            source_shapes.append(("sessions", root.source_path / "sessions"))
        if (root.source_path / "archived_sessions").is_dir():
            source_shapes.append(("archived_sessions", root.source_path / "archived_sessions"))
        for bucket, source_root in source_shapes:
            for source_path in sorted(source_root.rglob("*.jsonl")):
                files.append(
                    _LocalProjectionFile(
                        client="codex",
                        source_path=source_path,
                        relative_destination=Path("codex") / bucket / root.root_id / source_path.relative_to(source_root),
                    )
                )
        return files

    if root.client == "gemini":
        source_root = root.source_path / "tmp" if (root.source_path / "tmp").is_dir() else root.source_path
        for source_path in sorted(source_root.rglob("*.json")):
            if source_path.parent.name == "chats":
                files.append(
                    _LocalProjectionFile(
                        client="gemini",
                        source_path=source_path,
                        relative_destination=Path("gemini") / root.root_id / source_path.relative_to(source_root),
                    )
                )
        return files

    if root.client == "openclaw":
        source_root = root.source_path / "agents" if (root.source_path / "agents").is_dir() else root.source_path
        for source_path in sorted(source_root.rglob("*")):
            if source_path.is_file() and ".jsonl" in source_path.name:
                files.append(
                    _LocalProjectionFile(
                        client="openclaw",
                        source_path=source_path,
                        relative_destination=(
                            Path("openclaw")
                            / root.root_id
                            / _openclaw_projected_relative_path(source_root, source_path)
                        ),
                    )
                )
    return files


def _local_projection_signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _load_local_home_state(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"schema_version": 1, "files": {}}
    try:
        payload = _load_json_file(path)
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "files": {}}
    if (
        not isinstance(payload, dict)
        or payload.get("projector_version") != CODEX_PROJECTION_VERSION
        or not isinstance(payload.get("files"), dict)
    ):
        return {"schema_version": 1, "files": {}}
    return payload


def _project_local_file(item: _LocalProjectionFile, destination: Path) -> dict[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        if item.client == "codex":
            result = build_codex_projection_file(item.source_path, temporary)
        elif item.client == "gemini":
            shutil.copy2(item.source_path, temporary)
            result = {
                "source_bytes": item.source_path.stat().st_size,
                "dest_bytes": temporary.stat().st_size,
                "token_events": 0,
            }
        elif item.client == "openclaw":
            result = _build_openclaw_projection_file(item.source_path, temporary)
            result["token_events"] = 0
        else:
            raise ValueError(f"unsupported local projection client: {item.client}")
        temporary.replace(destination)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def _prepare_projection_home(config: VaultConfig) -> None:
    projection_home = config.paths.projection_home
    projection_home.mkdir(parents=True, exist_ok=True)
    for client in LOCAL_HOME_CLIENTS:
        unexpected_root = projection_home / f".{client}"
        if unexpected_root.exists() or unexpected_root.is_symlink():
            raise ValueError(f"projection HOME must not contain a live client root: {unexpected_root}")

    runtime_config_root = projection_home / ".config" / "tokscale"
    runtime_config_root.mkdir(parents=True, exist_ok=True)
    source_config_root = config.paths.home / ".config" / "tokscale"
    for name in ("credentials.json", "device.json"):
        source = source_config_root / name
        destination = runtime_config_root / name
        if not source.is_file():
            continue
        if destination.is_symlink():
            if destination.resolve() == source.resolve():
                continue
            destination.unlink()
        elif destination.exists():
            raise ValueError(f"projection HOME runtime config path already exists: {destination}")
        destination.symlink_to(source)


def refresh_local_home_projection(config: VaultConfig, *, dry_run: bool = False) -> LocalHomeProjectionResult:
    machine_root = local_home_projection_root(config)
    raw_root = machine_root / ".raw"
    state_path = machine_root / LOCAL_HOME_STATE_NAME
    machine = _local_home_machine(config)
    roots = discover_machine_roots(machine, config.paths.home)
    state = _load_local_home_state(state_path)
    state_files = state["files"]
    assert isinstance(state_files, dict)

    files_seen = 0
    files_written = 0
    files_skipped = 0
    source_bytes_total = 0
    dest_bytes_total = 0
    token_events_total = 0
    seen_clients: set[str] = set()

    for root in roots:
        seen_clients.add(root.client)
        for item in _iter_local_projection_files(root):
            files_seen += 1
            signature = _local_projection_signature(item.source_path)
            source_key = str(item.source_path.resolve())
            destination = raw_root / item.relative_destination
            previous = state_files.get(source_key)
            if (
                isinstance(previous, dict)
                and previous.get("size") == signature["size"]
                and previous.get("mtime_ns") == signature["mtime_ns"]
                and destination.is_file()
            ):
                files_skipped += 1
                continue
            files_written += 1
            if dry_run:
                continue
            result = _project_local_file(item, destination)
            source_bytes_total += result["source_bytes"]
            dest_bytes_total += result["dest_bytes"]
            token_events_total += result.get("token_events", 0)
            state_files[source_key] = {
                **signature,
                "client": item.client,
                "destination": str(destination),
                "projected_at": datetime.now(UTC).isoformat(),
            }

    if not dry_run:
        _prepare_projection_home(config)
        roots_manifest = {
            "machine": machine.name,
            "import_name": machine.import_name,
            "generated_at": datetime.now(UTC).isoformat(),
            "roots": [
                {
                    "root_id": root.root_id,
                    "client": root.client,
                    "source_path": str(root.source_path),
                    "label": root.label,
                    "kind": root.kind,
                }
                for root in roots
            ],
        }
        _write_json_file(machine_root / LOCAL_PROJECTION_ROOTS_MANIFEST_NAME, roots_manifest)
        state.update(
            {
                "schema_version": 1,
                "projector_version": CODEX_PROJECTION_VERSION,
                "status": "valid",
                "source_home": str(config.paths.home),
                "projection_home": str(config.paths.projection_home),
                "updated_at": datetime.now(UTC).isoformat(),
                "files": state_files,
            }
        )
        _write_json_file(state_path, state)

    return LocalHomeProjectionResult(
        machine_root=machine_root,
        projection_home=config.paths.projection_home,
        state_path=state_path,
        files_seen=files_seen,
        files_written=files_written,
        files_skipped=files_skipped,
        source_bytes_total=source_bytes_total,
        dest_bytes_total=dest_bytes_total,
        token_events_total=token_events_total,
        clients=tuple(sorted(seen_clients)),
        dry_run=dry_run,
    )


def local_home_projection_payload(result: LocalHomeProjectionResult) -> dict[str, object]:
    return {
        "status": "planned" if result.dry_run else "projected",
        "machine_root": str(result.machine_root),
        "projection_home": str(result.projection_home),
        "state_path": str(result.state_path),
        "files_seen": result.files_seen,
        "files_written": result.files_written,
        "files_skipped": result.files_skipped,
        "source_bytes_total": result.source_bytes_total,
        "dest_bytes_total": result.dest_bytes_total,
        "token_events_total": result.token_events_total,
        "clients": list(result.clients),
    }


def _load_current_snapshot_id(machine_root: Path | None) -> str | None:
    if machine_root is None:
        return None
    state_path = _projection_state_path(machine_root)
    if not state_path.is_file():
        return None
    payload = _load_json_file(state_path)
    if not isinstance(payload, dict):
        return None
    snapshot_id = payload.get("current_snapshot_id")
    return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None


def _build_projection_payload(machine: MachineConfig, source_home: Path, payload_root: Path) -> tuple[dict[str, object], dict[str, int]]:
    discovered = discover_machine_roots(machine, source_home)
    roots_manifest: dict[str, object] = {
        "machine": machine.name,
        "import_name": machine.import_name,
        "projector_versions": {"codex": CODEX_PROJECTION_VERSION},
        "generated_at": datetime.now(UTC).isoformat(),
        "roots": [],
    }
    summary = {"files_written": 0, "source_bytes_total": 0, "dest_bytes_total": 0, "token_events_total": 0}

    for root in discovered:
        if root.client == "codex":
            item = _project_codex_root(root, payload_root)
        elif root.client == "gemini":
            item = _project_gemini_root(root, payload_root)
        elif root.client == "openclaw":
            item = _project_openclaw_root(root, payload_root)
        else:
            continue
        roots_manifest["roots"].append(
            {
                "root_id": root.root_id,
                "client": root.client,
                "source_path": str(root.source_path),
                "label": root.label,
                "kind": root.kind,
            }
        )
        for key, value in item.items():
            if key in summary:
                summary[key] += value

    return roots_manifest, summary


def _build_projection_inventory(payload_root: Path) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    for file_path in sorted(path for path in payload_root.rglob("*") if path.is_file()):
        rel = file_path.relative_to(payload_root).as_posix()
        if rel in PROJECTION_METADATA_NAMES:
            continue
        inventory.append(
            {
                "path": rel,
                "sha256": _sha256_file(file_path),
                "bytes": file_path.stat().st_size,
            }
        )
    return inventory


def _inventory_index(inventory: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    index: dict[str, dict[str, object]] = {}
    for item in inventory:
        rel = item.get("path")
        if isinstance(rel, str):
            index[rel] = item
    return index


def _diff_projection_inventory(
    previous: list[dict[str, object]],
    current: list[dict[str, object]],
) -> tuple[list[str], list[str]]:
    previous_index = _inventory_index(previous)
    current_index = _inventory_index(current)

    changed = sorted(
        rel
        for rel, item in current_index.items()
        if rel not in previous_index or previous_index[rel].get("sha256") != item.get("sha256")
    )
    deleted = sorted(rel for rel in previous_index if rel not in current_index)
    return changed, deleted


def _write_projection_metadata(
    payload_root: Path,
    roots_manifest: dict[str, object],
    inventory: list[dict[str, object]],
) -> tuple[Path, Path]:
    roots_manifest_path = payload_root / PROJECTION_ROOTS_MANIFEST_NAME
    inventory_path = payload_root / PROJECTION_INVENTORY_NAME
    _write_json_file(roots_manifest_path, roots_manifest)
    _write_json_file(inventory_path, inventory)
    return roots_manifest_path, inventory_path


def _roots_manifest_identity(roots_manifest: object) -> object:
    if not isinstance(roots_manifest, dict):
        return roots_manifest
    return {
        "machine": roots_manifest.get("machine"),
        "import_name": roots_manifest.get("import_name"),
        "projector_versions": roots_manifest.get("projector_versions"),
        "roots": roots_manifest.get("roots"),
    }


def _copy_projection_subset(source_root: Path, dest_root: Path, relative_paths: list[str]) -> dict[str, int]:
    stats = {"files_written": 0, "source_bytes_total": 0, "dest_bytes_total": 0, "token_events_total": 0}
    for rel in relative_paths:
        source_path = source_root / Path(rel)
        if not source_path.is_file():
            raise FileNotFoundError(f"projection source file missing from staging payload: {source_path}")
        dest_path = dest_root / Path(rel)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, dest_path)
        file_bytes = source_path.stat().st_size
        stats["files_written"] += 1
        stats["source_bytes_total"] += file_bytes
        stats["dest_bytes_total"] += file_bytes
    return stats


def export_machine_projection(
    machine: MachineConfig,
    source_home: Path,
    relay_root: Path,
    machine_root: Path | None = None,
    base_snapshot_id: str | None = None,
) -> ProjectionBundle:
    source_home = source_home.expanduser().resolve()
    relay_root = relay_root.expanduser().resolve()
    machine_root = machine_root.expanduser().resolve() if machine_root is not None else None
    snapshot_id = _build_snapshot_id(machine.name)
    bundle_dir = relay_root / "projection" / machine.name / snapshot_id
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    staging_dir = bundle_dir / ".staging"
    payload_root = staging_dir / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)

    roots_manifest, full_summary = _build_projection_payload(machine, source_home, payload_root)
    full_roots_manifest_path, full_inventory_path = _write_projection_metadata(
        payload_root,
        roots_manifest,
        _build_projection_inventory(payload_root),
    )

    mode = "projection_full"
    resolved_base_snapshot_id: str | None = None
    fallback_reason: str | None = None
    changed_files: list[str] = []
    deleted_files: list[str] = []
    payload_source = payload_root
    roots_manifest_path = full_roots_manifest_path
    inventory_path = full_inventory_path
    summary = dict(full_summary)

    local_snapshot_id = base_snapshot_id or _load_current_snapshot_id(machine_root)
    if local_snapshot_id is None:
        fallback_reason = "missing_local_state"
    else:
        resolved_base_snapshot_id = local_snapshot_id
        previous_bundle_dir = relay_root / "projection" / machine.name / resolved_base_snapshot_id
        previous_roots_manifest_path = previous_bundle_dir / PROJECTION_ROOTS_MANIFEST_NAME
        previous_inventory_path = previous_bundle_dir / PROJECTION_INVENTORY_NAME
        if not previous_roots_manifest_path.is_file() or not previous_inventory_path.is_file():
            fallback_reason = "missing_base_snapshot"
            resolved_base_snapshot_id = None
        else:
            previous_roots_manifest = _load_json_file(previous_roots_manifest_path)
            if _roots_manifest_identity(previous_roots_manifest) != _roots_manifest_identity(roots_manifest):
                fallback_reason = "roots_manifest_changed"
                resolved_base_snapshot_id = None
            else:
                previous_inventory = _load_json_file(previous_inventory_path)
                if not isinstance(previous_inventory, list):
                    raise ValueError(f"invalid projection inventory: {previous_inventory_path}")
                current_inventory = _load_json_file(full_inventory_path)
                if not isinstance(current_inventory, list):
                    raise ValueError(f"invalid projection inventory: {full_inventory_path}")
                changed_files, deleted_files = _diff_projection_inventory(previous_inventory, current_inventory)
                delta_payload_root = staging_dir / "delta-payload"
                delta_payload_root.mkdir(parents=True, exist_ok=True)
                summary = _copy_projection_subset(payload_root, delta_payload_root, changed_files)
                roots_manifest_path, inventory_path = _write_projection_metadata(
                    delta_payload_root,
                    roots_manifest,
                    current_inventory,
                )
                payload_source = delta_payload_root
                mode = "projection_delta"

    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundle_dir / "payload.tar.zst"
    manifest_path = bundle_dir / "manifest.json"
    _pack_to_bundle_path(payload_source, bundle_path)
    bundle_roots_manifest_path = bundle_dir / PROJECTION_ROOTS_MANIFEST_NAME
    bundle_inventory_path = bundle_dir / PROJECTION_INVENTORY_NAME
    shutil.copy2(roots_manifest_path, bundle_roots_manifest_path)
    shutil.copy2(inventory_path, bundle_inventory_path)

    manifest_payload = {
        "version": 1,
        "mode": mode,
        "machine": machine.name,
        "import_name": machine.import_name,
        "snapshot_id": snapshot_id,
        "created_at": datetime.now(UTC).isoformat(),
        "bundle": {
            "name": bundle_path.name,
            "bytes": bundle_path.stat().st_size,
            "sha256": _sha256_file(bundle_path),
        },
        "roots_manifest": {"name": bundle_roots_manifest_path.name},
        "inventory": {
            "name": bundle_inventory_path.name,
            "bytes": bundle_inventory_path.stat().st_size,
            "sha256": _sha256_file(bundle_inventory_path),
        },
        "summary": summary,
    }
    if resolved_base_snapshot_id is not None:
        manifest_payload["base_snapshot_id"] = resolved_base_snapshot_id
    if fallback_reason is not None:
        manifest_payload["fallback_reason"] = fallback_reason
    if mode == "projection_delta":
        manifest_payload["changed_files"] = changed_files
        manifest_payload["deleted_files"] = deleted_files
    manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    shutil.rmtree(staging_dir)

    return ProjectionBundle(
        machine_name=machine.name,
        snapshot_id=snapshot_id,
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        bundle_path=bundle_path,
        roots_manifest_path=bundle_roots_manifest_path,
        inventory_path=bundle_inventory_path,
        bundle_bytes=bundle_path.stat().st_size,
        mode=mode,
        base_snapshot_id=resolved_base_snapshot_id,
        fallback_reason=fallback_reason,
    )


def import_machine_projection(
    config: VaultConfig,
    machine_name: str,
    bundle_dir: Path,
    canonicalize_command: str | None = None,
) -> ProjectionBundle:
    bundle_dir = bundle_dir.expanduser().resolve()
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"projection manifest not found: {manifest_path}")

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle_name = str(payload.get("bundle", {}).get("name") or "payload.tar.zst")
    bundle_path = bundle_dir / bundle_name
    if not bundle_path.is_file():
        raise FileNotFoundError(f"projection payload not found: {bundle_path}")
    if payload.get("machine") != machine_name:
        raise ValueError(f"bundle machine mismatch: expected {machine_name}, got {payload.get('machine')}")
    actual_sha256 = _sha256_file(bundle_path)
    expected_sha256 = payload.get("bundle", {}).get("sha256")
    if actual_sha256 != expected_sha256:
        raise ValueError(f"bundle sha256 mismatch: expected {expected_sha256}, got {actual_sha256}")

    machine = config.machines[machine_name]
    machine_root = config.paths.import_root / machine.import_name
    raw_root = machine_root / ".raw"
    extract_root = bundle_dir / ".extract"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["bsdtar", "-xf", str(bundle_path), "-C", str(extract_root)], check=True)

    mode = str(payload.get("mode") or "projection_full")
    if mode == "projection_full":
        for client in ("codex", "gemini", "openclaw"):
            if (extract_root / client).is_dir():
                # Keep the imported `.raw` tree append-only so Tokscale can
                # continue submitting historical usage after upstream cleanup.
                _copy_tree(extract_root / client, raw_root / client)
    elif mode == "projection_delta":
        expected_base_snapshot_id = payload.get("base_snapshot_id")
        current_snapshot_id = _load_current_snapshot_id(machine_root)
        if current_snapshot_id != expected_base_snapshot_id:
            raise ValueError(
                f"base snapshot mismatch: expected {expected_base_snapshot_id}, got {current_snapshot_id}"
            )
        changed_files = payload.get("changed_files")
        deleted_files = payload.get("deleted_files")
        if not isinstance(changed_files, list) or not isinstance(deleted_files, list):
            raise ValueError(f"invalid delta manifest in {manifest_path}")
        for rel in changed_files:
            if not isinstance(rel, str):
                raise ValueError(f"invalid changed file entry in {manifest_path}: {rel!r}")
            source_path = extract_root / Path(rel)
            if not source_path.is_file():
                raise FileNotFoundError(f"projection delta file missing from bundle: {source_path}")
            dest_path = raw_root / Path(rel)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, dest_path)
        for rel in deleted_files:
            if not isinstance(rel, str):
                raise ValueError(f"invalid deleted file entry in {manifest_path}: {rel!r}")
    else:
        raise ValueError(f"unsupported projection bundle mode: {mode}")

    roots_manifest_path = extract_root / PROJECTION_ROOTS_MANIFEST_NAME
    inventory_path = extract_root / PROJECTION_INVENTORY_NAME
    stored_roots_manifest = _projection_roots_manifest_path(machine_root)
    stored_inventory_path = _projection_inventory_path(machine_root)
    machine_root.mkdir(parents=True, exist_ok=True)
    if roots_manifest_path.is_file():
        shutil.copy2(roots_manifest_path, stored_roots_manifest)
    if inventory_path.is_file():
        shutil.copy2(inventory_path, stored_inventory_path)

    _write_json_file(
        _projection_state_path(machine_root),
        {
            "current_snapshot_id": payload.get("snapshot_id"),
            "last_imported_at": datetime.now(UTC).isoformat(),
        },
    )
    shutil.rmtree(extract_root)

    if canonicalize_command:
        subprocess.run([canonicalize_command, "--machine-root", str(machine_root)], check=True)

    projection_state = payload.get("projection_state")
    if not isinstance(projection_state, dict):
        projection_state = {}
    return ProjectionBundle(
        machine_name=machine_name,
        snapshot_id=str(payload.get("snapshot_id")),
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        bundle_path=bundle_path,
        roots_manifest_path=stored_roots_manifest,
        inventory_path=stored_inventory_path if stored_inventory_path.exists() else None,
        bundle_bytes=int(payload.get("bundle", {}).get("bytes", 0)),
        mode=mode,
        base_snapshot_id=payload.get("base_snapshot_id") if isinstance(payload.get("base_snapshot_id"), str) else None,
        fallback_reason=payload.get("fallback_reason") if isinstance(payload.get("fallback_reason"), str) else None,
        state_status=(
            projection_state.get("status") if isinstance(projection_state.get("status"), str) else None
        ),
        files_seen=int(projection_state.get("files_seen", 0)),
        files_projected=int(projection_state.get("files_projected", 0)),
        files_reused=int(projection_state.get("files_reused", 0)),
    )


def pending_projection_bundle_dirs(config: VaultConfig, machine_name: str) -> list[Path]:
    bundle_root = config.paths.relay_root / "projection" / machine_name
    if not bundle_root.is_dir():
        return []
    machine = config.machines[machine_name]
    machine_root = config.paths.import_root / machine.import_name
    current_snapshot_id = _load_current_snapshot_id(machine_root)

    candidates: list[tuple[str, str, str | None, Path]] = []
    for manifest_path in sorted(bundle_root.glob("*/manifest.json")):
        bundle_snapshot_id = manifest_path.parent.name
        if isinstance(current_snapshot_id, str) and bundle_snapshot_id <= current_snapshot_id:
            continue
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        snapshot_id = payload.get("snapshot_id")
        mode = payload.get("mode")
        base_snapshot_id = payload.get("base_snapshot_id")
        if not isinstance(snapshot_id, str) or not isinstance(mode, str):
            continue
        if isinstance(current_snapshot_id, str) and snapshot_id <= current_snapshot_id:
            continue
        candidates.append((snapshot_id, mode, base_snapshot_id if isinstance(base_snapshot_id, str) else None, manifest_path.parent))
    if not candidates:
        return []
    candidates.sort(key=lambda item: item[0])

    pending: list[Path] = []
    if current_snapshot_id is None:
        applicable_fulls = [item for item in candidates if item[1] == "projection_full"]
        if not applicable_fulls:
            return []
        snapshot_id, _, _, bundle_dir = applicable_fulls[-1]
        pending.append(bundle_dir)
        current_snapshot_id = snapshot_id

    while current_snapshot_id is not None:
        applicable = [
            item
            for item in candidates
            if item[0] > current_snapshot_id
            and (item[1] == "projection_full" or (item[1] == "projection_delta" and item[2] == current_snapshot_id))
        ]
        if not applicable:
            break
        snapshot_id, _, _, bundle_dir = applicable[-1]
        pending.append(bundle_dir)
        current_snapshot_id = snapshot_id
    return pending


def expected_local_projection_bundle_dir(config: VaultConfig, machine_name: str, snapshot_id: str) -> Path:
    return config.paths.relay_root / "projection" / machine_name / snapshot_id


def _remote_helper_source() -> str:
    return """
from __future__ import annotations

from datetime import datetime, timezone
import base64
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

PROJECTION_ROOTS_MANIFEST_NAME = "roots-manifest.json"
PROJECTION_INVENTORY_NAME = "inventory.json"
CODEX_PROJECTION_VERSION = 2
PROJECTION_STATE_SCHEMA_VERSION = 1
CODEX_SYSTEM_INJECTED_PREFIXES = (
    "<environment_context>",
    "<system-reminder>",
    "<user_instructions>",
)


def _json_load(line):
    try:
        value = json.loads(line)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _sanitize_slug(value):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "root"


def _expand_user_like(raw, source_home):
    if raw == "~":
        return source_home
    if raw.startswith("~/"):
        return source_home / raw[2:]
    return Path(raw)


def _derive_root_label(path, fallback):
    candidates = [path.name, path.parent.name, fallback]
    for candidate in candidates:
        cleaned = candidate.lstrip(".")
        if cleaned and cleaned not in {"sessions", "archived_sessions", "tmp", "agents"}:
            return cleaned
    return fallback


def _root_id(rule, source_path):
    base_label = rule.get("label") or _derive_root_label(source_path, rule["client"])
    digest = hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:8]
    return f"{_sanitize_slug(base_label)}-{digest}"


def discover_machine_roots(rules, source_home):
    discovered = {}
    for rule in rules:
        client = rule["client"]
        if "path" in rule and rule["path"]:
            candidate_paths = [_expand_user_like(rule["path"], source_home)]
        else:
            expanded = _expand_user_like(rule["glob"], source_home)
            candidate_paths = [Path(match) for match in sorted(glob.glob(str(expanded), recursive=True))]
        for candidate in candidate_paths:
            if not candidate.is_dir():
                continue
            resolved = candidate.resolve()
            key = (client, str(resolved))
            if key in discovered:
                continue
            discovered[key] = {
                "client": client,
                "root_id": _root_id(rule, resolved),
                "source_path": resolved,
                "label": rule.get("label"),
                "kind": rule.get("kind"),
            }
    return [discovered[key] for key in sorted(discovered, key=lambda item: (item[0], item[1]))]


def _pack_to_bundle_path(source, bundle_path):
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    if bundle_path.exists():
        bundle_path.unlink()
    if bundle_path.suffixes[-2:] == [".tar", ".zst"] and shutil.which("zstd"):
        subprocess.run(["bsdtar", "--zstd", "-cf", str(bundle_path), "-C", str(source), "."], check=True)
        return
    with tarfile.open(bundle_path, "w:gz") as tar:
        tar.add(source, arcname=".")


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_file(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\\n", encoding="utf-8")


def _selected_fields(value, names):
    return {name: value[name] for name in names if name in value}


def _project_codex_user_message(value):
    if not isinstance(value, str):
        return ""
    stripped = value.lstrip()
    for prefix in CODEX_SYSTEM_INJECTED_PREFIXES:
        if stripped.startswith(prefix):
            return prefix
    return "user"


def _project_codex_record(obj):
    projected = _selected_fields(obj, ("timestamp", "time", "created_at", "type", "model", "model_name", "usage"))
    obj_type = obj.get("type")
    payload = obj.get("payload")
    if isinstance(payload, dict):
        if obj_type == "session_meta":
            payload_fields = (
                "id",
                "forked_from_id",
                "source",
                "thread_source",
                "cwd",
                "model_provider",
                "agent_nickname",
            )
        elif obj_type == "turn_context":
            payload_fields = ("type", "model", "model_name", "model_info", "turn_id")
        else:
            payload_fields = ("type", "model", "model_name", "model_info", "info", "turn_id")
        projected_payload = _selected_fields(payload, payload_fields)
        if obj_type == "event_msg" and payload.get("type") == "user_message":
            projected_payload["message"] = _project_codex_user_message(payload.get("message"))
        projected["payload"] = projected_payload

    for container_name in ("data", "result", "response"):
        container = obj.get(container_name)
        if not isinstance(container, dict) or not isinstance(container.get("usage"), dict):
            continue
        projected[container_name] = _selected_fields(
            container,
            ("timestamp", "time", "created_at", "model", "model_name", "usage"),
        )
    return projected


def _build_codex_projection_file(source_path, dest_path):
    lines = []
    token_events = 0
    with source_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            obj = _json_load(raw)
            if obj is None:
                continue
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if obj.get("type") == "event_msg" and payload.get("type") == "token_count":
                token_events += 1
            lines.append(json.dumps(_project_codex_record(obj), ensure_ascii=False, separators=(",", ":")))
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with dest_path.open("w", encoding="utf-8") as handle:
        if lines:
            handle.write("\\n".join(lines))
            handle.write("\\n")
    return {
        "source_bytes": source_path.stat().st_size,
        "dest_bytes": dest_path.stat().st_size,
        "token_events": token_events,
    }


def _project_openclaw_content_item(item):
    if not isinstance(item, dict):
        return item
    item_type = item.get("type")
    if item_type == "text":
        projected = {"type": "text", "text": ""}
        if "textSignature" in item:
            projected["textSignature"] = item["textSignature"]
        return projected
    if item_type == "thinking":
        projected = {"type": "thinking", "thinking": ""}
        if "thinkingSignature" in item:
            projected["thinkingSignature"] = item["thinkingSignature"]
        return projected
    if item_type == "toolCall":
        projected = {"type": "toolCall"}
        for key in ("id", "name"):
            if key in item:
                projected[key] = item[key]
        if "arguments" in item:
            arguments = item["arguments"]
            projected["arguments"] = {} if isinstance(arguments, dict) else arguments
        if "partialJson" in item:
            projected["partialJson"] = ""
        return projected
    if item_type == "image":
        projected = {"type": "image"}
        if "mimeType" in item:
            projected["mimeType"] = item["mimeType"]
        if "data" in item:
            projected["data"] = ""
        return projected
    return item


def _openclaw_projected_relative_path(source_root, file_path):
    rel = file_path.relative_to(source_root)
    name = rel.name
    if name.endswith(".jsonl") or ".reset." in name:
        return rel
    normalized_name = f"{name.replace('.jsonl', '__jsonl__')}.jsonl"
    return Path(*rel.parts[:-1]) / "_normalized" / normalized_name


def _project_openclaw_record(obj):
    if obj.get("type") != "message":
        return obj
    message = obj.get("message")
    if not isinstance(message, dict):
        return obj
    projected_message = {key: value for key, value in message.items() if key != "content"}
    content = message.get("content")
    if isinstance(content, list):
        projected_message["content"] = [_project_openclaw_content_item(item) for item in content]
    elif isinstance(content, str):
        projected_message["content"] = ""
    elif content is not None:
        projected_message["content"] = content
    projected = dict(obj)
    projected["message"] = projected_message
    return projected


def _build_openclaw_projection_file(source_path, dest_path):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("r", encoding="utf-8", errors="replace") as src, dest_path.open("w", encoding="utf-8") as dst:
        for raw in src:
            try:
                obj = json.loads(raw)
            except Exception:
                dst.write(raw)
                continue
            dst.write(json.dumps(_project_openclaw_record(obj), ensure_ascii=False, separators=(",", ":")))
            dst.write("\\n")
    return {
        "source_bytes": source_path.stat().st_size,
        "dest_bytes": dest_path.stat().st_size,
    }


def _add_projection_file(items, client, source_path, relative_destination):
    rel = relative_destination.as_posix()
    existing = items.get(rel)
    if existing is not None and existing["source_path"] != source_path:
        raise RuntimeError(f"projection destination collision: {rel}")
    items[rel] = {
        "client": client,
        "source_path": source_path,
        "destination": rel,
    }


def _build_projection_plan(machine_name, import_name, roots, source_home):
    discovered = discover_machine_roots(roots, source_home)
    roots_manifest = {
        "machine": machine_name,
        "import_name": import_name,
        "projector_versions": {"codex": CODEX_PROJECTION_VERSION},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": [],
    }
    items = {}
    for root in discovered:
        roots_manifest["roots"].append(
            {
                "root_id": root["root_id"],
                "client": root["client"],
                "source_path": str(root["source_path"]),
                "label": root.get("label"),
                "kind": root.get("kind"),
            }
        )
        client = root["client"]
        source_path = root["source_path"]
        if client == "codex":
            source_shapes = []
            if (source_path / "sessions").is_dir():
                source_shapes.append(("sessions", source_path / "sessions"))
            if (source_path / "archived_sessions").is_dir():
                source_shapes.append(("archived_sessions", source_path / "archived_sessions"))
            if source_path.name == "sessions":
                source_shapes.append(("sessions", source_path))
            if source_path.name == "archived_sessions":
                source_shapes.append(("archived_sessions", source_path))
            for bucket, source_root in source_shapes:
                for file_path in sorted(source_root.rglob("*.jsonl")):
                    _add_projection_file(
                        items,
                        client,
                        file_path,
                        Path("codex") / bucket / root["root_id"] / file_path.relative_to(source_root),
                    )
        elif client == "gemini":
            source_root = source_path / "tmp" if (source_path / "tmp").is_dir() else source_path
            for file_path in sorted(source_root.rglob("*.json")):
                if file_path.parent.name == "chats":
                    _add_projection_file(
                        items,
                        client,
                        file_path,
                        Path("gemini") / root["root_id"] / file_path.relative_to(source_root),
                    )
        elif client == "openclaw":
            source_root = source_path / "agents" if (source_path / "agents").is_dir() else source_path
            for file_path in sorted(source_root.rglob("*")):
                if file_path.is_file() and ".jsonl" in file_path.name:
                    _add_projection_file(
                        items,
                        client,
                        file_path,
                        Path("openclaw") / root["root_id"] / _openclaw_projected_relative_path(source_root, file_path),
                    )
    return roots_manifest, [items[rel] for rel in sorted(items)]


def _projection_signature(path):
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _load_projection_state(path, roots_identity):
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != PROJECTION_STATE_SCHEMA_VERSION:
        return None
    if payload.get("projector_versions") != {"codex": CODEX_PROJECTION_VERSION}:
        return None
    if payload.get("roots_identity") != roots_identity:
        return None
    if not isinstance(payload.get("files"), dict):
        return None
    return payload


def _project_to_blob(item, blob_root):
    source_path = item["source_path"]
    before = _projection_signature(source_path)
    blob_root.mkdir(parents=True, exist_ok=True)
    temp_name = hashlib.sha1(item["destination"].encode("utf-8")).hexdigest()
    temporary = blob_root / f".{os.getpid()}-{temp_name}.tmp"
    if temporary.exists():
        temporary.unlink()
    try:
        if item["client"] == "codex":
            result = _build_codex_projection_file(source_path, temporary)
        elif item["client"] == "gemini":
            shutil.copy2(source_path, temporary)
            result = {
                "source_bytes": source_path.stat().st_size,
                "dest_bytes": temporary.stat().st_size,
                "token_events": 0,
            }
        elif item["client"] == "openclaw":
            result = _build_openclaw_projection_file(source_path, temporary)
            result["token_events"] = 0
        else:
            raise RuntimeError(f"unsupported projection client: {item['client']}")
        after = _projection_signature(source_path)
        if after != before:
            raise RuntimeError(f"source changed during projection: {source_path}")
        digest = _sha256_file(temporary)
        blob_path = blob_root / digest
        if blob_path.is_file():
            temporary.unlink()
        else:
            temporary.replace(blob_path)
        return {
            "client": item["client"],
            "source_path": str(source_path),
            "size": before["size"],
            "mtime_ns": before["mtime_ns"],
            "sha256": digest,
            "source_bytes": int(result["source_bytes"]),
            "dest_bytes": int(result["dest_bytes"]),
            "token_events": int(result.get("token_events", 0)),
        }
    finally:
        if temporary.exists():
            temporary.unlink()


def _refresh_projection_files(items, previous_state, blob_root):
    previous_files = previous_state.get("files", {}) if previous_state else {}
    current_files = {}
    projected = []
    reused = 0
    for item in items:
        rel = item["destination"]
        signature = _projection_signature(item["source_path"])
        previous = previous_files.get(rel)
        reusable = isinstance(previous, dict)
        if reusable:
            blob_path = blob_root / str(previous.get("sha256") or "")
            reusable = (
                previous.get("source_path") == str(item["source_path"])
                and previous.get("client") == item["client"]
                and previous.get("size") == signature["size"]
                and previous.get("mtime_ns") == signature["mtime_ns"]
                and blob_path.is_file()
                and blob_path.stat().st_size == previous.get("dest_bytes")
            )
        if reusable:
            current_files[rel] = previous
            reused += 1
            continue
        current_files[rel] = _project_to_blob(item, blob_root)
        projected.append(rel)
    deleted = sorted(rel for rel in previous_files if rel not in current_files)
    return current_files, projected, reused, deleted


def _projection_inventory(files):
    return [
        {"path": rel, "sha256": files[rel]["sha256"], "bytes": files[rel]["dest_bytes"]}
        for rel in sorted(files)
    ]


def _summary_for_paths(files, relative_paths):
    return {
        "files_written": len(relative_paths),
        "source_bytes_total": sum(int(files[rel]["source_bytes"]) for rel in relative_paths),
        "dest_bytes_total": sum(int(files[rel]["dest_bytes"]) for rel in relative_paths),
        "token_events_total": sum(int(files[rel].get("token_events", 0)) for rel in relative_paths),
    }


def _materialize_projection(files, blob_root, payload_root, relative_paths):
    for rel in relative_paths:
        source_path = blob_root / files[rel]["sha256"]
        if not source_path.is_file():
            raise FileNotFoundError(f"projection blob missing: {source_path}")
        dest_path = payload_root / Path(rel)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(str(source_path), str(dest_path))
        except OSError:
            shutil.copy2(source_path, dest_path)


def _write_projection_state(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
    temporary.replace(path)


def _cleanup_projection_blobs(blob_root, files):
    referenced = {item["sha256"] for item in files.values()}
    if not blob_root.is_dir():
        return
    for path in blob_root.iterdir():
        if path.is_file() and path.name not in referenced:
            path.unlink()


def _inventory_index(inventory):
    index = {}
    for item in inventory:
        rel = item.get("path")
        if isinstance(rel, str):
            index[rel] = item
    return index


def _diff_projection_inventory(previous, current):
    previous_index = _inventory_index(previous)
    current_index = _inventory_index(current)
    changed = sorted(
        rel
        for rel, item in current_index.items()
        if rel not in previous_index or previous_index[rel].get("sha256") != item.get("sha256")
    )
    deleted = sorted(rel for rel in previous_index if rel not in current_index)
    return changed, deleted


def _write_projection_metadata(payload_root, roots_manifest, inventory):
    roots_manifest_path = payload_root / PROJECTION_ROOTS_MANIFEST_NAME
    inventory_path = payload_root / PROJECTION_INVENTORY_NAME
    _write_json_file(roots_manifest_path, roots_manifest)
    _write_json_file(inventory_path, inventory)
    return roots_manifest_path, inventory_path


def _roots_manifest_identity(roots_manifest):
    if not isinstance(roots_manifest, dict):
        return roots_manifest
    return {
        "machine": roots_manifest.get("machine"),
        "import_name": roots_manifest.get("import_name"),
        "projector_versions": roots_manifest.get("projector_versions"),
        "roots": roots_manifest.get("roots"),
    }


def main():
    encoded = os.environ.get("ASV_REQUEST_B64")
    if not encoded:
        raise RuntimeError("missing ASV_REQUEST_B64")
    request = json.loads(base64.b64decode(encoded.encode("ascii")).decode("utf-8"))
    machine_name = request["machine_name"]
    import_name = request.get("import_name") or machine_name
    source_home = Path(request["source_home"]).expanduser().resolve()
    relay_root = Path(request["relay_root"]).expanduser().resolve()
    state_root_raw = request.get("state_root")
    roots = request["roots"]
    requested_base_snapshot_id = request.get("base_snapshot_id")
    snapshot_id = f"{machine_name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    bundle_dir = relay_root / "projection" / machine_name / snapshot_id
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    staging_dir = bundle_dir / ".staging"
    payload_root = staging_dir / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)

    roots_manifest, projection_items = _build_projection_plan(machine_name, import_name, roots, source_home)
    roots_identity = _roots_manifest_identity(roots_manifest)
    persistent_state = isinstance(state_root_raw, str) and bool(state_root_raw)
    if persistent_state:
        state_dir = Path(state_root_raw).expanduser().resolve() / machine_name / "projection"
    else:
        state_dir = staging_dir / ".projection-state"
    state_path = state_dir / "state.json"
    blob_root = state_dir / "blobs"
    previous_state = _load_projection_state(state_path, roots_identity) if persistent_state else None
    state_status = "incremental" if previous_state is not None else "rebuilt" if persistent_state else "disabled"
    current_files, projected_files, reused_files, state_deleted_files = _refresh_projection_files(
        projection_items,
        previous_state,
        blob_root,
    )
    current_inventory = _projection_inventory(current_files)

    mode = "projection_full"
    resolved_base_snapshot_id = None
    fallback_reason = None
    changed_files = []
    deleted_files = []
    payload_paths = sorted(current_files)

    if isinstance(requested_base_snapshot_id, str) and requested_base_snapshot_id:
        previous_bundle_dir = relay_root / "projection" / machine_name / requested_base_snapshot_id
        previous_roots_manifest_path = previous_bundle_dir / PROJECTION_ROOTS_MANIFEST_NAME
        previous_inventory_path = previous_bundle_dir / PROJECTION_INVENTORY_NAME
        if not previous_roots_manifest_path.is_file() or not previous_inventory_path.is_file():
            fallback_reason = "missing_base_snapshot"
        else:
            previous_roots_manifest = json.loads(previous_roots_manifest_path.read_text(encoding="utf-8"))
            if _roots_manifest_identity(previous_roots_manifest) != _roots_manifest_identity(roots_manifest):
                fallback_reason = "roots_manifest_changed"
            else:
                previous_inventory = json.loads(previous_inventory_path.read_text(encoding="utf-8"))
                changed_files, deleted_files = _diff_projection_inventory(previous_inventory, current_inventory)
                mode = "projection_delta"
                resolved_base_snapshot_id = requested_base_snapshot_id
                payload_paths = changed_files
    else:
        fallback_reason = "missing_local_state"

    _materialize_projection(current_files, blob_root, payload_root, payload_paths)
    roots_manifest_path, inventory_path = _write_projection_metadata(payload_root, roots_manifest, current_inventory)
    summary = _summary_for_paths(current_files, payload_paths)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    if shutil.which("zstd"):
        bundle_path = bundle_dir / "payload.tar.zst"
    else:
        bundle_path = bundle_dir / "payload.tar.gz"
    _pack_to_bundle_path(payload_root, bundle_path)
    bundle_roots_manifest_path = bundle_dir / PROJECTION_ROOTS_MANIFEST_NAME
    bundle_inventory_path = bundle_dir / PROJECTION_INVENTORY_NAME
    shutil.copy2(roots_manifest_path, bundle_roots_manifest_path)
    shutil.copy2(inventory_path, bundle_inventory_path)
    manifest_payload = {
        "version": 1,
        "mode": mode,
        "machine": machine_name,
        "import_name": import_name,
        "snapshot_id": snapshot_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "bundle": {
            "name": bundle_path.name,
            "bytes": bundle_path.stat().st_size,
            "sha256": _sha256_file(bundle_path),
        },
        "roots_manifest": {"name": bundle_roots_manifest_path.name},
        "inventory": {
            "name": bundle_inventory_path.name,
            "bytes": bundle_inventory_path.stat().st_size,
            "sha256": _sha256_file(bundle_inventory_path),
        },
        "summary": summary,
        "projection_state": {
            "status": state_status,
            "files_seen": len(projection_items),
            "files_projected": len(projected_files),
            "files_reused": reused_files,
            "files_deleted": len(state_deleted_files),
        },
    }
    if resolved_base_snapshot_id is not None:
        manifest_payload["base_snapshot_id"] = resolved_base_snapshot_id
    if fallback_reason is not None:
        manifest_payload["fallback_reason"] = fallback_reason
    if mode == "projection_delta":
        manifest_payload["changed_files"] = changed_files
        manifest_payload["deleted_files"] = deleted_files
    manifest_path = bundle_dir / "manifest.json"
    _write_json_file(manifest_path, manifest_payload)
    shutil.rmtree(staging_dir)
    if persistent_state:
        _write_projection_state(
            state_path,
            {
                "schema_version": PROJECTION_STATE_SCHEMA_VERSION,
                "projector_versions": {"codex": CODEX_PROJECTION_VERSION},
                "roots_identity": roots_identity,
                "current_snapshot_id": snapshot_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "files": current_files,
            },
        )
        _cleanup_projection_blobs(blob_root, current_files)
    print(
        json.dumps(
            {
                "machine_name": machine_name,
                "snapshot_id": snapshot_id,
                "bundle_dir": str(bundle_dir),
                "manifest_path": str(manifest_path),
                "bundle_path": str(bundle_path),
                "bundle_bytes": bundle_path.stat().st_size,
                "mode": mode,
                "base_snapshot_id": resolved_base_snapshot_id,
                "fallback_reason": fallback_reason,
                "state_status": state_status,
                "files_seen": len(projection_items),
                "files_projected": len(projected_files),
                "files_reused": reused_files,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def export_machine_projection_ssh(
    machine: MachineConfig,
    source_home: Path,
    relay_root: Path,
    ssh_target: str | None = None,
    command_prefix: list[str] | None = None,
    base_snapshot_id: str | None = None,
    ssh_options: list[str] | None = None,
    timeout_seconds: float | None = None,
) -> ProjectionBundle:
    if ssh_target is None:
        return export_machine_projection(
            machine=machine,
            source_home=source_home,
            relay_root=relay_root,
            base_snapshot_id=base_snapshot_id,
        )
    rules = [
        {
            "client": rule.client,
            "path": rule.path,
            "glob": rule.glob,
            "label": rule.label,
            "kind": rule.kind,
        }
        for rule in (*machine.roots, *machine.root_globs)
    ]
    encoded = base64.b64encode(
        json.dumps(
            {
                "machine_name": machine.name,
                "import_name": machine.import_name,
                "source_home": str(source_home),
                "relay_root": str(relay_root),
                "state_root": str(machine.remote_state_root) if machine.remote_state_root else None,
                "roots": rules,
                "base_snapshot_id": base_snapshot_id,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).decode("ascii")
    script = _remote_helper_source()
    base_command = command_prefix or ["python3", "-"]
    env = None
    if ssh_target:
        command = ["ssh", *(ssh_options or []), ssh_target, "env", f"ASV_REQUEST_B64={encoded}", *base_command]
    else:
        command = base_command
        env = dict(os.environ)
        env["ASV_REQUEST_B64"] = encoded
    run_kwargs: dict[str, object] = {
        "input": script,
        "text": True,
        "capture_output": True,
        "env": env,
    }
    if timeout_seconds is not None:
        run_kwargs["timeout"] = timeout_seconds
    completed = subprocess.run(command, **run_kwargs)
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        details = stderr or stdout or f"exit={completed.returncode}"
        raise RuntimeError(f"remote projection helper failed: {details}")
    payload = json.loads(completed.stdout.strip())
    return ProjectionBundle(
        machine_name=machine.name,
        snapshot_id=str(payload["snapshot_id"]),
        bundle_dir=Path(str(payload["bundle_dir"])),
        manifest_path=Path(str(payload["manifest_path"])),
        bundle_path=Path(str(payload["bundle_path"])),
        roots_manifest_path=Path(str(payload["bundle_dir"])) / "roots-manifest.json",
        inventory_path=Path(str(payload["bundle_dir"])) / PROJECTION_INVENTORY_NAME,
        bundle_bytes=int(payload.get("bundle_bytes", 0)),
        mode=str(payload.get("mode") or "projection_full"),
        base_snapshot_id=payload.get("base_snapshot_id") if isinstance(payload.get("base_snapshot_id"), str) else None,
        fallback_reason=payload.get("fallback_reason") if isinstance(payload.get("fallback_reason"), str) else None,
        state_status=payload.get("state_status") if isinstance(payload.get("state_status"), str) else None,
        files_seen=int(payload.get("files_seen", 0)),
        files_projected=int(payload.get("files_projected", 0)),
        files_reused=int(payload.get("files_reused", 0)),
    )


def fetch_projection_bundle_ssh(
    ssh_target: str,
    remote_bundle_dir: Path,
    local_bundle_dir: Path,
    ssh_options: list[str] | None = None,
    timeout_seconds: float | None = None,
    capture_output: bool = False,
) -> Path:
    local_bundle_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix="asv-projection-fetch-"))
    moved = False
    try:
        ssh_command = shlex.join(["ssh", "-C", "-o", "BatchMode=yes", *(ssh_options or [])])
        run_kwargs = {"check": True}
        if capture_output:
            run_kwargs.update({"capture_output": True, "text": True})
        if timeout_seconds is not None:
            run_kwargs["timeout"] = timeout_seconds
        subprocess.run(
            [
                "rsync",
                "-az",
                "-e",
                ssh_command,
                f"{ssh_target}:{remote_bundle_dir}/",
                f"{staging_dir}/",
            ],
            **run_kwargs,
        )
        if local_bundle_dir.exists():
            shutil.rmtree(local_bundle_dir)
        shutil.move(str(staging_dir), str(local_bundle_dir))
        moved = True
    finally:
        if not moved:
            shutil.rmtree(staging_dir, ignore_errors=True)
    return local_bundle_dir

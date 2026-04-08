from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timezone
import base64
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .archive import _pack_to_bundle_path, _sha256_file
from .config import MachineConfig, RootRuleConfig, VaultConfig


TERMINAL_EVENT_TYPES = {"task_complete", "turn_aborted"}


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
    bundle_bytes: int


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


def _build_codex_projection_file(source_path: Path, dest_path: Path) -> dict[str, int]:
    leading_session_meta_lines: list[str] = []
    turn_context_line: str | None = None
    token_event_lines: list[str] = []
    terminal_line: str | None = None
    leading_meta_open = True

    with source_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            obj = _json_load(line)
            if obj is None:
                leading_meta_open = False
                continue

            obj_type = obj.get("type")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

            if obj_type == "session_meta" and leading_meta_open:
                leading_session_meta_lines.append(line)
            elif obj_type != "session_meta":
                leading_meta_open = False

            if obj_type == "turn_context" and turn_context_line is None:
                turn_context_line = line

            if obj_type != "event_msg":
                continue

            event_type = payload.get("type")
            if event_type == "token_count":
                token_event_lines.append(line)
            elif event_type in TERMINAL_EVENT_TYPES:
                terminal_line = line

    lines: list[str] = []
    lines.extend(leading_session_meta_lines)
    if turn_context_line is not None:
        lines.append(turn_context_line)
    lines.extend(token_event_lines)
    if terminal_line is not None:
        lines.append(terminal_line)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with dest_path.open("w", encoding="utf-8") as handle:
        if lines:
            handle.write("\n".join(lines))
            handle.write("\n")
    return {
        "source_bytes": source_path.stat().st_size,
        "dest_bytes": dest_path.stat().st_size,
        "token_events": len(token_event_lines),
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
            item = _build_codex_projection_file(file_path, dest_path)
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


def export_machine_projection(
    machine: MachineConfig,
    source_home: Path,
    relay_root: Path,
) -> ProjectionBundle:
    source_home = source_home.expanduser().resolve()
    relay_root = relay_root.expanduser().resolve()
    snapshot_id = _build_snapshot_id(machine.name)
    bundle_dir = relay_root / "projection" / machine.name / snapshot_id
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    staging_dir = bundle_dir / ".staging"
    payload_root = staging_dir / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)

    discovered = discover_machine_roots(machine, source_home)
    roots_manifest = {
        "machine": machine.name,
        "import_name": machine.import_name,
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

    bundle_dir.mkdir(parents=True, exist_ok=True)
    roots_manifest_path = payload_root / "roots-manifest.json"
    roots_manifest_path.write_text(json.dumps(roots_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    bundle_path = bundle_dir / "payload.tar.zst"
    manifest_path = bundle_dir / "manifest.json"
    _pack_to_bundle_path(payload_root, bundle_path)
    bundle_roots_manifest_path = bundle_dir / "roots-manifest.json"
    shutil.copy2(roots_manifest_path, bundle_roots_manifest_path)

    manifest_payload = {
        "version": 1,
        "mode": "projection_full",
        "machine": machine.name,
        "import_name": machine.import_name,
        "snapshot_id": snapshot_id,
        "created_at": datetime.now(UTC).isoformat(),
        "bundle": {
            "name": bundle_path.name,
            "bytes": bundle_path.stat().st_size,
            "sha256": _sha256_file(bundle_path),
        },
        "summary": summary,
    }
    manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    shutil.rmtree(staging_dir)

    return ProjectionBundle(
        machine_name=machine.name,
        snapshot_id=snapshot_id,
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        bundle_path=bundle_path,
        roots_manifest_path=bundle_roots_manifest_path,
        bundle_bytes=bundle_path.stat().st_size,
    )


def _replace_tree(source_root: Path, dest_root: Path) -> None:
    if dest_root.exists():
        shutil.rmtree(dest_root)
    if source_root.is_dir():
        shutil.copytree(source_root, dest_root)


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

    for client in machine.clients:
        client_root = raw_root / client
        if client_root.exists():
            shutil.rmtree(client_root)

    for client in ("codex", "gemini", "openclaw"):
        if (extract_root / client).is_dir():
            _replace_tree(extract_root / client, raw_root / client)

    roots_manifest_path = extract_root / "roots-manifest.json"
    stored_roots_manifest = machine_root / ".projection-roots-manifest.json"
    if roots_manifest_path.is_file():
        machine_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(roots_manifest_path, stored_roots_manifest)

    state_path = machine_root / ".projection-state.json"
    state_path.write_text(
        json.dumps(
            {
                "current_snapshot_id": payload.get("snapshot_id"),
                "last_imported_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    shutil.rmtree(extract_root)

    if canonicalize_command:
        subprocess.run([canonicalize_command, "--machine-root", str(machine_root)], check=True)

    return ProjectionBundle(
        machine_name=machine_name,
        snapshot_id=str(payload.get("snapshot_id")),
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        bundle_path=bundle_path,
        roots_manifest_path=stored_roots_manifest,
        bundle_bytes=int(payload.get("bundle", {}).get("bytes", 0)),
    )


def pending_projection_bundle_dirs(config: VaultConfig, machine_name: str) -> list[Path]:
    bundle_root = config.paths.relay_root / "projection" / machine_name
    if not bundle_root.is_dir():
        return []
    machine = config.machines[machine_name]
    state_path = config.paths.import_root / machine.import_name / ".projection-state.json"
    current_snapshot_id = None
    if state_path.is_file():
        current_snapshot_id = json.loads(state_path.read_text(encoding="utf-8")).get("current_snapshot_id")

    candidates: list[tuple[str, Path]] = []
    for manifest_path in sorted(bundle_root.glob("*/manifest.json")):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        snapshot_id = payload.get("snapshot_id")
        if not isinstance(snapshot_id, str):
            continue
        if isinstance(current_snapshot_id, str) and snapshot_id <= current_snapshot_id:
            continue
        candidates.append((snapshot_id, manifest_path.parent))
    if not candidates:
        return []
    candidates.sort(key=lambda item: item[0])
    return [candidates[-1][1]]


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
import sys
import tarfile

TERMINAL_EVENT_TYPES = {"task_complete", "turn_aborted"}


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
        if "path" in rule:
            candidate_paths = [_expand_user_like(rule["path"], source_home)]
        else:
            candidate_paths = [Path(match) for match in sorted(glob.glob(str(_expand_user_like(rule["glob"], source_home)), recursive=True))]
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


def _copy_tree(source_root, dest_root):
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


def _copy_relative_file(source_root, file_path, dest_root):
    rel = file_path.relative_to(source_root)
    dest_path = dest_root / rel
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file_path, dest_path)
    return dest_path.stat().st_size


def _build_codex_projection_file(source_path, dest_path):
    leading_session_meta_lines = []
    turn_context_line = None
    token_event_lines = []
    terminal_line = None
    leading_meta_open = True
    with source_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\\n")
            obj = _json_load(line)
            if obj is None:
                leading_meta_open = False
                continue
            obj_type = obj.get("type")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if obj_type == "session_meta" and leading_meta_open:
                leading_session_meta_lines.append(line)
            elif obj_type != "session_meta":
                leading_meta_open = False
            if obj_type == "turn_context" and turn_context_line is None:
                turn_context_line = line
            if obj_type != "event_msg":
                continue
            event_type = payload.get("type")
            if event_type == "token_count":
                token_event_lines.append(line)
            elif event_type in TERMINAL_EVENT_TYPES:
                terminal_line = line
    lines = []
    lines.extend(leading_session_meta_lines)
    if turn_context_line is not None:
        lines.append(turn_context_line)
    lines.extend(token_event_lines)
    if terminal_line is not None:
        lines.append(terminal_line)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with dest_path.open("w", encoding="utf-8") as handle:
        if lines:
            handle.write("\\n".join(lines))
            handle.write("\\n")
    return {
        "source_bytes": source_path.stat().st_size,
        "dest_bytes": dest_path.stat().st_size,
        "token_events": len(token_event_lines),
    }


def _project_codex_root(root, payload_root):
    stats = {"files_written": 0, "source_bytes_total": 0, "dest_bytes_total": 0, "token_events_total": 0}
    source_path = root["source_path"]
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
            rel = file_path.relative_to(source_root)
            dest_path = payload_root / "codex" / bucket / root["root_id"] / rel
            item = _build_codex_projection_file(file_path, dest_path)
            for key, value in item.items():
                if key == "token_events":
                    stats["token_events_total"] += value
                elif key == "source_bytes":
                    stats["source_bytes_total"] += value
                elif key == "dest_bytes":
                    stats["dest_bytes_total"] += value
            stats["files_written"] += 1
    return stats


def _project_gemini_root(root, payload_root):
    source_root = root["source_path"] / "tmp" if (root["source_path"] / "tmp").is_dir() else root["source_path"]
    dest_root = payload_root / "gemini" / root["root_id"]
    stats = {"files_written": 0, "source_bytes_total": 0, "dest_bytes_total": 0}
    for file_path in sorted(source_root.rglob("*.json")):
        if file_path.parent.name != "chats":
            continue
        stats["files_written"] += 1
        stats["source_bytes_total"] += file_path.stat().st_size
        stats["dest_bytes_total"] += _copy_relative_file(source_root, file_path, dest_root)
    return stats


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


def _project_openclaw_root(root, payload_root):
    source_root = root["source_path"] / "agents" if (root["source_path"] / "agents").is_dir() else root["source_path"]
    dest_root = payload_root / "openclaw" / root["root_id"]
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


def main():
    encoded = os.environ.get("ASV_REQUEST_B64")
    if not encoded:
        raise RuntimeError("missing ASV_REQUEST_B64")
    request = json.loads(base64.b64decode(encoded.encode("ascii")).decode("utf-8"))
    machine_name = request["machine_name"]
    source_home = Path(request["source_home"]).expanduser().resolve()
    relay_root = Path(request["relay_root"]).expanduser().resolve()
    roots = request["roots"]
    snapshot_id = f"{machine_name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    bundle_dir = relay_root / "projection" / machine_name / snapshot_id
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    staging_dir = bundle_dir / ".staging"
    payload_root = staging_dir / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)
    discovered = discover_machine_roots(roots, source_home)
    roots_manifest = {
        "machine": machine_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": [],
    }
    summary = {"files_written": 0, "source_bytes_total": 0, "dest_bytes_total": 0, "token_events_total": 0}
    for root in discovered:
        if root["client"] == "codex":
            item = _project_codex_root(root, payload_root)
        elif root["client"] == "gemini":
            item = _project_gemini_root(root, payload_root)
        elif root["client"] == "openclaw":
            item = _project_openclaw_root(root, payload_root)
        else:
            continue
        roots_manifest["roots"].append(
            {
                "root_id": root["root_id"],
                "client": root["client"],
                "source_path": str(root["source_path"]),
                "label": root.get("label"),
                "kind": root.get("kind"),
            }
        )
        for key, value in item.items():
            if key in summary:
                summary[key] += value
    bundle_dir.mkdir(parents=True, exist_ok=True)
    roots_manifest_path = payload_root / "roots-manifest.json"
    roots_manifest_path.write_text(json.dumps(roots_manifest, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
    if shutil.which("zstd"):
        bundle_path = bundle_dir / "payload.tar.zst"
    else:
        bundle_path = bundle_dir / "payload.tar.gz"
    _pack_to_bundle_path(payload_root, bundle_path)
    shutil.copy2(roots_manifest_path, bundle_dir / "roots-manifest.json")
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "projection_full",
                "machine": machine_name,
                "snapshot_id": snapshot_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "bundle": {
                    "name": bundle_path.name,
                    "bytes": bundle_path.stat().st_size,
                    "sha256": _sha256_file(bundle_path),
                },
                "summary": summary,
            },
            indent=2,
            sort_keys=True,
        )
        + "\\n",
        encoding="utf-8",
    )
    shutil.rmtree(staging_dir)
    print(
        json.dumps(
            {
                "machine_name": machine_name,
                "snapshot_id": snapshot_id,
                "bundle_dir": str(bundle_dir),
                "manifest_path": str(manifest_path),
                "bundle_path": str(bundle_path),
                "bundle_bytes": bundle_path.stat().st_size,
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
) -> ProjectionBundle:
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
                "source_home": str(source_home),
                "relay_root": str(relay_root),
                "roots": rules,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).decode("ascii")
    script = _remote_helper_source()
    base_command = command_prefix or ["python3", "-"]
    env = None
    if ssh_target:
        command = ["ssh", ssh_target, "env", f"ASV_REQUEST_B64={encoded}", *base_command]
    else:
        command = base_command
        env = dict(os.environ)
        env["ASV_REQUEST_B64"] = encoded
    completed = subprocess.run(command, input=script, text=True, capture_output=True, env=env)
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
        bundle_bytes=int(payload.get("bundle_bytes", 0)),
    )


def fetch_projection_bundle_ssh(
    ssh_target: str,
    remote_bundle_dir: Path,
    local_bundle_dir: Path,
) -> Path:
    local_bundle_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "rsync",
            "-az",
            "-e",
            "ssh -C -o BatchMode=yes",
            f"{ssh_target}:{remote_bundle_dir}/",
            f"{local_bundle_dir}/",
        ],
        check=True,
    )
    return local_bundle_dir

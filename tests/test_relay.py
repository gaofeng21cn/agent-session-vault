import hashlib
from pathlib import Path
import json
import tarfile

from agent_session_vault.config import load_config
from agent_session_vault.relay import export_machine_delta, import_machine_delta


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_relay_export_and_import_round_trip_with_incremental_state(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    workspace = tmp_path / "workspace"
    imports = target_home / ".config" / "tokscale" / "imports"
    shadow_home = target_home / ".config" / "tokscale" / "shadow-home"
    extras = target_home / ".config" / "tokscale" / "local-workspace-extras"
    archive = tmp_path / "archive"
    relay_root = archive / "relay"

    _write(source_home / ".codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl", "a")
    _write(source_home / ".gemini" / "tmp" / "proj" / "chats" / "chat.json", "g")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{target_home}"
workspace_root = "{workspace}"
import_root = "{imports}"
shadow_home = "{shadow_home}"
local_workspace_extras = "{extras}"
archive_root = "{archive}"

[machines.imac]
import_name = "imac"
ssh_target = "imac-sync"
clients = ["codex", "gemini", "openclaw"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    first = export_machine_delta(
        machine_name="imac",
        source_home=source_home,
        relay_root=relay_root,
        state_root=source_home / ".config" / "agent-session-vault" / "relay-state",
    )
    import_machine_delta(config=config, machine_name="imac", bundle_dir=first.bundle_dir)

    assert (imports / "imac" / ".raw" / "codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl").read_text(encoding="utf-8") == "a"
    assert (imports / "imac" / ".raw" / "gemini" / "proj" / "chats" / "chat.json").read_text(encoding="utf-8") == "g"

    _write(source_home / ".codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl", "aa")
    _write(source_home / ".openclaw" / "agents" / "main" / "sessions" / "session.jsonl", "o")

    second = export_machine_delta(
        machine_name="imac",
        source_home=source_home,
        relay_root=relay_root,
        state_root=source_home / ".config" / "agent-session-vault" / "relay-state",
    )
    import_machine_delta(config=config, machine_name="imac", bundle_dir=second.bundle_dir)

    assert (imports / "imac" / ".raw" / "codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl").read_text(encoding="utf-8") == "aa"
    assert (imports / "imac" / ".raw" / "openclaw" / "agents" / "main" / "sessions" / "session.jsonl").read_text(encoding="utf-8") == "o"


def test_relay_import_rejects_snapshot_gap(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    workspace = tmp_path / "workspace"
    imports = target_home / ".config" / "tokscale" / "imports"
    shadow_home = target_home / ".config" / "tokscale" / "shadow-home"
    extras = target_home / ".config" / "tokscale" / "local-workspace-extras"
    archive = tmp_path / "archive"
    relay_root = archive / "relay"

    _write(source_home / ".codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl", "a")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{target_home}"
workspace_root = "{workspace}"
import_root = "{imports}"
shadow_home = "{shadow_home}"
local_workspace_extras = "{extras}"
archive_root = "{archive}"

[machines.imac]
import_name = "imac"
ssh_target = "imac-sync"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    first = export_machine_delta(
        machine_name="imac",
        source_home=source_home,
        relay_root=relay_root,
        state_root=source_home / ".config" / "agent-session-vault" / "relay-state",
    )
    _write(source_home / ".codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl", "aa")
    second = export_machine_delta(
        machine_name="imac",
        source_home=source_home,
        relay_root=relay_root,
        state_root=source_home / ".config" / "agent-session-vault" / "relay-state",
    )

    try:
        import_machine_delta(config=config, machine_name="imac", bundle_dir=second.bundle_dir)
    except ValueError as exc:
        assert "previous_snapshot_id" in str(exc)
    else:
        raise AssertionError("expected snapshot gap import to fail")


def test_relay_import_reads_bundle_name_from_manifest(tmp_path: Path) -> None:
    target_home = tmp_path / "target-home"
    workspace = tmp_path / "workspace"
    imports = target_home / ".config" / "tokscale" / "imports"
    shadow_home = target_home / ".config" / "tokscale" / "shadow-home"
    extras = target_home / ".config" / "tokscale" / "local-workspace-extras"
    archive = tmp_path / "archive"
    bundle_dir = tmp_path / "relay" / "imac" / "imac-000001"
    payload_root = tmp_path / "payload-root"
    payload_file = payload_root / ".raw" / "codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl"
    _write(payload_file, "a")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[paths]
home = "{target_home}"
workspace_root = "{workspace}"
import_root = "{imports}"
shadow_home = "{shadow_home}"
local_workspace_extras = "{extras}"
archive_root = "{archive}"

[machines.imac]
import_name = "imac"
ssh_target = "imac-sync"
clients = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)

    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundle_dir / "payload.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as tar:
        tar.add(payload_root, arcname=".")

    manifest = {
        "version": 1,
        "mode": "delta",
        "machine": "imac",
        "snapshot_id": "imac-000001",
        "previous_snapshot_id": None,
        "bundle": {
            "name": bundle_path.name,
            "bytes": bundle_path.stat().st_size,
            "sha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
        },
        "files": {
            ".raw/codex/sessions/2026/04/07/one.jsonl": "dummy",
        },
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    import_machine_delta(config=config, machine_name="imac", bundle_dir=bundle_dir)

    assert (imports / "imac" / ".raw" / "codex" / "sessions" / "2026" / "04" / "07" / "one.jsonl").read_text(encoding="utf-8") == "a"

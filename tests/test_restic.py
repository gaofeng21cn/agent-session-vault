from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from agent_session_vault import restic as restic_mod
from agent_session_vault.restic import (
    ResticError,
    ResticRepository,
    ResticUnavailableError,
    ensure_password_file,
    escape_restic_pattern,
    resolve_restic_binary,
)


def test_ensure_password_file_creates_0600_and_is_idempotent(tmp_path: Path) -> None:
    pw = tmp_path / "nested" / "restic-password"
    created = ensure_password_file(pw)
    assert created == pw
    mode = stat.S_IMODE(pw.stat().st_mode)
    assert mode == 0o600
    first_secret = pw.read_text(encoding="utf-8")
    assert len(first_secret.strip()) >= 32
    assert ensure_password_file(pw) == pw
    assert pw.read_text(encoding="utf-8") == first_secret


def test_escape_restic_pattern_escapes_glob_metacharacters() -> None:
    assert escape_restic_pattern("/sessions/a[1]*?\\b.jsonl") == r"/sessions/a\[1\]\*\?\\b.jsonl"


def test_resolve_restic_binary_raises_when_missing(monkeypatch) -> None:
    monkeypatch.delenv(restic_mod.RESTIC_BINARY_ENV, raising=False)
    monkeypatch.setattr(restic_mod.shutil, "which", lambda _: None)
    with pytest.raises(ResticUnavailableError, match="restic binary is unavailable"):
        resolve_restic_binary()


def test_restic_repository_subprocess_contracts_without_binary(tmp_path: Path, monkeypatch) -> None:
    fake_bin = tmp_path / "fake-restic"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    pw = tmp_path / "pw"
    repo_dir = tmp_path / "repo"
    recorded: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd, *, cwd=None, env=None, capture_output=True, text=True, timeout=3600, check=False):
        recorded.append((list(cmd), dict(env or {})))
        sub = cmd[1]
        if sub == "init":
            (repo_dir).mkdir(parents=True, exist_ok=True)
            (repo_dir / "config").write_text("{}", encoding="utf-8")
            stdout = json.dumps({"message_type": "initialized", "id": "abc123", "repository": str(repo_dir)})
        elif sub == "cat":
            stdout = "repository abc123 opened\n" + json.dumps({"version": 2, "id": "abc123"})
        elif sub == "backup":
            stdout = json.dumps({"message_type": "status"}) + "\n" + json.dumps({
                "message_type": "summary",
                "snapshot_id": "snap-restic-1",
                "files_new": 1,
                "data_blobs": 1,
                "data_added": 40,
                "data_added_packed": 32,
                "total_files_processed": 1,
                "total_bytes_processed": 12,
            })
        elif sub == "snapshots":
            stdout = json.dumps([{"id": "snap-restic-1", "short_id": "snap-res", "time": "2026-11-24T00:00:00Z", "hostname": "m1", "tags": ["vault:archive"], "paths": ["/a"]}])
        elif sub == "ls":
            stdout = json.dumps({"message_type": "node", "type": "file", "path": "/a.jsonl", "size": 12})
        elif sub == "restore":
            stdout = json.dumps({"message_type": "summary", "total_files": 1, "files_restored": 1})
        elif sub == "check":
            stdout = json.dumps({"message_type": "summary", "num_errors": 0, "broken_packs": None})
        elif sub == "stats":
            stdout = json.dumps({"total_size": 32, "total_uncompressed_size": 40, "total_blob_count": 1, "snapshots_count": 1})
        elif sub == "prune":
            stdout = json.dumps({"message_type": "summary", "blobs": {"removed": 0}})
        elif sub == "forget":
            stdout = json.dumps({"keep": [{"id": "snap-restic-1"}]})
        else:
            return subprocess.CompletedProcess(cmd, 1, "", "unexpected")
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.delenv("RESTIC_PASSWORD", raising=False)
    monkeypatch.delenv("RESTIC_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("RESTIC_PASSWORD_COMMAND", raising=False)
    monkeypatch.setattr(restic_mod.subprocess, "run", fake_run)

    repo = ResticRepository(repo_dir, password_file=pw, cache_dir=tmp_path / "cache", binary=str(fake_bin))
    init_res = repo.init()
    assert init_res["id"] == "abc123"
    assert pw.is_file()
    assert repo.cat_config()["id"] == "abc123"

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.jsonl").write_text("data", encoding="utf-8")
    summary = repo.backup(src, ["a.jsonl"], hostname="m1", tags=("vault:archive",))
    assert summary.snapshot_id == "snap-restic-1"
    assert summary.data_added_packed == 32
    assert repo.snapshots(tags=("vault:archive",))[0].id == "snap-restic-1"
    assert repo.ls("snap-restic-1")[0].path == "/a.jsonl"
    assert repo.restore("snap-restic-1", tmp_path / "out", includes=("a[1].jsonl",))["files_restored"] == 1
    assert any(r"/a\[1\].jsonl" in cmd for cmd, _ in recorded)
    assert repo.check(read_data=True).ok is True
    assert repo.stats().total_size == 32
    assert "blobs" in repo.prune()
    assert repo.forget(keep_last=1, prune=True)
    assert all(env["RESTIC_REPOSITORY"] == str(repo_dir) for _, env in recorded)
    assert all(env["RESTIC_PASSWORD_FILE"] == str(pw) for _, env in recorded)


def test_restic_error_propagates_nonzero_exit(tmp_path: Path, monkeypatch) -> None:
    fake_bin = tmp_path / "fake-restic"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    pw = ensure_password_file(tmp_path / "pw")
    monkeypatch.setenv("RESTIC_PASSWORD", "inline")
    monkeypatch.setattr(
        restic_mod.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, "", "wrong password"),
    )
    repo = ResticRepository(tmp_path / "repo", password_file=pw, binary=str(fake_bin))
    with pytest.raises(ResticError, match="wrong password") as exc_info:
        repo.snapshots()
    assert exc_info.value.returncode == 1
    assert os.environ.get("RESTIC_PASSWORD") == "inline"

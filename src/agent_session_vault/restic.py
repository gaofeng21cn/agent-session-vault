"""Zero-dependency Python wrapper around the ``restic`` CLI."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
from typing import Any


RESTIC_BINARY_ENV = "AGENT_SESSION_VAULT_RESTIC_BIN"
_PASSWORD_ENV_VARS = ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND")
_GLOB_SPECIAL_RE = re.compile(r"([\\*?\[\]])")


class ResticError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        command: list[str] | None = None,
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.command = command or []
        self.returncode = returncode
        self.stderr = stderr


class ResticUnavailableError(ResticError):
    pass


@dataclass(frozen=True)
class ResticBackupSummary:
    snapshot_id: str
    files_new: int = 0
    files_changed: int = 0
    files_unmodified: int = 0
    dirs_new: int = 0
    dirs_changed: int = 0
    dirs_unmodified: int = 0
    data_blobs: int = 0
    tree_blobs: int = 0
    data_added: int = 0
    data_added_packed: int = 0
    total_files_processed: int = 0
    total_bytes_processed: int = 0

    def to_payload(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "files_new": self.files_new,
            "files_changed": self.files_changed,
            "files_unmodified": self.files_unmodified,
            "dirs_new": self.dirs_new,
            "dirs_changed": self.dirs_changed,
            "dirs_unmodified": self.dirs_unmodified,
            "data_blobs": self.data_blobs,
            "tree_blobs": self.tree_blobs,
            "data_added": self.data_added,
            "data_added_packed": self.data_added_packed,
            "total_files_processed": self.total_files_processed,
            "total_bytes_processed": self.total_bytes_processed,
        }


@dataclass(frozen=True)
class ResticSnapshotInfo:
    id: str
    short_id: str
    time: str
    hostname: str
    tags: tuple[str, ...]
    paths: tuple[str, ...]
    parent: str | None = None
    tree: str | None = None
    summary: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResticNode:
    path: str
    type: str
    size: int = 0
    mtime: str | None = None


@dataclass(frozen=True)
class ResticCheckResult:
    ok: bool
    num_errors: int
    broken_packs: tuple[str, ...]
    suggest_repair_index: bool
    suggest_prune: bool
    raw: dict[str, Any]

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "num_errors": self.num_errors,
            "broken_packs": list(self.broken_packs),
            "suggest_repair_index": self.suggest_repair_index,
            "suggest_prune": self.suggest_prune,
        }


@dataclass(frozen=True)
class ResticStats:
    total_size: int
    total_uncompressed_size: int | None
    compression_ratio: float | None
    compression_space_saving: float | None
    total_blob_count: int | None
    snapshots_count: int
    total_file_count: int | None
    raw: dict[str, Any]

    def to_payload(self) -> dict[str, object]:
        return {
            "total_size": self.total_size,
            "total_uncompressed_size": self.total_uncompressed_size,
            "compression_ratio": self.compression_ratio,
            "compression_space_saving": self.compression_space_saving,
            "total_blob_count": self.total_blob_count,
            "snapshots_count": self.snapshots_count,
            "total_file_count": self.total_file_count,
        }


def default_password_file(home: Path) -> Path:
    return home.expanduser() / ".config" / "agent-session-vault" / "restic-password"


def has_password_env(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return any(bool(source.get(name)) for name in _PASSWORD_ENV_VARS)


def ensure_password_file(path: Path) -> Path:
    target = path.expanduser()
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32) + "\n"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return target
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
        handle.flush()
        os.fsync(handle.fileno())
    return target


def resolve_restic_binary(binary: str | None = None) -> str:
    candidate = binary or os.environ.get(RESTIC_BINARY_ENV) or shutil.which("restic")
    if not candidate:
        raise ResticUnavailableError(
            "restic binary is unavailable; install restic or set AGENT_SESSION_VAULT_RESTIC_BIN"
        )
    if os.sep in candidate and not Path(candidate).is_file():
        raise ResticUnavailableError(f"configured restic binary does not exist: {candidate}")
    return candidate


def restic_available(binary: str | None = None) -> bool:
    try:
        resolve_restic_binary(binary)
    except ResticUnavailableError:
        return False
    return True


def escape_restic_pattern(path: str) -> str:
    return _GLOB_SPECIAL_RE.sub(r"\\\1", path)


def _parse_json_lines(stdout: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _parse_json_object(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        value = json.loads(text[start : end + 1])
        if isinstance(value, dict):
            return value
    raise ResticError(f"restic did not return a valid JSON object: {text[:200]!r}")


class ResticRepository:
    """Thin subprocess wrapper around a single local or mounted restic repository."""

    def __init__(
        self,
        repo_path: Path,
        *,
        password_file: Path | None = None,
        cache_dir: Path | None = None,
        binary: str | None = None,
    ) -> None:
        self.repo_path = repo_path.expanduser()
        self.password_file = password_file.expanduser() if password_file is not None else None
        self.cache_dir = cache_dir.expanduser() if cache_dir is not None else None
        self.binary = binary

    def exists(self) -> bool:
        return (self.repo_path / "config").is_file()

    def _env(self, *, create_password: bool = False) -> dict[str, str]:
        env = dict(os.environ)
        env["RESTIC_REPOSITORY"] = str(self.repo_path)
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            env["RESTIC_CACHE_DIR"] = str(self.cache_dir)
        if not has_password_env(env):
            if self.password_file is None:
                raise ResticError(
                    "restic credentials are not configured; set RESTIC_PASSWORD_FILE or archive.password_file"
                )
            if create_password:
                ensure_password_file(self.password_file)
            elif not self.password_file.is_file():
                raise ResticError(f"restic password file is missing: {self.password_file}")
            env["RESTIC_PASSWORD_FILE"] = str(self.password_file)
        return env

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        create_password: bool = False,
        timeout: float = 3600,
    ) -> subprocess.CompletedProcess[str]:
        executable = resolve_restic_binary(self.binary)
        command = [executable, *args]
        env = self._env(create_password=create_password)
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ResticError(
                f"restic {' '.join(args[:2])} failed (exit={completed.returncode}): {detail}",
                command=command,
                returncode=completed.returncode,
                stderr=detail,
            )
        return completed

    def init(self) -> dict[str, object]:
        if self.exists():
            config_payload = self.cat_config()
            return {
                "status": "existing",
                "repository": str(self.repo_path),
                "id": str(config_payload.get("id", "")),
            }
        self.repo_path.parent.mkdir(parents=True, exist_ok=True)
        completed = self._run(["init", "--json"], create_password=True)
        payload = _parse_json_object(completed.stdout)
        return {
            "status": "initialized",
            "repository": str(self.repo_path),
            "id": str(payload.get("id", "")),
        }

    def cat_config(self) -> dict[str, Any]:
        completed = self._run(["cat", "config", "--quiet"])
        return _parse_json_object(completed.stdout)

    def backup(
        self,
        cwd: Path,
        relative_paths: list[str],
        *,
        hostname: str,
        tags: tuple[str, ...] = (),
        parent: str | None = None,
    ) -> ResticBackupSummary:
        if not relative_paths:
            raise ValueError("restic backup requires at least one relative path")
        list_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="asv-restic-files-", suffix=".txt", mode="w", encoding="utf-8", delete=False
            ) as handle:
                list_path = Path(handle.name)
                for item in relative_paths:
                    if "\n" in item or "\r" in item or "\x00" in item:
                        raise ValueError(f"unsupported control character in archive path: {item!r}")
                    handle.write(item + "\n")
            args = ["backup", "--json", "--host", hostname]
            for tag in tags:
                args.extend(["--tag", tag])
            if parent:
                args.extend(["--parent", parent])
            args.extend(["--files-from-verbatim", str(list_path)])
            completed = self._run(args, cwd=cwd)
        finally:
            if list_path is not None:
                list_path.unlink(missing_ok=True)

        summary = next(
            (item for item in reversed(_parse_json_lines(completed.stdout)) if item.get("message_type") == "summary"),
            None,
        )
        if summary is None or not isinstance(summary.get("snapshot_id"), str) or not summary["snapshot_id"]:
            raise ResticError(f"restic backup did not return a snapshot summary: {completed.stdout[:300]!r}")
        return ResticBackupSummary(
            snapshot_id=str(summary["snapshot_id"]),
            files_new=int(summary.get("files_new", 0)),
            files_changed=int(summary.get("files_changed", 0)),
            files_unmodified=int(summary.get("files_unmodified", 0)),
            dirs_new=int(summary.get("dirs_new", 0)),
            dirs_changed=int(summary.get("dirs_changed", 0)),
            dirs_unmodified=int(summary.get("dirs_unmodified", 0)),
            data_blobs=int(summary.get("data_blobs", 0)),
            tree_blobs=int(summary.get("tree_blobs", 0)),
            data_added=int(summary.get("data_added", 0)),
            data_added_packed=int(summary.get("data_added_packed", 0)),
            total_files_processed=int(summary.get("total_files_processed", 0)),
            total_bytes_processed=int(summary.get("total_bytes_processed", 0)),
        )

    def snapshots(
        self,
        *,
        tags: tuple[str, ...] = (),
        snapshot_ids: tuple[str, ...] = (),
    ) -> list[ResticSnapshotInfo]:
        args = ["snapshots", "--json"]
        for tag in tags:
            args.extend(["--tag", tag])
        args.extend(snapshot_ids)
        completed = self._run(args)
        raw = json.loads(completed.stdout or "[]")
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ResticError("restic snapshots returned a non-list JSON payload")
        items: list[ResticSnapshotInfo] = []
        for entry in raw:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            items.append(
                ResticSnapshotInfo(
                    id=str(entry["id"]),
                    short_id=str(entry.get("short_id", entry["id"][:8])),
                    time=str(entry.get("time", "")),
                    hostname=str(entry.get("hostname", "")),
                    tags=tuple(str(tag) for tag in entry.get("tags") or ()),
                    paths=tuple(str(path) for path in entry.get("paths") or ()),
                    parent=str(entry["parent"]) if isinstance(entry.get("parent"), str) else None,
                    tree=str(entry["tree"]) if isinstance(entry.get("tree"), str) else None,
                    summary=entry.get("summary") if isinstance(entry.get("summary"), dict) else None,
                )
            )
        return items

    def ls(self, snapshot_id: str) -> list[ResticNode]:
        completed = self._run(["ls", "--json", snapshot_id])
        nodes: list[ResticNode] = []
        for item in _parse_json_lines(completed.stdout):
            if item.get("message_type") != "node" and item.get("struct_type") != "node":
                continue
            path = item.get("path")
            node_type = item.get("type")
            if not isinstance(path, str) or not isinstance(node_type, str):
                continue
            nodes.append(
                ResticNode(
                    path=path,
                    type=node_type,
                    size=int(item.get("size", 0) or 0),
                    mtime=str(item["mtime"]) if isinstance(item.get("mtime"), str) else None,
                )
            )
        return nodes

    def restore(
        self,
        snapshot_id: str,
        target: Path,
        *,
        includes: tuple[str, ...] = (),
    ) -> dict[str, object]:
        target_dir = target.expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
        args = ["restore", snapshot_id, "--target", str(target_dir), "--json"]
        for item in includes:
            normalized = "/" + item.lstrip("/")
            args.extend(["--include", escape_restic_pattern(normalized)])
        completed = self._run(args)
        summary = next(
            (item for item in reversed(_parse_json_lines(completed.stdout)) if item.get("message_type") == "summary"),
            {},
        )
        return dict(summary)

    def check(self, *, read_data: bool = False) -> ResticCheckResult:
        args = ["check", "--json"]
        if read_data:
            args.append("--read-data")
        executable = resolve_restic_binary(self.binary)
        command = [executable, *args]
        completed = subprocess.run(
            command,
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
        records = _parse_json_lines(completed.stdout)
        summary = next((item for item in reversed(records) if item.get("message_type") == "summary"), {})
        num_errors = int(summary.get("num_errors", 0 if completed.returncode == 0 else 1))
        broken = tuple(str(pack) for pack in (summary.get("broken_packs") or ()))
        ok = completed.returncode == 0 and num_errors == 0 and not broken
        return ResticCheckResult(
            ok=ok,
            num_errors=num_errors,
            broken_packs=broken,
            suggest_repair_index=bool(summary.get("suggest_repair_index", False)),
            suggest_prune=bool(summary.get("suggest_prune", False)),
            raw=summary or {"returncode": completed.returncode, "stderr": (completed.stderr or "").strip()},
        )

    def prune(self, *, dry_run: bool = False, repack_small: bool = False) -> dict[str, Any]:
        args = ["prune", "--json"]
        if dry_run:
            args.append("--dry-run")
        if repack_small:
            args.append("--repack-small")
        completed = self._run(args)
        summary = next(
            (item for item in reversed(_parse_json_lines(completed.stdout)) if item.get("message_type") == "summary"),
            {},
        )
        return dict(summary)

    def forget(
        self,
        *,
        keep_last: int | None = None,
        keep_daily: int | None = None,
        keep_weekly: int | None = None,
        keep_monthly: int | None = None,
        prune: bool = True,
        dry_run: bool = False,
        tags: tuple[str, ...] = (),
        group_by: str | None = "host",
        repack_small: bool = False,
    ) -> dict[str, Any]:
        args = ["forget", "--json"]
        if group_by is not None:
            args.extend(["--group-by", group_by])
        if keep_last is not None:
            args.extend(["--keep-last", str(keep_last)])
        if keep_daily is not None:
            args.extend(["--keep-daily", str(keep_daily)])
        if keep_weekly is not None:
            args.extend(["--keep-weekly", str(keep_weekly)])
        if keep_monthly is not None:
            args.extend(["--keep-monthly", str(keep_monthly)])
        for tag in tags:
            args.extend(["--tag", tag])
        if prune:
            args.append("--prune")
            if repack_small:
                args.append("--repack-small")
        if dry_run:
            args.append("--dry-run")
        completed = self._run(args)
        records = _parse_json_lines(completed.stdout)
        if records:
            return {"records": records}
        try:
            parsed = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError:
            parsed = []
        return {"groups": parsed}

    def stats(self, *, mode: str = "raw-data") -> ResticStats:
        completed = self._run(["stats", "--mode", mode, "--json"])
        payload = _parse_json_object(completed.stdout)
        return ResticStats(
            total_size=int(payload.get("total_size", 0)),
            total_uncompressed_size=(
                int(payload["total_uncompressed_size"]) if payload.get("total_uncompressed_size") is not None else None
            ),
            compression_ratio=(
                float(payload["compression_ratio"]) if payload.get("compression_ratio") is not None else None
            ),
            compression_space_saving=(
                float(payload["compression_space_saving"])
                if payload.get("compression_space_saving") is not None
                else None
            ),
            total_blob_count=(
                int(payload["total_blob_count"]) if payload.get("total_blob_count") is not None else None
            ),
            snapshots_count=int(payload.get("snapshots_count", 0)),
            total_file_count=(
                int(payload["total_file_count"]) if payload.get("total_file_count") is not None else None
            ),
            raw=payload,
        )

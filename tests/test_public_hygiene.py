from __future__ import annotations

import ipaddress
from pathlib import Path
import re
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
MAC_HOME_PATTERN = re.compile(r"(?<![A-Za-z0-9._-])/Users/([A-Za-z0-9._-]+)")
LINUX_HOME_PATTERN = re.compile(r"(?<![A-Za-z0-9._-])/home/([A-Za-z0-9._-]+)")
IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ALLOWED_HOME_NAMES = {"your-user"}
TAILSCALE_NETWORK = ipaddress.ip_network((int.from_bytes(bytes((100, 64, 0, 0))), 10))


def _public_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [REPO_ROOT / item.decode() for item in completed.stdout.split(b"\0") if item]


def _public_text() -> list[tuple[Path, str]]:
    payloads: list[tuple[Path, str]] = []
    for path in _public_files():
        try:
            payloads.append((path, path.read_text(encoding="utf-8")))
        except UnicodeDecodeError:
            continue
    return payloads


def test_public_tree_uses_placeholder_home_paths() -> None:
    offenders: list[str] = []
    for path, text in _public_text():
        for pattern in (MAC_HOME_PATTERN, LINUX_HOME_PATTERN):
            for match in pattern.finditer(text):
                if match.group(1) not in ALLOWED_HOME_NAMES:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")

    assert not offenders, "real home paths are tracked:\n" + "\n".join(offenders)


def test_public_tree_has_no_tailscale_addresses() -> None:
    offenders: list[str] = []
    for path, text in _public_text():
        for match in IPV4_PATTERN.finditer(text):
            try:
                address = ipaddress.ip_address(match.group(0))
            except ValueError:
                continue
            if address in TAILSCALE_NETWORK:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {address}")

    assert not offenders, "Tailscale addresses are tracked:\n" + "\n".join(offenders)


def test_sensitive_local_artifacts_are_ignored() -> None:
    candidates = (
        ".env",
        ".env.local",
        "config.toml",
        "config/agent-session-vault.local.toml",
        "ops/daily-tokscale/current.json",
        "runs/example/receipt.json",
        "sessions/example.jsonl",
        "archived_sessions/example.jsonl",
        "relay/bundle.tar.zst",
        "archive/bundle.tar.zst",
        "stable/imports/example.jsonl",
        "private-key.pem",
    )
    missing: list[str] = []
    for candidate in candidates:
        completed = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", candidate],
            cwd=REPO_ROOT,
        )
        if completed.returncode != 0:
            missing.append(candidate)

    assert not missing, "sensitive local artifacts are not ignored:\n" + "\n".join(missing)

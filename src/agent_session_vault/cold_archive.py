"""Archive AI-reviewed, complete cold batches; enforce byte and path boundaries."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile

from .archive import restore_bundle_member, sha256_file
from .archive_maintenance import (
    local_root, no_links, preflight, read_json, session_gate, write_json,
)
from .config import VaultConfig

HISTORY = ("runs", "audit", "audits", "work", "episodes", "history", "recovery", "source-retention", "runtime")
MATERIALS = ("recovery", "backups", "thread-repair-backups", "retrospection-runs", "worktree-cleanup-evidence", "task-archives")
CONTROLS = {"automation.toml", "memory.md", "state.json", "current.json", "current", "latest", "latest.json"}


def _category(root: Path, path: Path) -> str:
    """Validate scope independently of editable inventory/decision documents."""
    no_links(path)
    parts = path.relative_to(root).parts
    if path.name in CONTROLS or path.name in {"site", "sites", "shared", "node_modules", ".venv"}:
        raise ValueError("current controls and shared runtimes are protected")
    if len(parts) == 5 and parts[0] == "visualizations":
        return "visualizations"
    if len(parts) == 2 and parts[0] in (*MATERIALS, "generated_images"):
        return parts[0]
    if len(parts) == 4 and parts[:3] == ("projects", "redcube-ai", "workspaces"):
        return "projects/redcube-ai"
    if len(parts) == 4 and parts[0] == "automations" and parts[2] in HISTORY:
        return "automations"
    if len(parts) == 4 and parts[:3] == ("packages", "standalone", "releases"):
        return "packages/standalone"
    if len(parts) == 4 and parts[:3] == (".tmp", "marketplaces", ".staging"):
        return ".tmp/marketplaces/.staging"
    raise ValueError(f"path is outside whole-batch cold-data scope: {path}")


def _signature(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_nlink, stat.S_IMODE(info.st_mode)]


def _hash(path: Path) -> str:
    # O_NOFOLLOW protects the final path as well as the previously checked parents.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        before = _signature(os.fstat(source.fileno()))
        digest = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
        if before != _signature(os.fstat(source.fileno())) or before != _signature(path.lstat()):
            raise ValueError(f"file changed while reading: {path}")
        return digest.hexdigest()


def collect(root: Path, source: Path, *, hashes: bool) -> dict:
    no_links(source)
    if source == root or not source.is_relative_to(root):
        raise ValueError("cold unit must be strictly inside Codex home")
    device = root.stat().st_dev
    files, directories, latest = [], [], 0.0
    stack = [source]
    while stack:
        path = stack.pop()
        info = path.lstat()
        if info.st_dev != device:
            raise ValueError(f"cross-volume batch excluded: {path}")
        latest = max(latest, info.st_mtime, info.st_ctime, getattr(info, "st_birthtime", 0))
        member = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            directories.append({"member": member, "signature": _signature(info)})
            stack.extend(Path(item.path) for item in os.scandir(path))
        elif stat.S_ISREG(info.st_mode):
            dependency_locks = {"uv.lock", "yarn.lock", "bun.lock", "poetry.lock", "Pipfile.lock", "Cargo.lock"}
            if path.name in {".git", ".lease", "lease", "lock"} or path.suffix == ".lease" or (
                    path.suffix == ".lock" and path.name not in dependency_locks):
                raise ValueError(f"worktree or lock/lease material is retained: {path}")
            row = {"member": member, "bytes": info.st_size, "signature": _signature(info)}
            if hashes:
                row["sha256"] = _hash(path)
                if _signature(path.lstat()) != row["signature"]:
                    raise ValueError(f"file changed during hashing: {path}")
            files.append(row)
        else:
            raise ValueError(f"symlink or special file excluded: {path}")
    return {"files": sorted(files, key=lambda item: item["member"]),
            "directories": sorted(directories, key=lambda item: item["member"]),
            "latest_write_creation_or_ctime": datetime.fromtimestamp(latest, UTC).isoformat(),
            "file_count": len(files), "logical_bytes": sum(item["bytes"] for item in files)}


def _children(path: Path) -> list[Path]:
    if path.is_symlink():
        return []
    no_links(path)
    return sorted(path.iterdir()) if path.is_dir() else []


def _units(root: Path):
    no_links(root)
    # Walk only date/session levels; glob/rglob may traverse a symlink parent.
    level = [root / "visualizations"]
    for _ in range(4):
        level = [child for parent in level if not parent.is_symlink() for child in _children(parent)
                 if not child.is_symlink()]
    yield from (("visualizations", path) for path in level)
    yield from (("generated_images", path) for path in _children(root / "generated_images"))
    for category in MATERIALS:
        yield from ((category, path) for path in _children(root / category))
    yield from (("projects/redcube-ai", path) for path in _children(root / "projects/redcube-ai/workspaces"))
    for automation in _children(root / "automations"):
        if automation.is_symlink() or not automation.is_dir():
            continue
        for name in HISTORY:
            parent = automation / name
            if not parent.is_symlink():
                yield from (("automations", path) for path in _children(parent))
    yield from (("packages/standalone", path) for path in _children(root / "packages/standalone/releases"))
    yield from ((".tmp/marketplaces/.staging", path) for path in _children(root / ".tmp/marketplaces/.staging"))


def _protected_versions(config: VaultConfig) -> set[str] | None:
    root = config.paths.home / ".codex/packages/standalone/releases"
    versions = [path for path in _children(root) if path.is_dir() and not path.is_symlink()]
    if not versions:
        return set()
    executable = (config.paths.home / ".local/bin/codex").resolve()
    current = [path for path in versions if executable.is_relative_to(path)]
    pointer = config.paths.home / ".codex/packages/standalone/current"
    if len(current) != 1 or pointer.resolve() != current[0]:
        return None
    def key(path):
        return tuple(int(part) for part in re.findall(r"\d+", path.name)[:3])
    if any(not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].+)?", path.name) for path in versions):
        return None
    return {current[0].name, *(path.name for path in sorted(
        (path for path in versions if path != current[0]), key=key, reverse=True)[:2])}


def inventory(config: VaultConfig, run_dir: Path, *, now: datetime | None = None) -> dict:
    session_gate(config, run_dir)
    preflight(config)
    write_json(run_dir / "cold-inventory.intent.json", {"started_at": datetime.now(UTC).isoformat()})
    root = config.paths.home / ".codex"
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=30)
    versions = _protected_versions(config)
    rows = []
    for category, path in _units(root):
        row = {"category": category, "source": str(path), "batch": str(path)}
        try:
            if _category(root, path) != category:
                raise ValueError("candidate category differs from exact scope")
            no_links(path)
            info = path.lstat()
            if path.name in CONTROLS or path.name in {"site", "sites", "shared", "node_modules", ".venv"}:
                row["status"] = "keep_current_control_or_runtime"
            elif category == "packages/standalone" and (versions is None or path.name in versions):
                row["status"] = "keep_current_recent_versions_or_unresolved_pointer"
            elif max(info.st_mtime, info.st_ctime, getattr(info, "st_birthtime", 0)) >= cutoff.timestamp():
                row["status"] = "keep_recent_batch_root"
            else:
                row.update(collect(root, path, hashes=False))
                row["status"] = ("cold_for_review" if datetime.fromisoformat(row["latest_write_creation_or_ctime"]) < cutoff
                                 and row["file_count"] else "keep_recent_descendant_or_empty")
        except (OSError, ValueError) as exc:
            row.update(status="keep_unknown_or_link", reason=str(exc))
        rows.append(row)
    categories = {name: {"units": 0, "cold_for_review": 0, "cold_logical_bytes": 0} for name in
                  ("visualizations", "generated_images", *MATERIALS, "projects/redcube-ai", "automations",
                   "packages/standalone", ".tmp/marketplaces/.staging")}
    for row in rows:
        summary = categories[row["category"]]
        summary["units"] += 1
        if row["status"] == "cold_for_review":
            summary["cold_for_review"] += 1
            summary["cold_logical_bytes"] += row["logical_bytes"]
    result = {"schema_version": "codex-cold-inventory.v1", "status": "measured", "run_id": run_dir.name,
              "root": str(root), "measured_at": now.isoformat(), "cutoff": cutoff.isoformat(), "units": rows,
              "categories": categories}
    write_json(run_dir / "cold-inventory.json", result)
    return {"status": "measured", "inventory_path": str(run_dir / "cold-inventory.json"),
            "inventory_sha256": sha256_file(run_dir / "cold-inventory.json"), "units": len(rows),
            "cold_for_review": sum(row["status"] == "cold_for_review" for row in rows), "cutoff": cutoff.isoformat(),
            "categories": categories}


def _references(config: VaultConfig) -> tuple[list[tuple[str, str]], str, list[str]]:
    controls = []
    for automation in _children(config.paths.home / ".codex/automations"):
        if not automation.is_dir() or automation.is_symlink():
            continue
        paths = [automation / name for name in CONTROLS]
        for folder in (automation / "state", automation / "heartbeat-recovery"):
            if folder.exists():
                no_links(folder)
                for base, dirs, files in os.walk(folder, followlinks=False):
                    if any(Path(base, name).is_symlink() for name in dirs + files):
                        raise ValueError("current control scan has links; retain uncertain batches")
                    paths.extend(Path(base, name) for name in files)
        for path in paths:
            if path.is_file():
                no_links(path)
                if path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError(f"current control too large for complete scan: {path}")
                content = path.read_text(encoding="utf-8")
                if path.name == "memory.md":
                    lines = content.splitlines()
                    content = "\n".join(lines[:100] + lines[-100:])
                controls.append((str(path), content))
    # Current local config/scripts can point to retired releases or workspaces.
    for path in _children(local_root(config)):
        if path.suffix in {".toml", ".py"} and path.is_file():
            no_links(path)
            controls.append((str(path), path.read_text(encoding="utf-8")))
    processes = subprocess.run(["/bin/ps", "-ww", "-axo", "pid=,command="], capture_output=True, text=True,
                               check=True, timeout=15).stdout
    opened = subprocess.run(["/usr/sbin/lsof", "-b", "-n", "-P", "-a", "-u", str(os.getuid()), "-Fpn"],
                            capture_output=True, text=True, timeout=45)
    if opened.returncode != 0:
        raise ValueError("cannot establish open-file references; retain cold batches")
    return controls, processes, [line[1:] for line in opened.stdout.splitlines() if line.startswith("n/")]


def _reference_hit(unit: dict, root: Path, references) -> str | None:
    path = Path(unit["source"])
    needles = [str(path), path.relative_to(root).as_posix(), path.name, path.stem]
    controls, processes, opened = references
    for control, text in controls:
        if any(needle in text for needle in needles):
            return f"current_control_reference:{control}"
    if str(path) in processes or any(name == str(path) or name.startswith(str(path) + "/") for name in opened):
        return "active_process_or_open_file"
    if unit["category"] == ".tmp/marketplaces/.staging" and re.search(
            r"(?:codex|npm|uv|plugin|marketplace).*\b(?:install|upgrade|update)\b", processes, re.IGNORECASE):
        return "installation_or_upgrade_process"
    return None


def _evidence(paths: list[str]) -> list[dict]:
    if not paths:
        raise ValueError("review must cite persistent completion/reference evidence")
    result = []
    for raw in paths:
        path = Path(raw)
        no_links(path)
        if not path.is_file():
            raise ValueError(f"review evidence not found: {path}")
        result.append({"path": str(path), "sha256": _hash(path)})
    return result


def _asset_references(config: VaultConfig, units: list[dict]) -> set[str]:
    """A failed search is unknown, never evidence that an asset is unreferenced."""
    assets = [unit for unit in units if unit["category"] in {"visualizations", "generated_images"}]
    if not assets:
        return set()
    # Batch all patterns into one search, and use only existing, ordinary roots.
    scopes = [config.paths.home / ".codex/sessions", config.paths.workspace_root]
    if any(not path.is_dir() or path.is_symlink() for path in scopes):
        return {unit["source"] for unit in assets}
    command = ["rg", "--hidden", "--search-zip", "--files-with-matches", "--fixed-strings",
               "--glob", "!**/node_modules/**", "--glob", "!**/.git/**", "--glob", "!**/.venv/**",
               "--glob", "!**/.env*", "--glob", "!**/auth.json"]
    for unit in assets:
        command.extend(["-e", unit["source"], "-e", Path(unit["source"]).name])
    result = subprocess.run([*command, "--", *(str(path) for path in scopes)],
                            capture_output=True, text=True, timeout=180)
    if result.returncode != 1:
        # A hit or an incomplete scan conservatively retains this asset group.
        return {unit["source"] for unit in assets}
    return set()


def build_plan(config: VaultConfig, run_dir: Path, decisions_path: Path) -> dict:
    from .archive_maintenance import require_stage
    session_gate(config, run_dir)
    measured_stage = require_stage(run_dir, "cold-inventory", "measured")
    preflight(config)
    write_json(run_dir / "cold-plan.intent.json", {"decisions_path": str(decisions_path)})
    no_links(decisions_path)
    if decisions_path.parent != run_dir:
        raise ValueError("decisions must be stored in this persistent run directory")
    decisions = read_json(decisions_path)
    inventory_path = run_dir / "cold-inventory.json"
    measured = read_json(inventory_path)
    if measured_stage.get("inventory_sha256") != sha256_file(inventory_path):
        raise ValueError("inventory differs from original runner output")
    if (decisions.get("schema_version") != "codex-cold-decisions.v1" or decisions.get("run_id") != run_dir.name
            or decisions.get("inventory_sha256") != sha256_file(inventory_path)):
        raise ValueError("review does not match this run inventory")
    root = config.paths.home / ".codex"
    cutoff = datetime.fromisoformat(measured["cutoff"])
    if cutoff != datetime.fromisoformat(measured["measured_at"]) - timedelta(days=30):
        raise ValueError("inventory cutoff is not a rolling 30-day boundary")
    rows = {row["source"]: row for row in measured["units"]}
    references = _references(config)
    selected, retained, seen = [], [], set()
    requests = decisions.get("archive", [])
    for request in requests:
        raw = request["source"]
        if raw in seen or raw not in rows:
            raise ValueError("duplicate or out-of-inventory cold unit")
        seen.add(raw)
        row = rows[raw]
        if _category(root, Path(raw)) != row["category"]:
            raise ValueError("candidate is outside the allowed category")
        if row["status"] != "cold_for_review":
            raise ValueError(f"unit is protected or not cold: {raw}")
        if not all(isinstance(request.get(key), str) and request[key].strip() for key in ("completion", "reference_review")):
            raise ValueError("AI review must explain completion and current-use checks")
        proof = _evidence(request.get("evidence_paths", []))
        if Path(raw).parts[-2] == "runtime" and request.get("retired_runtime") is not True:
            raise ValueError("runtime batch needs explicit retirement evidence")
        hit = _reference_hit(row, root, references)
        if hit:
            retained.append({"source": raw, "category": row["category"], "reason": hit})
            continue
        tree = collect(root, Path(raw), hashes=True)
        if datetime.fromisoformat(tree["latest_write_creation_or_ctime"]) >= cutoff:
            raise ValueError(f"batch became recent: {raw}")
        selected.append({"source": raw, "batch": row["batch"], "category": row["category"], **tree,
                         "completion": request["completion"], "reference_review": request["reference_review"],
                         "evidence": proof, "retired_runtime": request.get("retired_runtime", False)})
    assets_retained = _asset_references(config, selected)
    for unit in selected[:]:
        if unit["source"] in assets_retained:
            selected.remove(unit)
            retained.append({"source": unit["source"], "category": unit["category"],
                             "reason": "asset_reference_found_or_scan_incomplete"})
    for row in measured["units"]:
        if row["source"] not in seen:
            retained.append({"source": row["source"], "category": row["category"],
                             "reason": row["status"] if row["status"] != "cold_for_review" else "not_confirmed_complete_and_unreferenced"})
    for index, unit in enumerate(selected):
        path = Path(unit["source"])
        if any(path.is_relative_to(Path(other["source"])) or Path(other["source"]).is_relative_to(path)
               for other in selected[:index]):
            raise ValueError("overlapping whole batches are excluded")
        unit["source_sha256"] = hashlib.sha256(json.dumps(unit["files"], sort_keys=True).encode()).hexdigest()
    plan = {"schema_version": "codex-cold-plan.v1", "status": "planned", "run_id": run_dir.name,
            "root": str(root), "cutoff": measured["cutoff"], "inventory_sha256": sha256_file(inventory_path),
            "decisions_sha256": sha256_file(decisions_path), "created_at": datetime.now(UTC).isoformat(),
            "units": selected, "retained": retained,
            "file_count": sum(unit["file_count"] for unit in selected),
            "logical_bytes": sum(unit["logical_bytes"] for unit in selected)}
    write_json(run_dir / "cold-plan.json", plan)
    return {"status": "planned", "plan_path": str(run_dir / "cold-plan.json"),
            "plan_sha256": sha256_file(run_dir / "cold-plan.json"), "units": len(selected),
            "file_count": plan["file_count"], "logical_bytes": plan["logical_bytes"], "retained": len(retained)}


def verify_sources(config: VaultConfig, plan: dict) -> None:
    root = config.paths.home / ".codex"
    if plan.get("root") != str(root):
        raise ValueError("plan root differs from local Codex home")
    references = _references(config)
    versions = _protected_versions(config)
    for unit in plan["units"]:
        path = Path(unit["source"])
        if _category(root, path) != unit["category"]:
            raise ValueError("plan contains an out-of-scope batch")
        if path.parts[-2] == "runtime" and unit.get("retired_runtime") is not True:
            raise ValueError("runtime batch lacks retirement evidence")
        if unit["category"] == "packages/standalone" and (versions is None or path.name in versions):
            raise ValueError("release became current or recently protected")
        hit = _reference_hit(unit, root, references)
        if hit:
            raise ValueError(f"batch acquired a current reference: {path}: {hit}")
        for evidence in unit["evidence"]:
            no_links(Path(evidence["path"]))
            if _hash(Path(evidence["path"])) != evidence["sha256"]:
                raise ValueError("completion/reference evidence changed")
        tree = collect(root, path, hashes=True)
        if any(tree[key] != unit[key] for key in ("files", "directories", "file_count", "logical_bytes")):
            raise ValueError(f"whole batch changed since plan: {path}")
        if datetime.fromisoformat(tree["latest_write_creation_or_ctime"]) >= datetime.fromisoformat(plan["cutoff"]):
            raise ValueError(f"batch is no longer cold: {path}")
    if _asset_references(config, plan["units"]):
        raise ValueError("asset acquired a reference or current-reference search is incomplete")


def inspect_package(path: Path, units: list[dict]) -> tuple[dict, str]:
    """One streaming pass checks paths, structure, hardlink contents and hashes."""
    expected = {file["member"]: (file["bytes"], file["sha256"]) for unit in units for file in unit["files"]}
    directories = {row["member"] for unit in units for row in unit["directories"]}
    actual, links, members, regular = {}, {}, set(), []
    process = subprocess.Popen(["zstd", "-dc", "--", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                relative = PurePosixPath(name)
                if not name or relative.is_absolute() or ".." in relative.parts or relative.as_posix() != name or name in members:
                    raise ValueError(f"unsafe or duplicate archive member: {name}")
                members.add(name)
                if member.isreg():
                    source = archive.extractfile(member)
                    digest, size = hashlib.sha256(), 0
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                        size += len(block)
                    actual[name] = (size, digest.hexdigest())
                    regular.append(name)
                elif member.islnk():
                    links[name] = member.linkname
                elif not member.isdir():
                    raise ValueError(f"unsupported archive member type: {name}")
        # Drain padding before wait(), including unusually large TAR end padding.
        while process.stdout.read(1024 * 1024):
            pass
    finally:
        process.stdout.close()
        code = process.wait()
    if code != 0:
        raise ValueError("compressed TAR did not decode successfully")
    while links:
        resolved = [name for name, target in links.items() if target in actual]
        if not resolved:
            raise ValueError("hardlink has a missing, external or cyclic target")
        for name in resolved:
            actual[name] = actual[links.pop(name)]
    if actual != expected or members != set(expected) | directories:
        raise ValueError("archive members, logical bytes or SHA-256 differ from source manifest")
    if not regular:
        raise ValueError("archive has no restore sample")
    sample = min(regular, key=lambda name: abs(expected[name][0] - 4096))
    return expected, sample


def _plan(config: VaultConfig, run_dir: Path) -> dict:
    session_gate(config, run_dir)
    from .archive_maintenance import require_stage
    stage = require_stage(run_dir, "cold-plan", "planned")
    path = run_dir / "cold-plan.json"
    if stage.get("plan_sha256") != sha256_file(path):
        raise ValueError("cold plan differs from original runner output")
    plan = read_json(path)
    if plan.get("run_id") != run_dir.name or plan.get("schema_version") != "codex-cold-plan.v1":
        raise ValueError("cold plan belongs to another run")
    return plan


def _destinations(config: VaultConfig, run_dir: Path) -> tuple[Path, Path]:
    root = config.archive.root.parent
    auxiliary = root / "auxiliary/codex-home-archives" / run_dir.name
    evidence = root / "operations/space-optimization" / run_dir.name
    no_links(auxiliary)
    no_links(evidence)
    return auxiliary, evidence


def pack(config: VaultConfig, run_dir: Path) -> dict:
    plan = _plan(config, run_dir)
    preflight(config)
    write_json(run_dir / "cold-pack.intent.json", {"plan_sha256": sha256_file(run_dir / "cold-plan.json")})
    verify_sources(config, plan)
    auxiliary, evidence = _destinations(config, run_dir)
    evidence.mkdir(parents=True, exist_ok=False)
    if plan["units"]:
        auxiliary.mkdir(parents=True, exist_ok=False)
    packages = []
    root = config.paths.home / ".codex"
    for category in sorted({unit["category"] for unit in plan["units"]}):
        preflight(config)
        units = [unit for unit in plan["units"] if unit["category"] == category]
        name = "cold-" + category.replace("/", "-").replace(".", "") + ".tar.zst"
        final = auxiliary / name
        temporary = auxiliary / (name + ".part")
        paths = sorted({file["member"] for unit in units for file in unit["files"]}
                       | {row["member"] for unit in units for row in unit["directories"]})
        list_path = run_dir / (name + ".files.nul")
        with list_path.open("xb") as output:
            for member in paths:
                output.write(member.encode("utf-8", errors="surrogateescape") + b"\0")
        # Reserve our own partial file exclusively, then write directly by name.
        # macOS bsdtar pads compressed stdout to a TAR block boundary; zstd
        # correctly rejects those trailing bytes. Named-file output avoids it.
        with temporary.open("xb"):
            pass
        subprocess.run(["bsdtar", "--no-recursion", "--no-xattrs", "--no-mac-metadata", "--no-acls", "--no-fflags",
                        "--zstd", "-cf", str(temporary), "-C", str(root), "--null", "-T", str(list_path)], check=True)
        expected, sample = inspect_package(temporary, units)
        destination = run_dir / "restore-probe" / category / sample
        no_links(destination)
        if destination.exists():
            raise ValueError("restore probe destination already exists")
        if restore_bundle_member(temporary, sample, destination) != expected[sample] or _hash(destination) != expected[sample][1]:
            raise ValueError("real staging restore sample differs from manifest")
        verify_sources(config, {**plan, "units": units})
        digest = sha256_file(temporary)
        if final.exists():
            raise ValueError("immutable archive already exists")
        temporary.rename(final)
        packages.append({"category": category, "path": str(final), "sha256": digest,
                         "compressed_bytes": final.stat().st_size, "file_count": len(expected),
                         "logical_bytes": sum(size for size, _ in expected.values()),
                         "restore_sample": {"member": sample, "path": str(destination), "sha256": expected[sample][1]}})
    receipt = {"status": "verified", "run_id": run_dir.name, "plan_sha256": sha256_file(run_dir / "cold-plan.json"),
               "packages": packages, "file_count": plan["file_count"], "logical_bytes": plan["logical_bytes"],
               "restore_root": str(auxiliary), "evidence_root": str(evidence)}
    write_json(run_dir / "cold-pack.receipt.json", receipt)
    preflight(config)
    for name in ("cold-inventory.json", "cold-plan.json", "cold-pack.receipt.json"):
        with (evidence / name).open("xb") as output:
            output.write((run_dir / name).read_bytes())
        if sha256_file(evidence / name) != sha256_file(run_dir / name):
            raise ValueError("NAS manifest readback differs")
    return receipt


def metrics(root: Path) -> dict:
    result = {"measured_at": datetime.now(UTC).isoformat()}
    filesystem = os.statvfs(root)
    result["volume_available_bytes"] = filesystem.f_bavail * filesystem.f_frsize
    usage = subprocess.run(["/usr/bin/du", "-sk", str(root)], capture_output=True, text=True, timeout=120)
    result["codex_allocated_bytes"] = int(usage.stdout.split()[0]) * 1024 if usage.returncode == 0 else None
    result["databases"] = [{"path": str(path), "bytes": path.stat().st_size}
                           for path in root.iterdir() if path.is_file() and not path.is_symlink()
                           and re.search(r"(?:\.sqlite|\.db)(?:-(?:wal|shm))?$", path.name)]
    return result


def _parent_fd(root: Path, relative: str) -> tuple[int, str]:
    parts = PurePosixPath(relative).parts
    if not parts or ".." in parts or PurePosixPath(relative).is_absolute():
        raise ValueError("unsafe cleanup member")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nested = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = nested
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def purge_exact(trash: Path, expected: dict, directories: set[str], journal) -> None:
    """Delete only manifest files with no-follow directory descriptors; never rmtree."""
    observed = collect(trash.parent, trash, hashes=True)
    prefix = trash.name + "/"
    actual = {file["member"].removeprefix(prefix): (file["bytes"], file["sha256"]) for file in observed["files"]}
    actual_dirs = {row["member"].removeprefix(prefix) for row in observed["directories"]
                   if row["member"] != trash.name}
    if actual != expected or actual_dirs != directories:
        raise ValueError("dedicated Trash files differ from verified NAS coverage")
    signatures = {file["member"].removeprefix(prefix): file["signature"] for file in observed["files"]}
    removed_links = {}
    for member in sorted(expected):
        descriptor, name = _parent_fd(trash, member)
        try:
            actual_signature = _signature(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
            original = signatures[member]
            inode = tuple(original[:2])
            if actual_signature != original:
                removed = removed_links.get(inode, 0)
                # Removing an earlier manifest hardlink changes ctime/nlink of
                # its remaining aliases. Rehash that alias before accepting only
                # this known metadata change; external link changes still fail.
                if (not removed or any(actual_signature[index] != original[index] for index in (0, 1, 2, 3, 6))
                        or actual_signature[5] != original[5] - removed
                        or _hash(trash / member) != expected[member][1]):
                    raise ValueError("Trash file changed before unlink")
            journal.write(json.dumps({"action": "unlink_intent", "member": member}) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
            os.unlink(name, dir_fd=descriptor)
            removed_links[inode] = removed_links.get(inode, 0) + 1
        finally:
            os.close(descriptor)
    # Include structural ancestors created solely inside this dedicated subtree.
    for base, dirs, files in os.walk(trash, topdown=False, followlinks=False):
        if files or any(Path(base, name).is_symlink() for name in dirs):
            raise ValueError("unexpected files or links appeared during purge")
        relative = Path(base).relative_to(trash)
        if relative.parts:
            descriptor, name = _parent_fd(trash, relative.as_posix())
            try:
                os.rmdir(name, dir_fd=descriptor)
            finally:
                os.close(descriptor)
    trash.rmdir()


def cleanup(config: VaultConfig, run_dir: Path) -> dict:
    from .archive_maintenance import require_stage
    plan = _plan(config, run_dir)
    packed = require_stage(run_dir, "cold-pack", "verified")
    if packed != read_json(run_dir / "cold-pack.receipt.json") or packed.get("run_id") != run_dir.name:
        raise ValueError("pack receipt differs from this run's terminal stdout")
    if packed.get("plan_sha256") != sha256_file(run_dir / "cold-plan.json"):
        raise ValueError("packed bytes belong to a different plan")
    preflight(config)
    verify_sources(config, plan)
    auxiliary, evidence = _destinations(config, run_dir)
    expected = {file["member"]: (file["bytes"], file["sha256"]) for unit in plan["units"] for file in unit["files"]}
    for package in packed["packages"]:
        path = Path(package["path"])
        no_links(path)
        if path.parent != auxiliary or sha256_file(path) != package["sha256"]:
            raise ValueError("NAS package changed or left this run namespace")
        inspect_package(path, [unit for unit in plan["units"] if unit["category"] == package["category"]])
    if {item["category"] for item in packed["packages"]} != {unit["category"] for unit in plan["units"]}:
        raise ValueError("NAS packages do not cover all planned categories")
    root = config.paths.home / ".codex"
    trash = config.paths.home / ".Trash" / ("codex-cold-" + run_dir.name)
    no_links(trash)
    before = metrics(root)
    intent = {"status": "verified_for_trash_move", "run_id": run_dir.name, "trash": str(trash),
              "plan_sha256": sha256_file(run_dir / "cold-plan.json"), "before": before}
    preflight(config)
    write_json(run_dir / "cold-cleanup.intent.json", intent)
    if expected:
        trash.parent.mkdir(mode=0o700, exist_ok=True)
        if trash.parent.stat().st_dev != root.stat().st_dev:
            raise ValueError("dedicated Trash must be on the source filesystem")
        trash.mkdir(mode=0o700)
        with (run_dir / "cold-moves.jsonl").open("x") as journal:
            for unit in plan["units"]:
                source = Path(unit["source"])
                destination = trash / source.relative_to(root)
                no_links(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError("Trash destination already exists")
                tree = collect(root, source, hashes=True)
                if unit["category"] == "packages/standalone":
                    versions = _protected_versions(config)
                    if versions is None or source.name in versions:
                        raise ValueError("release became current immediately before Trash move")
                if tree["files"] != unit["files"] or tree["directories"] != unit["directories"]:
                    raise ValueError("source changed immediately before Trash move")
                journal.write(json.dumps({"action": "move_intent", "source": str(source), "trash": str(destination)}) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
                source.rename(destination)
                journal.write(json.dumps({"action": "moved", "source": str(source), "trash": str(destination)}) + "\n")
                journal.flush()
        references = _references(config)
        for unit in plan["units"]:
            if _reference_hit(unit, root, references):
                raise ValueError("batch acquired a reference after Trash move; preserve Trash")
        if any(path == str(trash) or path.startswith(str(trash) + "/") for path in references[2]):
            raise ValueError("dedicated Trash has an open file; preserve it")
        preflight(config)
        for package in packed["packages"]:
            if sha256_file(Path(package["path"])) != package["sha256"]:
                raise ValueError("NAS package changed before purge")
        with (run_dir / "cold-purge.jsonl").open("x") as journal:
            directories = {row["member"] for unit in plan["units"] for row in unit["directories"]}
            for member in [*expected, *directories]:
                directories.update(parent.as_posix() for parent in PurePosixPath(member).parents if parent.parts)
            purge_exact(trash, expected, directories, journal)
    after = metrics(root)
    receipt = {**intent, "status": "purged" if expected else "no_candidates", "after": after,
               "purged_files": len(expected), "purged_logical_bytes": sum(size for size, _ in expected.values()),
               "trash_exists": trash.exists(), "packages": packed["packages"], "retained": plan["retained"],
               "finished_at": datetime.now(UTC).isoformat(),
               "space_note": "logical purged bytes are not physical SSD recovery; volume free space includes snapshots and concurrent writes"}
    write_json(run_dir / "cold-cleanup.receipt.json", receipt)
    preflight(config)
    for name in ("cold-cleanup.intent.json", "cold-cleanup.receipt.json", "cold-moves.jsonl", "cold-purge.jsonl"):
        source = run_dir / name
        if source.exists():
            with (evidence / name).open("xb") as output:
                output.write(source.read_bytes())
            if sha256_file(source) != sha256_file(evidence / name):
                raise ValueError("NAS cleanup receipt readback differs")
    return receipt

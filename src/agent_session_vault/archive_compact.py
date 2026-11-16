"""Explicit, one-shot storage migration; logical snapshot history never changes."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tarfile
import threading

from .archive import _resolve_hardlinks, bundle_member_digests, restore_bundle_member, sha256_file
from .archive_backend import FilesystemArchiveBackend
from .archive_catalog import write_catalog_segment
from .archive_content import ArchiveContentIndex
from .archive_maintenance import local_root, no_links, preflight, read_json, stage_status, write_json
from .archive_model import ArchiveBundle, payload_sha256, utc_now, validate_relative_path
from .config import VaultConfig


SCHEMA = "archive.compaction-plan.v1"
CHUNK_BYTES = 512 * 1024 * 1024


def _run_paths(config, run_dir):
    no_links(run_dir)
    if (run_dir.parent != local_root(config) / "ops/archive-compaction"
            or not run_dir.name or run_dir.name.startswith(".")):
        raise ValueError("compaction run must be an immediate child of ops/archive-compaction")
    root = config.archive.root
    no_links(root)
    work = root.parent / "operations/archive-compaction" / run_dir.name
    no_links(work)
    return root, work


def _signature(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError(f"expected ordinary file: {path}")
    return [value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_dev, value.st_ino, value.st_nlink]


def _inventory(root, *, hashes):
    no_links(root)
    files, directories = {}, []
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs):
            path = Path(base) / name
            if path.is_symlink():
                raise ValueError(f"symlink excluded from migration: {path}")
            directories.append(path.relative_to(root).as_posix())
        for name in sorted(names):
            path = Path(base) / name
            member = path.relative_to(root).as_posix()
            before = _signature(path)
            row = {"signature": before, "bytes": before[0]}
            if hashes and not member.startswith("objects/"):
                row["sha256"] = sha256_file(path)
            if before != _signature(path):
                raise ValueError(f"source changed while inventorying: {path}")
            files[member] = row
    return {"files": files, "directories": sorted(directories)}


def _unchanged(root, inventory):
    actual = _inventory(root, hashes=False)
    expected = {"files": {name: {k: v for k, v in row.items() if k != "sha256"}
                          for name, row in inventory["files"].items()},
                "directories": inventory["directories"]}
    if actual != expected:
        raise ValueError(f"archive changed since plan; retain all generations: {root}")


def _logical_digest(backend):
    """Keep every logical field and order, excluding only physical addresses."""
    snapshots, digest = [], hashlib.sha256()
    for directory in backend.iter_snapshot_dirs():
        published = backend.load_verified_metadata(directory)
        payload = read_json(directory / "snapshot.json")
        payload.pop("manifest_sha256")
        manifests = []
        for item in read_json(directory / "manifest.json")["manifests"]:
            item.pop("bundles")
            item["files"] = [{k: v for k, v in row.items() if k not in {"member", "bundle_id"}}
                             for row in item["files"]]
            manifests.append(item)
        value = {"snapshot": payload, "manifests": manifests}
        digest.update(bytes.fromhex(payload_sha256(value)))
        snapshots.append(published.snapshot.snapshot_id)
    return {"snapshot_ids": snapshots, "logical_sha256": digest.hexdigest()}


def compact_plan(config: VaultConfig, run_dir: Path) -> dict:
    preflight(config)
    root, work = _run_paths(config, run_dir)
    backend = FilesystemArchiveBackend(root)
    backend.ensure_writable()
    marker = backend.ensure_ready()
    index = ArchiveContentIndex.load(backend)
    if not index.snapshots:
        raise ValueError("no committed archive history to compact")
    if (root / "staging").exists() and any((root / "staging").iterdir()):
        raise ValueError("archive staging is not empty; reconcile original runners first")
    inventory = _inventory(root, hashes=True)
    allowed = {"objects", "snapshots", "catalog", "receipts", "staging", "root-marker.json"}
    if any(path.split("/")[0] not in allowed for path in (*inventory["files"], *inventory["directories"])):
        raise ValueError("unknown archive root content; reconcile before migration")
    committed = [directory.relative_to(root).as_posix() + "/" for directory in backend.iter_snapshot_dirs()]
    if any(name.startswith("snapshots/") and not any(name.startswith(prefix) for prefix in committed)
           for name in inventory["files"]):
        raise ValueError("uncommitted snapshot evidence must be reconciled before migration")
    objects = {location.bundle.object_path for location in index.locations.values()}
    # Include duplicate-only bundles, not just the index's preferred locations.
    for directory in backend.iter_snapshot_dirs():
        for manifest in backend.load_verified_metadata(directory).manifests:
            objects.update(bundle.object_path for bundle in manifest.bundles)
    if objects != {name for name in inventory["files"] if name.startswith("objects/")}:
        raise ValueError("unreferenced or missing objects require explicit reconciliation")
    old_bytes = sum(inventory["files"][name]["bytes"] for name in objects)
    available = shutil.disk_usage(root).free
    # Incompressible contents, TAR headers/padding, and the metadata backup fit.
    required = int(sum(size for size, _ in index.locations) * 1.02) + 1024**3
    if available < required:
        raise ValueError(f"insufficient NAS workspace: {available} < {required}")
    plan = {"schema_version": SCHEMA, "status": "planned", "created_at": utc_now(),
            "run_id": run_dir.name, "root": str(root), "work": str(work), "marker": marker,
            "inventory": inventory, "history": _logical_digest(backend), "content": index.report(),
            "old_object_bytes": old_bytes, "nas_available_bytes": available,
            "required_workspace_bytes": required, "chunk_logical_bytes": CHUNK_BYTES}
    plan["plan_digest"] = payload_sha256(plan)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "plan.json", plan)
    return {k: v for k, v in plan.items() if k != "inventory"}


def _load_plan(config, path):
    plan = read_json(path)
    if (plan.get("schema_version") != SCHEMA
            or plan.get("plan_digest") != payload_sha256({k: v for k, v in plan.items() if k != "plan_digest"})):
        raise ValueError("compaction plan digest/schema mismatch")
    root, work = _run_paths(config, path.parent)
    if str(root) != plan["root"] or str(work) != plan["work"] or path.name != "plan.json":
        raise ValueError("compaction plan path/config mismatch")
    return plan, root, work


class _HashReader:
    def __init__(self, source):
        self.source, self.digest, self.size = source, hashlib.sha256(), 0

    def read(self, size=-1):
        value = self.source.read(size)
        self.digest.update(value)
        self.size += len(value)
        return value

    def value(self):
        return self.size, self.digest.hexdigest()


class _PackWriter:
    """Only explicit regular members: no filesystem traversal, links or xattrs."""
    def __init__(self, candidate, limit, progress):
        self.candidate, self.limit, self.progress = candidate, limit, progress
        self.locations, self.bundles = {}, {}
        self.process = self.archive = self.output = None
        self.sink = None
        self.keys, self.total, self.sequence = [], 0, 0

    def add(self, key, source):
        if self.archive is not None and self.total + key[0] > self.limit:
            self.finish()
        if self.archive is None:
            self.sequence += 1
            self.pending = self.candidate / "objects" / f".pack-{self.sequence}.pending"
            self.output = self.pending.open("xb")
            self.process = subprocess.Popen(["zstd", "-q", "-T2", "-3", "-c"], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE)
            self.output_digest, self.sink_error = hashlib.sha256(), None

            def save_output():
                try:
                    for block in iter(lambda: self.process.stdout.read(1024**2), b""):
                        self.output.write(block)
                        self.output_digest.update(block)
                except BaseException as exc:
                    self.sink_error = exc
                    self.process.kill()
                finally:
                    self.process.stdout.close()

            self.sink = threading.Thread(target=save_output)
            self.sink.start()
            self.archive = tarfile.open(fileobj=self.process.stdin, mode="w|", bufsize=1024**2,
                                        format=tarfile.PAX_FORMAT, copybufsize=1024**2)
        member = f"content/{key[1]}"
        entry = tarfile.TarInfo(member)
        entry.size, entry.mode = key[0], 0o600
        reader = _HashReader(source)
        self.archive.addfile(entry, reader)
        if reader.value() != key:
            raise ValueError(f"source content differs from manifest: {member}")
        self.keys.append(key)
        self.total += key[0]
        # A pending location prevents duplicates within the open pack as well.
        self.locations[key] = None

    def finish(self):
        if self.archive is None:
            return
        self.archive.close()
        self.process.stdin.close()
        code = self.process.wait()
        self.sink.join()
        self.output.close()
        self.archive = self.process = self.output = None
        if code != 0 or self.sink_error is not None:
            raise RuntimeError(f"zstd pack failed: {code}, output error: {self.sink_error}")
        digest = self.output_digest.hexdigest()
        bundle = ArchiveBundle(f"bundle-{digest}", f"objects/{digest}.tar.zst", digest,
                               self.pending.stat().st_size, self.total, len(self.keys))
        destination = self.candidate / bundle.object_path
        if destination.exists():
            raise ValueError("unexpected candidate object collision")
        self.pending.rename(destination)
        self.bundles[bundle.bundle_id] = bundle
        for key in self.keys:
            self.locations[key] = (bundle, f"content/{key[1]}")
        self.keys, self.total = [], 0
        self.progress("packing", output_objects=len(self.bundles), unique_files=len(self.locations),
                      output_bytes=sum(item.bytes for item in self.bundles.values()))

    def abort(self):
        if self.archive is not None:
            try:
                self.archive.close()
            except (OSError, ValueError):
                pass
            self.archive = None
        if self.process is not None:
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                pass
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
        if self.sink is not None:
            self.sink.join()
        if self.output is not None:
            self.output.close()


@contextmanager
def _decoded(path, expected_sha256):
    """Hash compressed input while decoding it, with one NAS read per object."""
    process = subprocess.Popen(["zstd", "-dc"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    digest, errors = hashlib.sha256(), []

    def feed():
        try:
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024**2), b""):
                    digest.update(block)
                    process.stdin.write(block)
        except BaseException as exc:
            errors.append(exc)
            if process.poll() is None:
                process.kill()
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    feeder = threading.Thread(target=feed)
    feeder.start()
    try:
        yield process.stdout
        for _ in iter(lambda: process.stdout.read(1024**2), b""):
            pass
        code = process.wait()
        feeder.join()
        if digest.hexdigest() != expected_sha256:
            raise ValueError(f"object SHA-256 mismatch: {path}")
        if code != 0 or errors:
            raise RuntimeError(f"zstd decode failed: {path}")
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        feeder.join()


def _appledouble_counterpart(name, body):
    """Recognize Finder/xattr sidecars; a filename prefix alone is not authority."""
    path = Path(name)
    if not path.name.startswith("._") or len(body) < 26:
        return None
    if struct.unpack(">II", body[:8]) != (0x00051607, 0x00020000):
        return None
    count = struct.unpack(">H", body[24:26])[0]
    if not 0 < count <= 32 or len(body) < 26 + count * 12:
        return None
    for position in range(count):
        kind, offset, size = struct.unpack(">III", body[26 + position * 12:38 + position * 12])
        if kind not in {2, 9} or offset < 26 + count * 12 or offset + size > len(body) or (kind == 2 and size):
            return None
    return path.with_name(path.name[2:]).as_posix()


def _repack(backend, index, writer, progress, *, completed=()):
    expected_by_object = {}
    for (bundle_id, member), key in index.member_locations.items():
        expected_by_object.setdefault(bundle_id, {})[member] = key
    for number, (bundle_id, expected) in enumerate(sorted(expected_by_object.items()), 1):
        path = backend.object_path(bundle_id.removeprefix("bundle-"))
        if path.name in completed and all(key in writer.locations for key in expected.values()):
            progress("source_already_verified", source_object=path.name)
            continue
        progress("reading_source", source_object=path.name, source_index=number,
                 source_objects=len(expected_by_object))
        actual, links, seen, sidecars = {}, {}, set(), {}
        with _decoded(path, bundle_id.removeprefix("bundle-")) as stream:
            with tarfile.open(fileobj=stream, mode="r|", bufsize=1024**2) as archive:
                for member in archive:
                    name = validate_relative_path(member.name)
                    if member.isdir():
                        continue
                    if name in seen:
                        raise ValueError(f"duplicate TAR member path: {path}:{name}")
                    seen.add(name)
                    if member.islnk():
                        links[name] = validate_relative_path(member.linkname)
                        continue
                    if not member.isreg():
                        raise ValueError(f"non-regular archive content: {path}:{name}")
                    source = archive.extractfile(member)
                    key = expected.get(name)
                    if key is None and Path(name).name.startswith("._") and member.size <= 1024**2:
                        body = source.read()
                        actual[name] = (len(body), hashlib.sha256(body).hexdigest())
                        counterpart = _appledouble_counterpart(name, body)
                        if counterpart in expected:
                            sidecars[name] = counterpart
                    elif key is not None and key not in writer.locations:
                        writer.add(key, source)
                        actual[name] = key
                    else:
                        reader = _HashReader(source)
                        for _ in iter(lambda: reader.read(1024**2), b""):
                            pass
                        actual[name] = reader.value()
        _resolve_hardlinks(actual, links)
        # Old TARs may hardlink equal sidecars. Only proven AppleDouble bytes
        # paired with a manifest-bound original qualify for noise exclusion.
        sidecar_keys = {actual[name] for name in sidecars}
        for name in links:
            counterpart = Path(name).with_name(Path(name).name[2:]).as_posix() if Path(name).name.startswith("._") else None
            if name not in expected and counterpart in expected and actual.get(name) in sidecar_keys:
                sidecars[name] = counterpart
        if set(links) - set(actual):
            raise ValueError(f"unresolved archive hardlinks: {path}")
        if any(actual.get(name) != key for name, key in expected.items()):
            raise ValueError(f"source members differ from manifests: {path}")
        excluded = []
        for name, counterpart in sidecars.items():
            size, digest = actual.pop(name)
            excluded.append({"member": name, "counterpart": counterpart, "bytes": size, "sha256": digest})
        if excluded:
            progress("excluded_appledouble", source_object=path.name, excluded=excluded)
        # Never silently drop unindexed original material or hidden link targets.
        if any(key not in index.locations or key not in writer.locations for key in actual.values()):
            raise ValueError(f"unindexed source content requires reconciliation: {path}")
        progress("source_verified", source_index=number, source_objects=len(expected_by_object),
                 source_object=path.name, checked_members=len(actual))
    writer.finish()
    if set(writer.locations) != set(index.locations):
        raise ValueError("candidate does not cover every original file content")


def compact_recovery_plan(config, plan_path):
    """Authenticate existing progress after a known pre-switch noise rejection."""
    preflight(config)
    plan, root, work = _load_plan(config, plan_path)
    stopped = stage_status(plan_path.parent, "compact-apply")
    error = stopped.get("result", {}).get("error", "")
    if stopped["exit_code"] != 2 or not error.startswith("ValueError: unindexed source content requires reconciliation:"):
        raise ValueError("recovery requires the original known noise-rejection exit, not an unknown/other failure")
    _unchanged(root, plan["inventory"])
    backend = FilesystemArchiveBackend(root)
    if backend.ensure_ready() != plan["marker"] or (work / "retired").exists():
        raise ValueError("recovery is limited to the unchanged original pre-switch generation")
    candidate = work / "candidate"
    if any((candidate / "snapshots").iterdir()) or (work / "recovery-intent.json").exists():
        raise ValueError("recovery has already advanced; reconcile evidence only")
    fence = read_json(backend.maintenance_path)
    if fence["run_id"] != plan["run_id"] or fence["plan_digest"] != plan["plan_digest"]:
        raise ValueError("maintenance fence belongs to a different run")
    index = ArchiveContentIndex.load(backend)
    packs, fragments, contents = [], [], set()
    for path in sorted((candidate / "objects").iterdir()):
        signature = _signature(path)
        if path.name.startswith(".pack-") and path.suffix == ".pending":
            fragments.append({"path": path.name, "signature": signature, "sha256": sha256_file(path)})
            continue
        digest = path.name.removesuffix(".tar.zst")
        if path.name != f"{digest}.tar.zst" or backend.object_path(digest).name != path.name:
            raise ValueError("unexpected candidate file")
        actual = {}
        with _decoded(path, digest) as stream:
            with tarfile.open(fileobj=stream, mode="r|", bufsize=1024**2) as archive:
                for member in archive:
                    if not member.isreg() or member.name in actual:
                        raise ValueError("invalid reusable candidate member")
                    reader = _HashReader(archive.extractfile(member))
                    for _ in iter(lambda: reader.read(1024**2), b""):
                        pass
                    key = reader.value()
                    if member.name != f"content/{key[1]}" or key not in index.locations or key in contents:
                        raise ValueError("reusable candidate content differs from source index or repeats")
                    actual[member.name] = key
                    contents.add(key)
        if signature != _signature(path):
            raise ValueError("candidate changed while authenticating recovery")
        packs.append({"path": path.name, "signature": signature, "sha256": digest, "members": actual})
    journal = [json.loads(line) for line in (work / "journal.jsonl").read_text().splitlines()]
    completed = sorted({row["source_object"] for row in journal if row["phase"] == "source_verified"})
    recovery = {"status": "verified", "plan_digest": plan["plan_digest"], "original_exit_code": 2,
                "original_stdout_sha256": sha256_file(plan_path.parent / "compact-apply.stdout.json"),
                "fence": fence, "candidate_inventory": _inventory(candidate, hashes=False),
                "packs": packs, "fragments": fragments, "completed": completed, "reusable_contents": len(contents)}
    recovery["recovery_digest"] = payload_sha256(recovery)
    write_json(plan_path.parent / "recovery-plan.json", recovery)
    return {k: v for k, v in recovery.items() if k not in {"packs", "candidate_inventory"}}


def _publish_history(backend, candidate, writer):
    for directory in backend.iter_snapshot_dirs():
        old = backend.load_verified_metadata(directory)
        manifests = []
        for manifest in old.manifests:
            records, bundles = [], {}
            for record in manifest.files:
                bundle, member = writer.locations[(record.bytes, record.sha256)]
                bundles[bundle.bundle_id] = bundle
                records.append(replace(record, bundle_id=bundle.bundle_id, member=member))
            manifests.append(replace(manifest, files=tuple(records), bundles=tuple(bundles.values())))
        published = candidate.publish_snapshot(old.snapshot, tuple(manifests), {})
        write_catalog_segment(candidate, published)


def _verify_candidate(candidate, plan, progress):
    index = ArchiveContentIndex.load(candidate)
    if _logical_digest(candidate) != plan["history"]:
        raise ValueError("logical history changed during compaction")
    report = index.report()
    if (report["unique_file_contents"] != plan["content"]["unique_file_contents"]
            or report["unique_file_logical_bytes"] != plan["content"]["unique_file_logical_bytes"]
            or report["stored_member_locations"] != report["unique_file_contents"]):
        raise ValueError("candidate content identity/uniqueness mismatch")
    objects = {}
    for location in index.locations.values():
        objects.setdefault(location.bundle.bundle_id, (location.bundle, {}))[1][location.record.member] = (
            location.record.bytes, location.record.sha256)
    for number, (bundle, expected) in enumerate(objects.values(), 1):
        path = candidate.root / bundle.object_path
        actual = {}
        try:
            with _decoded(path, bundle.sha256) as stream:
                with tarfile.open(fileobj=stream, mode="r|", bufsize=1024**2) as archive:
                    for entry in archive:
                        if not entry.isreg() or entry.name in actual:
                            raise ValueError("unexpected/duplicate candidate member")
                        reader = _HashReader(archive.extractfile(entry))
                        for _ in iter(lambda: reader.read(1024**2), b""):
                            pass
                        actual[entry.name] = reader.value()
        except (OSError, ValueError, RuntimeError, tarfile.TarError) as exc:
            raise ValueError(f"candidate object/member mismatch: {path}") from exc
        if actual != expected:
            raise ValueError(f"candidate object/member mismatch: {path}")
        progress("verifying_candidate", checked_objects=number, total_objects=len(objects))
    return report


def _metadata_backup(root, work, inventory):
    expected = {name: (row["bytes"], row["sha256"]) for name, row in inventory["files"].items()
                if not name.startswith("objects/")}
    listing = work / "original-metadata.members"
    with listing.open("xb") as output:
        for name in sorted(expected):
            output.write(name.encode() + b"\0")
    package = work / "original-metadata.tar.zst"
    subprocess.run(["bsdtar", "--no-recursion", "--no-xattrs", "--no-mac-metadata", "--no-acls", "--no-fflags",
                    "--zstd", "-cf", str(package), "-C", str(root), "--null", "-T", str(listing)], check=True)
    if bundle_member_digests(package) != expected:
        raise ValueError("original metadata backup member verification failed")
    return {"path": str(package), "sha256": sha256_file(package), "bytes": package.stat().st_size,
            "files": len(expected)}


def _retire_exact(root, inventory, progress):
    from .cold_archive import _parent_fd
    _unchanged(root, inventory)
    removed = 0
    for name, row in sorted(inventory["files"].items()):
        descriptor, leaf = _parent_fd(root, name)
        try:
            if _signature(root / name) != row["signature"]:
                raise ValueError(f"retired source changed before unlink: {name}")
            progress("retiring", unlink_intent=name, removed_bytes=removed)
            os.unlink(leaf, dir_fd=descriptor)
            removed += row["bytes"]
        finally:
            os.close(descriptor)
    for name in sorted(inventory["directories"], key=lambda item: (-item.count("/"), item)):
        descriptor, leaf = _parent_fd(root, name)
        try:
            os.rmdir(leaf, dir_fd=descriptor)
        finally:
            os.close(descriptor)
    root.rmdir()
    return removed


def compact_apply(config: VaultConfig, plan_path: Path, *, recovery_path: Path | None = None) -> dict:
    preflight(config)
    plan, root, work = _load_plan(config, plan_path)
    backend = FilesystemArchiveBackend(root)
    recovery = None
    if recovery_path is not None:
        from .archive_maintenance import require_stage
        require_stage(plan_path.parent, "compact-recovery-plan", "verified")
        recovery = read_json(recovery_path)
        if (recovery_path != plan_path.parent / "recovery-plan.json"
                or recovery.get("recovery_digest") != payload_sha256({k: v for k, v in recovery.items() if k != "recovery_digest"})
                or recovery["plan_digest"] != plan["plan_digest"]
                or stage_status(plan_path.parent, "compact-apply")["exit_code"] != 2
                or sha256_file(plan_path.parent / "compact-apply.stdout.json") != recovery["original_stdout_sha256"]
                or read_json(backend.maintenance_path) != recovery["fence"]):
            raise ValueError("recovery identity, original exit or fence changed")
        _unchanged(work / "candidate", recovery["candidate_inventory"])
    else:
        backend.ensure_writable()
    if work.exists() and recovery is None:
        raise ValueError("compaction already has evidence; reconcile it, never reissue apply")
    lock_path = local_root(config) / "archive-cycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _unchanged(root, plan["inventory"])
        if backend.ensure_ready() != plan["marker"] or _logical_digest(backend) != plan["history"]:
            raise ValueError("source identity/history changed since plan")
        if shutil.disk_usage(root).free < plan["required_workspace_bytes"]:
            raise ValueError("NAS workspace is no longer sufficient")
        if recovery is None:
            work.mkdir(parents=True, exist_ok=False)
            write_json(work / "plan.json", plan)
        else:
            write_json(work / "recovery-intent.json", {"recovery_digest": recovery["recovery_digest"], "pid": os.getpid()})
        fence = {"operation": "archive-compaction", "run_id": plan["run_id"], "pid": os.getpid(),
                 "plan_path": str(plan_path), "work": str(work), "plan_digest": plan["plan_digest"]}
        write_json(backend.maintenance_path, fence, exclusive=recovery is None)

        def progress(phase, **details):
            payload = {"status": "stopped" if phase == "stopped" else "running", "phase": phase,
                       "at": utc_now(), "pid": os.getpid(), **details}
            write_json(work / "progress.json", payload, exclusive=False)
            with (work / "journal.jsonl").open("a") as journal:
                journal.write(json.dumps(payload, ensure_ascii=False) + "\n")
                journal.flush()
                os.fsync(journal.fileno())

        writer = None
        try:
            before_available = shutil.disk_usage(root).free
            candidate_path, retired = work / "candidate", work / "retired"
            marker = {**plan["marker"], "storage_generation": plan["run_id"]}
            if recovery is None:
                candidate_path.mkdir()
                for name in ("objects", "snapshots", "receipts", "staging", "catalog"):
                    (candidate_path / name).mkdir()
                write_json(candidate_path / "root-marker.json", marker)
            elif read_json(candidate_path / "root-marker.json") != marker:
                raise ValueError("recovery candidate generation changed")
            candidate = FilesystemArchiveBackend(candidate_path)
            index = ArchiveContentIndex.load(backend)
            writer = _PackWriter(candidate_path, plan["chunk_logical_bytes"], progress)
            fragments = []
            if recovery is not None:
                for pack in recovery["packs"]:
                    keys = [tuple(value) for value in pack["members"].values()]
                    bundle = ArchiveBundle(f"bundle-{pack['sha256']}", f"objects/{pack['path']}", pack["sha256"],
                                           pack["signature"][0], sum(key[0] for key in keys), len(keys))
                    writer.bundles[bundle.bundle_id] = bundle
                    for member, key in pack["members"].items():
                        writer.locations[tuple(key)] = (bundle, member)
                if recovery["fragments"]:
                    (work / "fragments").mkdir()
                for fragment in recovery["fragments"]:
                    source = candidate_path / "objects" / fragment["path"]
                    if _signature(source) != fragment["signature"] or sha256_file(source) != fragment["sha256"]:
                        raise ValueError("original fragment changed before custody move")
                    destination = work / "fragments" / source.name
                    source.rename(destination)
                    fragments.append({**fragment, "destination": str(destination), "signature": _signature(destination)})
                writer.sequence = len(recovery["packs"]) + len(fragments)
            _repack(backend, index, writer, progress, **({"completed": recovery["completed"]} if recovery else {}))
            _publish_history(backend, candidate, writer)
            # Preserve original receipts as provenance; regenerated catalog is logical-equivalent.
            for name, row in plan["inventory"]["files"].items():
                if name.startswith("receipts/"):
                    target = candidate_path / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(root / name, target)
                    if sha256_file(target) != row["sha256"]:
                        raise ValueError("receipt copy checksum mismatch")
            report = _verify_candidate(candidate, plan, progress)
            sample = min(index.locations, key=lambda key: (key[0] == 0, key[0], key[1]))
            bundle, member = writer.locations[sample]
            destination = plan_path.parent / "restore-sample" / sample[1]
            if restore_bundle_member(candidate_path / bundle.object_path, member, destination, expected=sample) != sample:
                raise ValueError("restore sample mismatch")
            if sha256_file(destination) != sample[1]:
                raise ValueError("restored sample changed")
            metadata = _metadata_backup(root, work, plan["inventory"])
            sample_name = next(name for name in plan["inventory"]["files"] if name == "root-marker.json")
            sample_key = (plan["inventory"]["files"][sample_name]["bytes"], plan["inventory"]["files"][sample_name]["sha256"])
            restore_bundle_member(Path(metadata["path"]), sample_name, plan_path.parent / "restore-metadata" / sample_name,
                                  expected=sample_key)
            _unchanged(root, plan["inventory"])
            new_bytes = sum(item.bytes for item in writer.bundles.values())
            excluded = [{"source_object": row["source_object"], **member}
                        for row in map(json.loads, (work / "journal.jsonl").read_text().splitlines())
                        if row["phase"] == "excluded_appledouble" for member in row["excluded"]]
            write_json(work / "excluded-appledouble.json", {"members": excluded, "count": len(excluded),
                       "logical_bytes": sum(row["bytes"] for row in excluded)})
            write_json(work / "verification.json", {"status": "verified", "history": plan["history"],
                       "content": report, "metadata_backup": metadata, "sample": {"bytes": sample[0], "sha256": sample[1]},
                       "new_object_bytes": new_bytes, "old_object_bytes": plan["old_object_bytes"]})
            if new_bytes + metadata["bytes"] >= plan["old_object_bytes"]:
                raise ValueError("candidate offers no measured storage saving; original retained")
            preflight(config)
            progress("switch_intent", candidate=str(candidate_path), retired=str(retired))
            root.rename(retired)
            try:
                candidate_path.rename(root)
            except BaseException:
                # One explicit rollback, only when both observed paths are unambiguous.
                if not root.exists() and retired.is_dir() and candidate_path.is_dir():
                    retired.rename(root)
                    progress("rolled_back", retired=str(retired))
                raise
            progress("switched")
            live_index = ArchiveContentIndex.load(backend)
            if (_logical_digest(backend) != plan["history"] or backend.ensure_ready() != marker
                    or live_index.report() != report):
                raise ValueError("post-switch archive identity/history/content mismatch")
            # Renames must preserve every verified new object, not replace its bytes.
            for bundle in writer.bundles.values():
                if (root / bundle.object_path).stat().st_size != bundle.bytes:
                    raise ValueError("post-switch object size mismatch")
            preflight(config)
            removed = _retire_exact(retired, plan["inventory"], progress)
            fragment_bytes = 0
            for fragment in fragments:
                path = Path(fragment["destination"])
                if _signature(path) != fragment["signature"] or sha256_file(path) != fragment["sha256"]:
                    raise ValueError("reconciled fragment changed before exact cleanup")
                progress("retiring_fragment", unlink_intent=str(path), bytes=fragment["signature"][0])
                path.unlink()
                fragment_bytes += fragment["signature"][0]
            if fragments:
                (work / "fragments").rmdir()
            result = {"status": "verified", "finished_at": utc_now(), "run_id": plan["run_id"],
                      "plan_digest": plan["plan_digest"], "root": str(root), "history": plan["history"],
                      "content": report, "old_object_bytes": plan["old_object_bytes"], "new_object_bytes": new_bytes,
                      "removed_old_container_bytes": removed, "metadata_backup": metadata,
                      "excluded_appledouble_members": len(excluded), "excluded_appledouble_logical_bytes": sum(row["bytes"] for row in excluded),
                      "reused_candidate_objects": len(recovery["packs"]) if recovery else 0,
                      "removed_failed_fragment_bytes": fragment_bytes,
                      "net_object_and_metadata_reduction_bytes": plan["old_object_bytes"] - new_bytes - metadata["bytes"],
                      "nas_available_before": before_available, "nas_available_after": shutil.disk_usage(root).free,
                      "restore_sample": str(destination), "receipt_path": str(work / "result.json")}
            write_json(work / "result.json", result)
            write_json(plan_path.parent / "result.json", result)
            if read_json(backend.maintenance_path) != fence:
                raise ValueError("maintenance fence ownership changed")
            backend.maintenance_path.unlink()
            return result
        except BaseException as exc:
            if writer is not None:
                writer.abort()
            progress("stopped", error=f"{type(exc).__name__}: {exc}")
            # Keep fence and all remaining data: status is read-only, never a retry.
            raise


def compact_status(config: VaultConfig, plan_path: Path) -> dict:
    plan, root, work = _load_plan(config, plan_path)
    result = {"status": "not_started", "run_id": plan["run_id"], "work": str(work),
              "root_exists": root.is_dir(), "candidate_exists": (work / "candidate").is_dir(),
              "retired_exists": (work / "retired").is_dir(),
              "maintenance_fence": FilesystemArchiveBackend(root).maintenance_path.exists()}
    result["runner"] = stage_status(plan_path.parent, "compact-apply")
    if (plan_path.parent / "compact-recover.launch.json").exists():
        result["original_runner"] = result["runner"]
        result["runner"] = stage_status(plan_path.parent, "compact-recover")
    for name in ("progress", "result"):
        if (work / f"{name}.json").is_file():
            result[name] = read_json(work / f"{name}.json")
            result["status"] = result[name]["status"]
    # This reports evidence only; real process exit comes from run-detached.py.
    return result

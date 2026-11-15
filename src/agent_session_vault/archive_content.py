"""Reconstruct the file-content index from authenticated immutable snapshots."""
from __future__ import annotations

from dataclasses import dataclass

from .archive_backend import ArchiveBackendError, FilesystemArchiveBackend, PublishedSnapshot
from .archive_model import ArchiveBundle, ArchiveFileRecord, validate_relative_path


@dataclass(frozen=True)
class ContentLocation:
    record: ArchiveFileRecord
    bundle: ArchiveBundle


class ArchiveContentIndex:
    """A per-operation index, never a second database or deletion authority.

    Paths/session IDs belong to manifests; raw file bytes own content identity.
    Object/member bytes are still deep-verified before a cycle permits pruning.
    """

    def __init__(self) -> None:
        self.locations: dict[tuple[int, str], ContentLocation] = {}
        self.latest: dict[tuple[str, str], PublishedSnapshot] = {}
        self.published_bundle_ids: set[str] = set()
        self.member_locations: dict[tuple[str, str], tuple[int, str]] = {}
        self.content_objects: dict[tuple[int, str], set[str]] = {}
        self.snapshots = 0

    @classmethod
    def load(cls, backend: FilesystemArchiveBackend) -> ArchiveContentIndex:
        index = cls()
        objects: dict[str, ArchiveBundle] = {}
        for directory in backend.iter_snapshot_dirs():
            published = backend.load_verified_metadata(directory)
            index.snapshots += 1
            for manifest in published.manifests:
                key = (manifest.source.machine_id, manifest.source.source_id)
                previous = index.latest.get(key)
                if previous is None or published.snapshot.captured_at > previous.snapshot.captured_at:
                    index.latest[key] = published
                bundles = {bundle.bundle_id: bundle for bundle in manifest.bundles}
                for bundle in manifest.bundles:
                    expected = f"objects/{bundle.sha256}.tar.zst"
                    if bundle.object_path != expected or bundle.bundle_id != f"bundle-{bundle.sha256}":
                        raise ArchiveBackendError(f"invalid content-addressed bundle: {bundle.bundle_id}")
                    existing = objects.get(bundle.bundle_id)
                    if existing is not None and existing != bundle:
                        raise ArchiveBackendError(f"inconsistent bundle metadata: {bundle.bundle_id}")
                    if existing is None:
                        path = backend.root / bundle.object_path
                        if path.is_symlink() or not path.is_file() or path.stat().st_size != bundle.bytes:
                            raise ArchiveBackendError(f"content index object missing or changed: {path}")
                        objects[bundle.bundle_id] = bundle
                    index.published_bundle_ids.add(bundle.bundle_id)
                for record in manifest.files:
                    bundle = bundles.get(record.bundle_id)
                    if bundle is None or not record.member:
                        raise ArchiveBackendError(f"content index record has no bundle/member: {record.path}")
                    validate_relative_path(record.member)
                    index.add(record, bundle)
        return index

    def add(self, record: ArchiveFileRecord, bundle: ArchiveBundle) -> None:
        if record.bundle_id != bundle.bundle_id or not record.member:
            raise ValueError("content location must name a real bundle member")
        key = (record.bytes, record.sha256)
        member_key = (bundle.bundle_id, record.member)
        existing = self.member_locations.get(member_key)
        if existing is not None and existing != key:
            raise ArchiveBackendError(f"conflicting member metadata: {member_key}")
        self.member_locations[member_key] = key
        self.content_objects.setdefault(key, set()).add(bundle.bundle_id)
        candidate = ContentLocation(record, bundle)
        previous = self.locations.get(key)
        # Prefer a smaller object when legacy history already has duplicates.
        # This reduces later restore/verification I/O without rewriting history.
        def cost(value):
            return value.bundle.bytes, value.bundle.sha256, value.record.member
        if previous is None or cost(candidate) < cost(previous):
            self.locations[key] = candidate

    def find(self, record: ArchiveFileRecord) -> ContentLocation | None:
        return self.locations.get((record.bytes, record.sha256))

    def report(self) -> dict[str, int]:
        return {"metadata_verified_snapshots": self.snapshots,
                "published_objects": len(self.published_bundle_ids),
                "unique_file_contents": len(self.locations),
                "unique_file_logical_bytes": sum(size for size, _ in self.locations),
                "stored_member_locations": len(self.member_locations),
                "stored_member_logical_bytes": sum(size for size, _ in self.member_locations.values()),
                "cross_object_duplicate_contents": sum(len(objects) > 1 for objects in self.content_objects.values()),
                "cross_object_redundant_logical_bytes": sum((len(objects) - 1) * size
                                                            for (size, _), objects in self.content_objects.items())}

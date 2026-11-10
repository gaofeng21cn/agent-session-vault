"""Read-only projections over Vault plans, coverage and owner receipts."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

from .archive import sha256_file
from .archive_model import RECEIPT_SCHEMA_VERSION, payload_sha256, utc_now
from .archive_ops import archive_backend
from .archive_prune import PRUNE_PLAN_SCHEMA, _stable_imports_fingerprint, _verify_entry, load_prune_plan
from .archive_restore import load_restore_plan
from .config import VaultConfig, load_config
from .stable import migration_plan_payload

REF = "agent.session.vault.v1#review"
STATUS_REF = "agent.session.vault.v1#status"
REQUEST_SCHEMA = "opl-package-app-contribution-request.v1"
RESPONSE_SCHEMA = "opl-package-app-contribution-response.v1"
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "config_path": {"type": "string", "title": "Vault configuration"},
        "plan_path": {"type": "string", "title": "Exact restore or prune plan"},
        "receipt_path": {"type": "string", "title": "Exact archive receipt"},
        "expected_revision": {"type": "string", "title": "Expected plan digest"},
    },
    "additionalProperties": False,
}


def runtime_status() -> dict[str, object]:
    """Observe local prerequisites without opening user data or running tools."""
    python_ready = sys.version_info >= (3, 12)
    fleet_present = shutil.which("opl-fleet") is not None
    npx_present = shutil.which("npx") is not None
    return {
        "state": "ready" if python_ready and fleet_present and npx_present else "attention",
        "data": {
            "read_only": True, "observed_at": utc_now(),
            "runtime_ready": python_ready,
            "business_configuration": "not_checked",
            "summary": "Local prerequisites only; node access, archive coverage, and submission require their own checks.",
            "items": [
                {"id": "python", "title": "Python runtime", "status": "ready" if python_ready else "missing",
                 "summary": f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}; requires 3.12 or later."},
                {"id": "fleet", "title": "Fleet synchronization", "status": "ready" if python_ready and fleet_present else "missing",
                 "summary": "OPL Fleet command found; node access is not checked." if fleet_present else "Install OPL Fleet to synchronize approved nodes."},
                {"id": "tokscale", "title": "Usage analytics", "status": "ready" if python_ready and npx_present else "missing",
                 "summary": "npx found; Tokscale version and service access are checked at invocation." if npx_present else "Install Node.js with npx to run Tokscale analytics."},
            ],
        },
    }


def _read_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


def _absolute_path(value: object, name: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return path.resolve()


def _receipt_item(path: Path, plan: dict[str, object] | None) -> dict[str, object]:
    payload = _read_object(path)
    if payload.get("schema_version") != RECEIPT_SCHEMA_VERSION or not all(
        isinstance(payload.get(key), str) and payload[key]
        for key in ("operation_id", "operation", "status", "machine_id")
    ) or not isinstance(payload.get("details"), dict):
        raise ValueError("invalid archive owner receipt")
    details = payload["details"]
    if plan is not None and (
        details.get("plan_id") != plan["plan_id"]
        or details.get("plan_digest") != plan["plan_digest"]
        or (plan.get("machine_id") is not None and payload.get("machine_id") != plan["machine_id"])
    ):
        raise ValueError("receipt does not belong to the selected plan revision and machine")
    return {
        "id": payload["operation_id"], "title": payload["operation"],
        "revision": sha256_file(path), "status": payload["status"],
        "summary": "Reconcile the recorded outcome and current sources before any new operation."
        if payload["status"] in ("partial", "failed", "unknown") else "Recorded owner outcome",
        "source_refs": [str(path)], "receipt": payload,
        "retry_available": False,
    }


def review_archive(
    config: VaultConfig, *, plan_path: Path | None = None,
    receipt_path: Path | None = None, expected_revision: str | None = None,
) -> dict[str, object]:
    """Inspect existing objects. This never builds a plan or calls an apply path."""
    coverage = migration_plan_payload(config)
    items: list[dict[str, object]] = [{
        "id": "stable-coverage", "title": "Stable analytics coverage",
        "revision": payload_sha256(coverage),
        "status": "verified" if not coverage["readiness"]["blockers"] else "unavailable",
        "summary": "Current coverage is read from the existing stable owner.",
        "source_refs": [coverage["current_mirror"]["manifest_path"]],
        "coverage": coverage,
    }]
    plan: dict[str, object] | None = None
    if expected_revision and plan_path is None:
        raise ValueError("expected_revision requires plan_path")
    if plan_path is not None:
        raw = _read_object(plan_path)
        if raw.get("schema_version") == PRUNE_PLAN_SCHEMA:
            loaded = load_prune_plan(plan_path)
            plan = loaded.to_payload()
            if expected_revision is not None and expected_revision != loaded.plan_digest:
                raise ValueError("selected plan revision changed")
            checks: dict[str, object] = {"plan_digest": "verified", "tokscale_preview": "rechecked_by_apply"}
            try:
                root, fingerprint = _stable_imports_fingerprint(config)
                checks["stable_coverage"] = "current" if str(root) == loaded.stable_root and fingerprint == loaded.imports_fingerprint else "changed"
            except (OSError, ValueError) as exc:
                checks["stable_coverage"] = "unavailable"
                checks["stable_reason"] = str(exc)
            backend = archive_backend(config)
            snapshots = {path.name: path for path in backend.iter_snapshot_dirs()}
            checks["snapshots"] = []
            for snapshot_id in sorted({entry.snapshot_id for entry in loaded.entries}):
                snapshot_dir = snapshots.get(snapshot_id)
                try:
                    check = backend.verify_snapshot(snapshot_dir, deep=False) if snapshot_dir else {"status": "missing", "snapshot_id": snapshot_id}
                except (OSError, ValueError, KeyError) as exc:
                    check = {"status": "unavailable", "snapshot_id": snapshot_id, "reason": str(exc)}
                checks["snapshots"].append(check)
            source_items = []
            for entry in loaded.entries:
                try:
                    _verify_entry(entry)
                    status, reason = "current", None
                except (OSError, ValueError) as exc:
                    status, reason = "changed_or_missing", str(exc)
                source_items.append({
                    "id": entry.source_path, "title": Path(entry.source_path).name,
                    "revision": entry.sha256, "status": status,
                    "source_refs": [ref for ref in (entry.source_path, entry.projection_path) if ref],
                    "snapshot_id": entry.snapshot_id, "archive_path": entry.archive_path,
                    "bytes": entry.bytes, "reason": reason,
                })
            current = checks["stable_coverage"] == "current" and all(
                item["status"] == "current" for item in source_items
            ) and all(item["status"] == "verified" for item in checks["snapshots"])
            items.append({
                "id": loaded.plan_id, "title": "Prune plan", "revision": loaded.plan_digest,
                "status": "reviewable" if current else "stale",
                "summary": f"{len(loaded.entries)} files; {sum(entry.bytes for entry in loaded.entries)} bytes. Read-only review; apply rechecks all prerequisites.",
                "source_refs": [str(plan_path)], "plan": plan, "checks": checks,
                "sources": source_items, "execution_available": False,
            })
        elif raw.get("schema_version") == "archive.restore-plan.v1":
            loaded_restore = load_restore_plan(plan_path)
            plan = loaded_restore.to_payload()
            if expected_revision is not None and expected_revision != loaded_restore.plan_digest:
                raise ValueError("selected plan revision changed")
            items.append({
                "id": loaded_restore.plan_id, "title": "Staging restore plan",
                "revision": loaded_restore.plan_digest, "status": "reviewable",
                "summary": "Restore stays in the exact staging destination; collision checks run at apply.",
                "source_refs": [str(plan_path)], "plan": plan, "execution_available": False,
            })
        else:
            raise ValueError("unsupported archive plan schema")
    if receipt_path is not None:
        items.append(_receipt_item(receipt_path, plan))
    elif plan is not None and plan.get("schema_version") == PRUNE_PLAN_SCHEMA:
        # Read receipts from the owner store, never infer deletion from an absent file.
        for path in sorted((archive_backend(config).root / "receipts").glob("*/*.json")):
            raw = _read_object(path)
            details = raw.get("details", {})
            if isinstance(details, dict) and details.get("plan_id") == plan["plan_id"] and details.get("plan_digest") == plan["plan_digest"]:
                items.append(_receipt_item(path, plan))
    return {"state": "ready", "data": {"items": items, "observed_at": utc_now(), "read_only": True}}


def read_contribution(request: object) -> dict[str, object]:
    if not isinstance(request, dict) or request.get("schema_version") != REQUEST_SCHEMA or request.get("operation") != "read" or request.get("ref") not in (REF, STATUS_REF):
        raise ValueError("unsupported app contribution request")
    if request["ref"] == STATUS_REF:
        if request.get("input", {}) != {}:
            raise ValueError("runtime status accepts no input")
        return {"schema_version": RESPONSE_SCHEMA, "ok": True, "ref": STATUS_REF,
                "operation": "read", "result": runtime_status()}
    input_value = request.get("input", {})
    if not isinstance(input_value, dict) or set(input_value) - set(INPUT_SCHEMA["properties"]):
        raise ValueError("unsupported contribution input")
    revision = input_value.get("expected_revision")
    if revision is not None and (not isinstance(revision, str) or not revision):
        raise ValueError("expected_revision must be a non-empty digest")
    result = review_archive(
        load_config(_absolute_path(input_value.get("config_path"), "config_path")),
        plan_path=_absolute_path(input_value.get("plan_path"), "plan_path"),
        receipt_path=_absolute_path(input_value.get("receipt_path"), "receipt_path"),
        expected_revision=revision,
    )
    return {"schema_version": RESPONSE_SCHEMA, "ok": True, "ref": REF, "operation": "read", "result": result}


def main() -> int:
    request: object = None
    try:
        request = json.load(sys.stdin)
        response = read_contribution(request)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        ref = STATUS_REF if isinstance(request, dict) and request.get("ref") == STATUS_REF else REF
        response = {"schema_version": RESPONSE_SCHEMA, "ok": False, "ref": ref, "operation": "read", "error": {"code": "review_unavailable", "message": str(exc)}}
    json.dump(response, sys.stdout, ensure_ascii=False)
    return 0 if response["ok"] else 2

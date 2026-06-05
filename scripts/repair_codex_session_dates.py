#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil


REPAIR_MANIFEST_NAME = "repair-manifest.json"


@dataclass(frozen=True)
class RepairResult:
    codex_root: Path
    manifest_path: Path
    files_copied: int
    files_repaired: int
    source_last_tokens_total: int
    repaired_last_tokens_total: int


@dataclass(frozen=True)
class ApplyResult:
    manifest_path: Path
    backup_root: Path | None
    report_path: Path | None
    files_checked: int
    files_repaired: int
    files_already_repaired: int
    source_last_tokens_total: int
    repaired_last_tokens_total: int
    dry_run: bool


@dataclass(frozen=True)
class _SessionFile:
    source_root: Path
    root_key: str
    path: Path
    relative_path: Path
    source_date: str | None
    last_tokens: int
    sha256: str


def _json_load(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _date_range(start: str, end: str) -> list[str]:
    current = _parse_day(start)
    stop = _parse_day(end)
    if current > stop:
        raise ValueError("target_start must be on or before target_end")
    days: list[str] = []
    while current <= stop:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _path_source_date(relative_path: Path) -> str | None:
    parts = relative_path.parts
    for index in range(len(parts) - 2):
        year, month, day = parts[index : index + 3]
        if len(year) == 4 and len(month) == 2 and len(day) == 2:
            candidate = f"{year}-{month}-{day}"
            try:
                _parse_day(candidate)
            except ValueError:
                continue
            return candidate
    return None


def _path_with_date(relative_path: Path, target_date: str) -> Path:
    year, month, day = target_date.split("-")
    parts = list(relative_path.parts)
    for index in range(len(parts) - 2):
        if f"{parts[index]}-{parts[index + 1]}-{parts[index + 2]}" == _path_source_date(Path(*parts[index:])):
            parts[index : index + 3] = [year, month, day]
            return Path(*parts)
    raise ValueError(f"relative path has no date components: {relative_path}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _last_token_total(path: Path) -> int:
    total = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            obj = _json_load(raw)
            if obj is None or obj.get("type") != "event_msg":
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            usage = info.get("last_token_usage")
            if not isinstance(usage, dict):
                continue
            value = usage.get("total_tokens")
            if isinstance(value, int):
                total += value
    return total


def _timestamp_days(path: Path) -> set[str]:
    days: set[str] = set()
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            obj = _json_load(raw)
            if obj is None:
                continue
            timestamp = obj.get("timestamp")
            if isinstance(timestamp, str) and len(timestamp) >= 10:
                days.add(timestamp[:10])
            payload = obj.get("payload")
            if isinstance(payload, dict):
                payload_timestamp = payload.get("timestamp")
                if isinstance(payload_timestamp, str) and len(payload_timestamp) >= 10:
                    days.add(payload_timestamp[:10])
    return days


def _is_already_repaired(path: Path, target_date: str, repaired_last_tokens: int) -> bool:
    if _last_token_total(path) != repaired_last_tokens:
        return False
    days = _timestamp_days(path)
    return bool(days) and days <= {target_date}


def _root_key(source_root: Path) -> str:
    digest = hashlib.sha1(str(source_root).encode("utf-8")).hexdigest()[:8]
    name = "-".join(part for part in source_root.parts[-3:] if part not in {"/", ""})
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in name).strip("-").lower()
    return f"{cleaned or 'root'}-{digest}"


def _output_relative_path(item: _SessionFile) -> Path:
    return Path("sessions") / item.root_key / item.relative_path


def _iter_session_files(source_root: Path) -> list[_SessionFile]:
    files: list[_SessionFile] = []
    key = _root_key(source_root)
    for path in sorted(source_root.rglob("*.jsonl")):
        rel = path.relative_to(source_root)
        files.append(
            _SessionFile(
                source_root=source_root,
                root_key=key,
                path=path,
                relative_path=rel,
                source_date=_path_source_date(rel),
                last_tokens=_last_token_total(path),
                sha256=_sha256_file(path),
            )
        )
    return files


def _assign_targets(files: list[_SessionFile], target_dates: list[str]) -> dict[Path, str]:
    loads = {day: 0 for day in target_dates}
    assignments: dict[Path, str] = {}
    for item in sorted(files, key=lambda current: (-current.last_tokens, str(current.relative_path))):
        target = min(target_dates, key=lambda day: (loads[day], day))
        assignments[item.path] = target
        loads[target] += item.last_tokens
    return assignments


def _rewrite_timestamps(source_path: Path, dest_path: Path, target_date: str) -> int:
    written = 0
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("r", encoding="utf-8", errors="replace") as src, dest_path.open("w", encoding="utf-8") as dst:
        for raw in src:
            obj = _json_load(raw)
            if obj is None:
                dst.write(raw)
                continue
            timestamp = obj.get("timestamp")
            if isinstance(timestamp, str) and len(timestamp) >= 10:
                obj["timestamp"] = target_date + timestamp[10:]
            payload = obj.get("payload")
            if isinstance(payload, dict):
                payload_timestamp = payload.get("timestamp")
                if isinstance(payload_timestamp, str) and len(payload_timestamp) >= 10:
                    payload["timestamp"] = target_date + payload_timestamp[10:]
            dst.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
            dst.write("\n")
            written += 1
    return written


def _rewrite_timestamps_in_place(source_path: Path, target_date: str) -> int:
    temp_path = source_path.with_name(source_path.name + ".repairing")
    if temp_path.exists():
        raise FileExistsError(f"temporary repair file already exists: {temp_path}")
    try:
        written = _rewrite_timestamps(source_path, temp_path, target_date)
        temp_path.replace(source_path)
        return written
    finally:
        if temp_path.exists():
            temp_path.unlink()


def build_repaired_view(
    *,
    source_root: Path | None = None,
    source_roots: list[Path] | None = None,
    output_root: Path,
    source_dates: list[str],
    target_start: str,
    target_end: str,
    exclude_target_dates: list[str] | None = None,
    namespace: str = "codex-date-repair",
) -> RepairResult:
    resolved_source_roots = [path.expanduser().resolve() for path in (source_roots or [])]
    if source_root is not None:
        resolved_source_roots.insert(0, source_root.expanduser().resolve())
    if not resolved_source_roots:
        raise ValueError("at least one source root is required")
    output_root = output_root.expanduser().resolve()
    codex_root = output_root / "codex"
    excluded_targets = set(exclude_target_dates or [])
    target_dates = [day for day in _date_range(target_start, target_end) if day not in excluded_targets]
    if not target_dates:
        raise ValueError("target date range is empty after applying excluded target dates")
    source_date_set = set(source_dates)

    if output_root.exists():
        shutil.rmtree(output_root)
    codex_root.mkdir(parents=True, exist_ok=True)

    files: list[_SessionFile] = []
    for current_root in resolved_source_roots:
        files.extend(_iter_session_files(current_root))
    repair_files = [item for item in files if item.source_date in source_date_set]
    keep_files = [item for item in files if item.source_date not in source_date_set]
    assignments = _assign_targets(repair_files, target_dates)

    manifest_items: list[dict[str, object]] = []
    for item in keep_files:
        dest = codex_root / _output_relative_path(item)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.path, dest)

    source_total = 0
    repaired_total = 0
    for item in repair_files:
        target_date = assignments[item.path]
        dest_rel = _path_with_date(item.relative_path, target_date)
        output_rel = _output_relative_path(
            _SessionFile(
                source_root=item.source_root,
                root_key=item.root_key,
                path=item.path,
                relative_path=dest_rel,
                source_date=item.source_date,
                last_tokens=item.last_tokens,
                sha256=item.sha256,
            )
        )
        dest = codex_root / output_rel
        _rewrite_timestamps(item.path, dest, target_date)
        repaired_tokens = _last_token_total(dest)
        source_total += item.last_tokens
        repaired_total += repaired_tokens
        manifest_items.append(
            {
                "source_path": str(item.path),
                "source_root": str(item.source_root),
                "root_key": item.root_key,
                "source_relative_path": str(item.relative_path),
                "source_date": item.source_date,
                "source_sha256": item.sha256,
                "target_relative_path": str(output_rel),
                "target_date": target_date,
                "last_tokens": item.last_tokens,
                "repaired_last_tokens": repaired_tokens,
            }
        )

    manifest = {
        "version": 1,
        "namespace": namespace,
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_roots": [str(path) for path in resolved_source_roots],
        "codex_root": str(codex_root),
        "source_dates": sorted(source_date_set),
        "target_start": target_start,
        "target_end": target_end,
        "excluded_target_dates": sorted(excluded_targets),
        "files_copied": len(keep_files),
        "files_repaired": len(repair_files),
        "source_last_tokens_total": source_total,
        "repaired_last_tokens_total": repaired_total,
        "items": manifest_items,
    }
    manifest_path = output_root / REPAIR_MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return RepairResult(
        codex_root=codex_root,
        manifest_path=manifest_path,
        files_copied=len(keep_files),
        files_repaired=len(repair_files),
        source_last_tokens_total=source_total,
        repaired_last_tokens_total=repaired_total,
    )


def apply_repair_manifest(
    *,
    manifest_path: Path,
    backup_root: Path | None = None,
    report_path: Path | None = None,
    dry_run: bool = False,
) -> ApplyResult:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest.get("items")
    if not isinstance(items, list):
        raise ValueError(f"repair manifest has no items list: {manifest_path}")

    if backup_root is None:
        generated_at = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
        backup_root = manifest_path.parent / "inplace-backups" / generated_at
    backup_root = backup_root.expanduser().resolve()
    if report_path is None:
        report_path = backup_root / "apply-report.json"
    report_path = report_path.expanduser().resolve()

    to_repair: list[dict[str, object]] = []
    already_repaired: list[dict[str, object]] = []
    errors: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"item {index} is not an object")
            continue
        source_path_raw = item.get("source_path")
        source_sha256 = item.get("source_sha256")
        target_date = item.get("target_date")
        repaired_last_tokens = item.get("repaired_last_tokens")
        if not isinstance(source_path_raw, str) or not isinstance(source_sha256, str):
            errors.append(f"item {index} is missing source path or sha")
            continue
        if not isinstance(target_date, str) or not isinstance(repaired_last_tokens, int):
            errors.append(f"item {index} is missing target date or repaired token total")
            continue
        source_path = Path(source_path_raw)
        if not source_path.is_file():
            errors.append(f"source file does not exist: {source_path}")
            continue
        current_sha = _sha256_file(source_path)
        if current_sha == source_sha256:
            to_repair.append(item)
            continue
        if _is_already_repaired(source_path, target_date, repaired_last_tokens):
            already_repaired.append(item)
            continue
        errors.append(f"source sha mismatch: {source_path}")

    if errors:
        raise ValueError("repair manifest preflight failed:\n" + "\n".join(errors[:20]))

    source_total = 0
    repaired_total = 0
    report_items: list[dict[str, object]] = []
    for item in to_repair:
        source_path = Path(str(item["source_path"]))
        root_key = str(item["root_key"])
        source_relative_path = Path(str(item["source_relative_path"]))
        target_date = str(item["target_date"])
        backup_path = backup_root / root_key / source_relative_path
        source_tokens = int(item["last_tokens"])
        expected_repaired_tokens = int(item["repaired_last_tokens"])
        source_total += source_tokens

        if dry_run:
            repaired_total += expected_repaired_tokens
            report_items.append(
                {
                    "source_path": str(source_path),
                    "backup_path": str(backup_path),
                    "target_date": target_date,
                    "status": "would_repair",
                    "last_tokens": source_tokens,
                    "repaired_last_tokens": expected_repaired_tokens,
                }
            )
            continue

        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_path.exists():
            raise FileExistsError(f"backup path already exists: {backup_path}")
        shutil.copy2(source_path, backup_path)
        _rewrite_timestamps_in_place(source_path, target_date)
        repaired_tokens = _last_token_total(source_path)
        if repaired_tokens != expected_repaired_tokens:
            shutil.copy2(backup_path, source_path)
            raise ValueError(f"repaired token total mismatch for {source_path}: {repaired_tokens}")
        repaired_total += repaired_tokens
        report_items.append(
            {
                "source_path": str(source_path),
                "backup_path": str(backup_path),
                "target_date": target_date,
                "status": "repaired",
                "last_tokens": source_tokens,
                "repaired_last_tokens": repaired_tokens,
                "source_sha256": item["source_sha256"],
                "repaired_sha256": _sha256_file(source_path),
            }
        )

    for item in already_repaired:
        source_total += int(item["last_tokens"])
        repaired_total += int(item["repaired_last_tokens"])
        report_items.append(
            {
                "source_path": str(item["source_path"]),
                "target_date": item["target_date"],
                "status": "already_repaired",
                "last_tokens": item["last_tokens"],
                "repaired_last_tokens": item["repaired_last_tokens"],
            }
        )

    if not dry_run:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "version": 1,
            "manifest_path": str(manifest_path),
            "backup_root": str(backup_root),
            "generated_at": datetime.now().astimezone().isoformat(),
            "dry_run": dry_run,
            "files_checked": len(items),
            "files_repaired": len(to_repair),
            "files_already_repaired": len(already_repaired),
            "source_last_tokens_total": source_total,
            "repaired_last_tokens_total": repaired_total,
            "items": report_items,
        }
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return ApplyResult(
        manifest_path=manifest_path,
        backup_root=backup_root if not dry_run else None,
        report_path=report_path if not dry_run else None,
        files_checked=len(items),
        files_repaired=len(to_repair),
        files_already_repaired=len(already_repaired),
        source_last_tokens_total=source_total,
        repaired_last_tokens_total=repaired_total,
        dry_run=dry_run,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or apply an auditable Codex date-repair Tokscale view")
    subparsers = parser.add_subparsers(dest="command")

    build = subparsers.add_parser("build", help="Build an auditable derived Codex repair view")
    build.add_argument("--source-root", action="append", type=Path, required=True)
    build.add_argument("--output-root", type=Path, required=True)
    build.add_argument("--source-date", action="append", required=True)
    build.add_argument("--target-start", required=True)
    build.add_argument("--target-end", required=True)
    build.add_argument("--exclude-target-date", action="append", default=[])
    build.add_argument("--namespace", default="codex-date-repair")
    build.add_argument("--json", action="store_true")

    apply = subparsers.add_parser("apply", help="Apply an existing repair manifest in place after sha preflight")
    apply.add_argument("--manifest", type=Path, required=True)
    apply.add_argument("--backup-root", type=Path, default=None)
    apply.add_argument("--report-path", type=Path, default=None)
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "build"
    if command == "apply":
        result = apply_repair_manifest(
            manifest_path=args.manifest,
            backup_root=args.backup_root,
            report_path=args.report_path,
            dry_run=args.dry_run,
        )
        payload = {
            "manifest_path": str(result.manifest_path),
            "backup_root": str(result.backup_root) if result.backup_root else None,
            "report_path": str(result.report_path) if result.report_path else None,
            "files_checked": result.files_checked,
            "files_repaired": result.files_repaired,
            "files_already_repaired": result.files_already_repaired,
            "source_last_tokens_total": result.source_last_tokens_total,
            "repaired_last_tokens_total": result.repaired_last_tokens_total,
            "dry_run": result.dry_run,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(payload)
        return 0

    result = build_repaired_view(
        source_roots=args.source_root,
        output_root=args.output_root,
        source_dates=args.source_date,
        target_start=args.target_start,
        target_end=args.target_end,
        exclude_target_dates=args.exclude_target_date,
        namespace=args.namespace,
    )
    payload = {
        "codex_root": str(result.codex_root),
        "manifest_path": str(result.manifest_path),
        "files_copied": result.files_copied,
        "files_repaired": result.files_repaired,
        "source_last_tokens_total": result.source_last_tokens_total,
        "repaired_last_tokens_total": result.repaired_last_tokens_total,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

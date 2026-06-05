from __future__ import annotations

import json
from pathlib import Path

from scripts.repair_codex_session_dates import apply_repair_manifest, build_repaired_view


def _write_session(path: Path, session_id: str, timestamp: str, total_tokens: int, last_tokens: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": timestamp,
                "model_provider": "gflab",
            },
        },
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"total_tokens": total_tokens},
                    "last_token_usage": {"total_tokens": last_tokens},
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n", encoding="utf-8")


def test_build_repaired_view_moves_bad_day_files_with_provenance(tmp_path: Path) -> None:
    source = tmp_path / "source" / "codex"
    first = source / "sessions" / "root-a" / "2026" / "04" / "05" / "first.jsonl"
    second = source / "sessions" / "root-a" / "2026" / "04" / "05" / "second.jsonl"
    _write_session(first, "first", "2026-04-05T10:00:00.000Z", total_tokens=100, last_tokens=100)
    _write_session(second, "second", "2026-04-05T11:00:00.000Z", total_tokens=200, last_tokens=200)

    result = build_repaired_view(
        source_root=source,
        output_root=tmp_path / "repair",
        source_dates=["2026-04-05"],
        target_start="2026-03-01",
        target_end="2026-03-02",
        exclude_target_dates=[],
        namespace="test-repair",
    )

    assert result.files_repaired == 2
    assert result.source_last_tokens_total == 300
    assert result.repaired_last_tokens_total == 300
    assert first.read_text(encoding="utf-8").splitlines()[0].startswith('{"timestamp":"2026-04-05')

    manifest = json.loads((tmp_path / "repair" / "repair-manifest.json").read_text(encoding="utf-8"))
    assert manifest["namespace"] == "test-repair"
    assert manifest["source_dates"] == ["2026-04-05"]
    assert manifest["source_last_tokens_total"] == 300
    assert manifest["repaired_last_tokens_total"] == 300

    repaired_files = sorted((tmp_path / "repair" / "codex").rglob("*.jsonl"))
    assert [path.parts[-4:-1] for path in repaired_files] == [("2026", "03", "01"), ("2026", "03", "02")]
    for repaired in repaired_files:
        rows = [json.loads(line) for line in repaired.read_text(encoding="utf-8").splitlines()]
        assert rows[0]["timestamp"].startswith("2026-03-")
        assert rows[0]["payload"]["timestamp"].startswith("2026-03-")


def test_build_repaired_view_can_exclude_bad_dates_as_targets(tmp_path: Path) -> None:
    source = tmp_path / "source" / "codex"
    first = source / "sessions" / "root-a" / "2026" / "04" / "05" / "first.jsonl"
    second = source / "sessions" / "root-a" / "2026" / "04" / "06" / "second.jsonl"
    _write_session(first, "first", "2026-04-05T10:00:00.000Z", total_tokens=100, last_tokens=100)
    _write_session(second, "second", "2026-04-06T10:00:00.000Z", total_tokens=100, last_tokens=100)

    result = build_repaired_view(
        source_root=source,
        output_root=tmp_path / "repair",
        source_dates=["2026-04-05", "2026-04-06"],
        target_start="2026-04-05",
        target_end="2026-04-07",
        exclude_target_dates=["2026-04-05", "2026-04-06"],
        namespace="test-repair",
    )

    assert result.files_repaired == 2
    repaired_files = sorted((tmp_path / "repair" / "codex").rglob("*.jsonl"))
    assert repaired_files
    assert {path.parts[-4:-1] for path in repaired_files} == {("2026", "04", "07")}


def test_build_repaired_view_combines_multiple_roots_without_collisions(tmp_path: Path) -> None:
    first_root = tmp_path / "first" / "codex"
    second_root = tmp_path / "second" / "codex"
    _write_session(
        first_root / "sessions" / "root-a" / "2026" / "04" / "05" / "same.jsonl",
        "first",
        "2026-04-05T10:00:00.000Z",
        total_tokens=100,
        last_tokens=100,
    )
    _write_session(
        second_root / "sessions" / "root-a" / "2026" / "04" / "05" / "same.jsonl",
        "second",
        "2026-04-05T11:00:00.000Z",
        total_tokens=200,
        last_tokens=200,
    )

    result = build_repaired_view(
        source_roots=[first_root, second_root],
        output_root=tmp_path / "repair",
        source_dates=["2026-04-05"],
        target_start="2026-03-01",
        target_end="2026-03-02",
        exclude_target_dates=[],
        namespace="test-repair",
    )

    assert result.files_repaired == 2
    assert result.source_last_tokens_total == 300
    repaired_files = sorted((tmp_path / "repair" / "codex").rglob("*.jsonl"))
    assert len(repaired_files) == 2
    assert len({path.relative_to(tmp_path / "repair" / "codex") for path in repaired_files}) == 2


def test_apply_repair_manifest_rewrites_sources_with_backup_and_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source" / "codex"
    session = source / "sessions" / "root-a" / "2026" / "04" / "05" / "first.jsonl"
    _write_session(session, "first", "2026-04-05T10:00:00.000Z", total_tokens=100, last_tokens=100)
    build_repaired_view(
        source_root=source,
        output_root=tmp_path / "repair",
        source_dates=["2026-04-05"],
        target_start="2026-03-01",
        target_end="2026-03-01",
        exclude_target_dates=[],
        namespace="test-repair",
    )
    manifest = tmp_path / "repair" / "repair-manifest.json"

    result = apply_repair_manifest(manifest_path=manifest, backup_root=tmp_path / "backups")

    assert result.files_checked == 1
    assert result.files_repaired == 1
    assert result.files_already_repaired == 0
    assert result.source_last_tokens_total == 100
    assert result.repaired_last_tokens_total == 100
    rows = [json.loads(line) for line in session.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["timestamp"].startswith("2026-03-01")
    assert rows[0]["payload"]["timestamp"].startswith("2026-03-01")
    assert list((tmp_path / "backups").rglob("*.jsonl"))

    second = apply_repair_manifest(manifest_path=manifest, backup_root=tmp_path / "second-backups", dry_run=True)

    assert second.files_checked == 1
    assert second.files_repaired == 0
    assert second.files_already_repaired == 1

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agent-session-vault" / "config.toml"
TOP_LEVEL_KEYS = {"paths", "archive"}
PATH_KEYS = {
    "home",
    "workspace_root",
    "import_root",
    "projection_home",
    "local_workspace_extras",
    "stable_root",
}
ARCHIVE_KEYS = {
    "root",
    "cadence_days",
    "cold_age_days",
    "staging_root",
    "machine_id_path",
    "source_paths",
    "require_quiescent_for_prune",
}
ARCHIVE_SOURCE_KEYS = {"path", "kind", "label"}


@dataclass(frozen=True)
class PathsConfig:
    home: Path
    workspace_root: Path
    import_root: Path
    projection_home: Path
    local_workspace_extras: Path
    stable_root: Path


@dataclass(frozen=True)
class ArchiveSourceConfig:
    path: str
    kind: str = "codex_home"
    label: str | None = None


@dataclass(frozen=True)
class ArchiveConfig:
    root: Path
    cadence_days: int
    cold_age_days: int
    staging_root: Path
    machine_id_path: Path
    source_paths: tuple[ArchiveSourceConfig, ...]
    require_quiescent_for_prune: bool


@dataclass(frozen=True)
class VaultConfig:
    config_path: Path
    paths: PathsConfig
    archive: ArchiveConfig


def _reject_unknown_keys(raw: dict[str, object], allowed: set[str], scope: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unsupported {scope} fields: {', '.join(unknown)}")


def _table_value(raw: dict[str, object], name: str) -> dict[str, object]:
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a table")
    return value


def _path_value(raw: object, fallback: Path, field: str) -> Path:
    if raw is None:
        return fallback
    if isinstance(raw, str) and raw:
        return Path(raw).expanduser()
    raise ValueError(f"{field} must be a non-empty path string")


def _str_value(raw: object, fallback: str | None, field: str) -> str | None:
    if raw is None:
        return fallback
    if isinstance(raw, str) and raw:
        return raw
    raise ValueError(f"{field} must be a non-empty string")


def _int_value(raw: object, fallback: int, field: str) -> int:
    if raw is None:
        return fallback
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    raise ValueError(f"{field} must be an integer")


def _bool_value(raw: object, fallback: bool, field: str) -> bool:
    if raw is None:
        return fallback
    if isinstance(raw, bool):
        return raw
    raise ValueError(f"{field} must be a boolean")


def load_config(config_path: Path | None = None) -> VaultConfig:
    path = (config_path or DEFAULT_CONFIG_PATH).expanduser()
    raw: dict[str, object] = {}
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    _reject_unknown_keys(raw, TOP_LEVEL_KEYS, "top-level")

    paths_raw = _table_value(raw, "paths")
    _reject_unknown_keys(paths_raw, PATH_KEYS, "paths")
    home = _path_value(paths_raw.get("home"), Path.home(), "paths.home")
    workspace_root = _path_value(paths_raw.get("workspace_root"), home / "workspace", "paths.workspace_root")
    import_root = _path_value(
        paths_raw.get("import_root"),
        home / ".config" / "tokscale" / "imports",
        "paths.import_root",
    )
    projection_home = _path_value(
        paths_raw.get("projection_home"),
        home / ".config" / "tokscale" / "projection-home",
        "paths.projection_home",
    )
    local_workspace_extras = _path_value(
        paths_raw.get("local_workspace_extras"),
        home / ".config" / "tokscale" / "local-workspace-extras",
        "paths.local_workspace_extras",
    )
    stable_root = _path_value(
        paths_raw.get("stable_root"),
        home / "agent-session-vault" / "stable",
        "paths.stable_root",
    )

    archive_raw = _table_value(raw, "archive")
    _reject_unknown_keys(archive_raw, ARCHIVE_KEYS, "archive")
    archive_root = _path_value(
        archive_raw.get("root"),
        home / "agent-session-vault" / "archive",
        "archive.root",
    )
    cadence_days = _int_value(archive_raw.get("cadence_days"), 14, "archive.cadence_days")
    cold_age_days = _int_value(archive_raw.get("cold_age_days"), 30, "archive.cold_age_days")
    staging_root = _path_value(
        archive_raw.get("staging_root"),
        home / ".cache" / "agent-session-vault" / "archive-staging",
        "archive.staging_root",
    )
    machine_id_path = _path_value(
        archive_raw.get("machine_id_path"),
        home / ".config" / "agent-session-vault" / "machine-id",
        "archive.machine_id_path",
    )

    source_paths_value = archive_raw.get("source_paths")
    if source_paths_value is None:
        source_paths_raw: list[object] = []
    elif isinstance(source_paths_value, list):
        source_paths_raw = source_paths_value
    else:
        raise ValueError("archive.source_paths must be an array")
    source_paths: list[ArchiveSourceConfig] = []
    for index, item in enumerate(source_paths_raw):
        if isinstance(item, str) and item:
            source_paths.append(ArchiveSourceConfig(path=item))
            continue
        if not isinstance(item, dict):
            raise ValueError(f"archive.source_paths[{index}] must be a non-empty string or table")
        _reject_unknown_keys(item, ARCHIVE_SOURCE_KEYS, f"archive.source_paths[{index}]")
        path_value = item.get("path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError(f"archive.source_paths[{index}].path must be a non-empty string")
        source_paths.append(
            ArchiveSourceConfig(
                path=path_value,
                kind=_str_value(
                    item.get("kind"),
                    "codex_home",
                    f"archive.source_paths[{index}].kind",
                )
                or "codex_home",
                label=_str_value(item.get("label"), None, f"archive.source_paths[{index}].label"),
            )
        )

    if cadence_days <= 0:
        raise ValueError("archive.cadence_days must be a positive integer")
    if cold_age_days < 0:
        raise ValueError("archive.cold_age_days must be a non-negative integer")

    return VaultConfig(
        config_path=path,
        paths=PathsConfig(
            home=home,
            workspace_root=workspace_root,
            import_root=import_root,
            projection_home=projection_home,
            local_workspace_extras=local_workspace_extras,
            stable_root=stable_root,
        ),
        archive=ArchiveConfig(
            root=archive_root,
            cadence_days=cadence_days,
            cold_age_days=cold_age_days,
            staging_root=staging_root,
            machine_id_path=machine_id_path,
            source_paths=tuple(source_paths),
            require_quiescent_for_prune=_bool_value(
                archive_raw.get("require_quiescent_for_prune"),
                True,
                "archive.require_quiescent_for_prune",
            ),
        ),
    )

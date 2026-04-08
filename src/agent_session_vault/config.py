from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agent-session-vault" / "config.toml"


@dataclass(frozen=True)
class PathsConfig:
    home: Path
    workspace_root: Path
    import_root: Path
    shadow_home: Path
    local_workspace_extras: Path
    archive_root: Path
    relay_root: Path


@dataclass(frozen=True)
class SyncConfig:
    default_strategy: str
    direct_max_delta_files: int
    direct_max_delta_bytes: int
    projection_transport: str
    projection_direct_max_bundle_bytes: int


@dataclass(frozen=True)
class RootRuleConfig:
    client: str
    path: str | None
    glob: str | None
    label: str | None
    kind: str | None


@dataclass(frozen=True)
class MachineConfig:
    name: str
    import_name: str
    ssh_target: str | None
    source_home: Path | None
    remote_relay_root: Path | None
    remote_state_root: Path | None
    sync_strategy: str | None
    direct_max_delta_files: int | None
    direct_max_delta_bytes: int | None
    clients: tuple[str, ...]
    roots: tuple[RootRuleConfig, ...]
    root_globs: tuple[RootRuleConfig, ...]


@dataclass(frozen=True)
class RetentionRuleConfig:
    name: str
    layer: str
    machine: str | None
    client: str | None
    workspace: str | None
    max_age_days: int
    min_size_bytes: int
    archive_subdir: str | None
    remove_source: bool


@dataclass(frozen=True)
class VaultConfig:
    config_path: Path
    paths: PathsConfig
    sync: SyncConfig
    machines: dict[str, MachineConfig]
    retention_rules: tuple[RetentionRuleConfig, ...]


def _path_value(raw: object, fallback: Path) -> Path:
    if isinstance(raw, str) and raw:
        return Path(raw).expanduser()
    return fallback


def _optional_path_value(raw: object) -> Path | None:
    if isinstance(raw, str) and raw:
        return Path(raw).expanduser()
    return None


def _str_value(raw: object, fallback: str | None = None) -> str | None:
    if isinstance(raw, str) and raw:
        return raw
    return fallback


def _int_value(raw: object, fallback: int | None = None) -> int | None:
    if isinstance(raw, bool):
        return fallback
    if isinstance(raw, int):
        return raw
    return fallback


def _bool_value(raw: object, fallback: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    return fallback


def load_config(config_path: Path | None = None) -> VaultConfig:
    path = (config_path or DEFAULT_CONFIG_PATH).expanduser()
    raw = {}
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))

    home = _path_value(raw.get("paths", {}).get("home") if isinstance(raw.get("paths"), dict) else None, Path.home())
    workspace_root = _path_value(
        raw.get("paths", {}).get("workspace_root") if isinstance(raw.get("paths"), dict) else None,
        home / "workspace",
    )
    import_root = _path_value(
        raw.get("paths", {}).get("import_root") if isinstance(raw.get("paths"), dict) else None,
        home / ".config" / "tokscale" / "imports",
    )
    shadow_home = _path_value(
        raw.get("paths", {}).get("shadow_home") if isinstance(raw.get("paths"), dict) else None,
        home / ".config" / "tokscale" / "shadow-home",
    )
    local_workspace_extras = _path_value(
        raw.get("paths", {}).get("local_workspace_extras") if isinstance(raw.get("paths"), dict) else None,
        home / ".config" / "tokscale" / "local-workspace-extras",
    )
    archive_root = _path_value(
        raw.get("paths", {}).get("archive_root") if isinstance(raw.get("paths"), dict) else None,
        home / "agent-session-vault-archive",
    )
    relay_root = _path_value(
        raw.get("paths", {}).get("relay_root") if isinstance(raw.get("paths"), dict) else None,
        archive_root / "relay",
    )

    sync_raw = raw.get("sync") if isinstance(raw.get("sync"), dict) else {}
    default_strategy = _str_value(sync_raw.get("default_strategy"), "auto")
    if default_strategy not in {"auto", "direct", "relay"}:
        raise ValueError(f"unsupported sync.default_strategy: {default_strategy}")
    direct_max_delta_files = _int_value(sync_raw.get("direct_max_delta_files"), 128)
    direct_max_delta_bytes = _int_value(sync_raw.get("direct_max_delta_bytes"), 256 * 1024 * 1024)
    projection_transport = _str_value(sync_raw.get("projection_transport"), "auto")
    projection_direct_max_bundle_bytes = _int_value(
        sync_raw.get("projection_direct_max_bundle_bytes"),
        1024 * 1024 * 1024,
    )
    if direct_max_delta_files is None or direct_max_delta_bytes is None:
        raise ValueError("sync direct delta thresholds must be integers")
    if projection_direct_max_bundle_bytes is None:
        raise ValueError("sync projection bundle threshold must be an integer")
    if projection_transport not in {"auto", "ssh", "relay"}:
        raise ValueError(f"unsupported sync.projection_transport: {projection_transport}")

    machines_raw = raw.get("machines") if isinstance(raw.get("machines"), dict) else {}
    machines: dict[str, MachineConfig] = {}
    for name, machine_raw in machines_raw.items():
        if not isinstance(machine_raw, dict):
            continue
        clients_raw = machine_raw.get("clients")
        clients = tuple(client for client in clients_raw if isinstance(client, str)) if isinstance(clients_raw, list) else (
            "codex",
            "gemini",
            "openclaw",
        )
        roots_raw = machine_raw.get("roots") if isinstance(machine_raw.get("roots"), list) else []
        root_globs_raw = machine_raw.get("root_globs") if isinstance(machine_raw.get("root_globs"), list) else []

        def _load_root_rules(items: list[object], *, allow_path: bool, allow_glob: bool) -> tuple[RootRuleConfig, ...]:
            rules: list[RootRuleConfig] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                client = _str_value(item.get("client"))
                if client is None:
                    raise ValueError(f"machine {name} root rule requires client")
                path_value = _str_value(item.get("path")) if allow_path else None
                glob_value = _str_value(item.get("glob")) if allow_glob else None
                if allow_path and path_value is None:
                    raise ValueError(f"machine {name} explicit root requires path")
                if allow_glob and glob_value is None:
                    raise ValueError(f"machine {name} root glob requires glob")
                rules.append(
                    RootRuleConfig(
                        client=client,
                        path=path_value,
                        glob=glob_value,
                        label=_str_value(item.get("label")),
                        kind=_str_value(item.get("kind")),
                    )
                )
            return tuple(rules)

        machines[name] = MachineConfig(
            name=name,
            import_name=str(machine_raw.get("import_name") or name),
            ssh_target=_str_value(machine_raw.get("ssh_target")),
            source_home=_optional_path_value(machine_raw.get("source_home")),
            remote_relay_root=_optional_path_value(machine_raw.get("remote_relay_root")),
            remote_state_root=_optional_path_value(machine_raw.get("remote_state_root")),
            sync_strategy=_str_value(machine_raw.get("sync_strategy")),
            direct_max_delta_files=_int_value(machine_raw.get("direct_max_delta_files")),
            direct_max_delta_bytes=_int_value(machine_raw.get("direct_max_delta_bytes")),
            clients=clients,
            roots=_load_root_rules(roots_raw, allow_path=True, allow_glob=False),
            root_globs=_load_root_rules(root_globs_raw, allow_path=False, allow_glob=True),
        )
        if machines[name].sync_strategy and machines[name].sync_strategy not in {"auto", "direct", "relay"}:
            raise ValueError(f"unsupported machines.{name}.sync_strategy: {machines[name].sync_strategy}")

    retention_raw = raw.get("retention") if isinstance(raw.get("retention"), dict) else {}
    retention_rules_raw = retention_raw.get("rules") if isinstance(retention_raw.get("rules"), list) else []
    retention_rules: list[RetentionRuleConfig] = []
    for rule_raw in retention_rules_raw:
        if not isinstance(rule_raw, dict):
            continue
        name = _str_value(rule_raw.get("name"))
        layer = _str_value(rule_raw.get("layer"))
        max_age_days = _int_value(rule_raw.get("max_age_days"))
        if name is None or layer is None or max_age_days is None:
            raise ValueError("retention rule requires name, layer, and max_age_days")
        retention_rules.append(
            RetentionRuleConfig(
                name=name,
                layer=layer,
                machine=_str_value(rule_raw.get("machine")),
                client=_str_value(rule_raw.get("client")),
                workspace=_str_value(rule_raw.get("workspace")),
                max_age_days=max_age_days,
                min_size_bytes=_int_value(rule_raw.get("min_size_bytes"), 1) or 1,
                archive_subdir=_str_value(rule_raw.get("archive_subdir")),
                remove_source=_bool_value(rule_raw.get("remove_source"), False),
            )
        )

    return VaultConfig(
        config_path=path,
        paths=PathsConfig(
            home=home,
            workspace_root=workspace_root,
            import_root=import_root,
            shadow_home=shadow_home,
            local_workspace_extras=local_workspace_extras,
            archive_root=archive_root,
            relay_root=relay_root,
        ),
        sync=SyncConfig(
            default_strategy=default_strategy,
            direct_max_delta_files=direct_max_delta_files,
            direct_max_delta_bytes=direct_max_delta_bytes,
            projection_transport=projection_transport,
            projection_direct_max_bundle_bytes=projection_direct_max_bundle_bytes,
        ),
        machines=machines,
        retention_rules=tuple(retention_rules),
    )

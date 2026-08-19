from __future__ import annotations

from dataclasses import dataclass
import os

from .config import VaultConfig
from .views import build_tokscale_view


DEFAULT_TOKSCALE_PACKAGE = "tokscale@3.0.0"
TOKSCALE_PACKAGE_ENV = "AGENT_SESSION_VAULT_TOKSCALE_PACKAGE"


@dataclass(frozen=True)
class TokscaleInvocation:
    env: dict[str, str]
    command: list[str]


def build_tokscale_invocation(
    config: VaultConfig,
    args: list[str],
    package_override: str | None = None,
) -> TokscaleInvocation:
    view = build_tokscale_view(config)
    env = dict(os.environ)
    env.pop("CODEX_HOME", None)
    env["HOME"] = str(view.home)
    env["NPM_CONFIG_CACHE"] = str(config.paths.home / ".npm")
    if view.extra_dirs:
        env["TOKSCALE_EXTRA_DIRS"] = view.tokscale_extra_dirs()
    else:
        env.pop("TOKSCALE_EXTRA_DIRS", None)
    tokscale_package = package_override or env.get(TOKSCALE_PACKAGE_ENV, DEFAULT_TOKSCALE_PACKAGE)
    if package_override:
        env[TOKSCALE_PACKAGE_ENV] = package_override
    return TokscaleInvocation(env=env, command=["npx", "-y", tokscale_package, *args])


def build_antigravity_sync_invocation(
    config: VaultConfig,
    *,
    package_override: str,
    config_dir: str,
) -> TokscaleInvocation:
    env = dict(os.environ)
    env.pop("CODEX_HOME", None)
    env.pop("TOKSCALE_EXTRA_DIRS", None)
    env["HOME"] = str(config.paths.home)
    env["NPM_CONFIG_CACHE"] = str(config.paths.home / ".npm")
    env["TOKSCALE_CONFIG_DIR"] = config_dir
    env[TOKSCALE_PACKAGE_ENV] = package_override
    return TokscaleInvocation(
        env=env,
        command=["npx", "-y", package_override, "antigravity", "sync"],
    )

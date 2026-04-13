from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from .config import VaultConfig
from .views import build_view


@dataclass(frozen=True)
class TokscaleInvocation:
    env: dict[str, str]
    command: list[str]


def build_tokscale_invocation(
    config: VaultConfig,
    mode: str,
    args: list[str],
    omx_replay_dedupe: str = "off",
) -> TokscaleInvocation:
    view = build_view(config, mode=mode, omx_replay_dedupe=omx_replay_dedupe)
    env = dict(os.environ)
    env.pop("CODEX_HOME", None)
    env["HOME"] = str(view.home)
    env["NPM_CONFIG_CACHE"] = str(config.paths.home / ".npm")
    if view.extra_dirs:
        env["TOKSCALE_EXTRA_DIRS"] = view.tokscale_extra_dirs()
    else:
        env.pop("TOKSCALE_EXTRA_DIRS", None)
    return TokscaleInvocation(env=env, command=["npx", "-y", "tokscale@latest", *args])

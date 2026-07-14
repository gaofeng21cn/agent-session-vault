# Agent Session Vault Repository Guide

This repository owns the local-first session control plane for multi-machine AI
agent histories, with projection-only Tokscale continuity as the default product
contract. Full-fidelity conversation migration remains an explicit optional
capability.

## Working Rules

- Use the user-level `~/.codex/TASTE.md` as the general collaboration taste,
  then apply this repository's README, docs, config, source, tests, and CLI
  readbacks as the project authority.
- Do not create a second truth source for Tokscale session inputs, projection
  state, stable local extras, or archive status. Keep project facts in this
  repo's config, docs, source, tests, and command output.
- Treat Tokscale as a downstream exporter. This repository prepares the raw or
  canonical views that Tokscale reads; it does not patch Tokscale upstream.
- Keep the default raw Tokscale view projection-only. Refresh the current local
  HOME into `imports/local-home/.raw` and do not point Tokscale at live client
  roots. Full live-session copies require an explicit migration command.
- Prefer the repository CLI over ad hoc scripts:
  `agent-session-vault config --json`,
  `agent-session-vault ops daily-tokscale --json`,
  `agent-session-vault sync auto <machine> --json`,
  `agent-session-vault tokscale env --mode raw --json`, and
  `agent-session-vault tokscale exec --mode raw -- submit -c codex,gemini,openclaw`.
- Keep `projection-first` as the default cross-machine sync path. Raw sync,
  relay details, archive/offload, and package-version overrides must stay
  explicit operator choices.
- When a task requires Tokscale package currentness, check the package version
  fresh and pass it through `AGENT_SESSION_VAULT_TOKSCALE_PACKAGE=<package>`;
  do not run a naked `tokscale` command outside this repo entrypoint.

## Verification

- Default validation is `python3 -m pytest`.
- Documentation-only changes can use review plus `git diff --check`.

<!-- CODEGRAPH_START -->
## CodeGraph

- 本仓库使用本地 `.codegraph/` 索引；该目录不得纳入 Git。
- 定义、调用、影响范围和代码路径等结构检索优先使用 CodeGraph；字面文本检索使用 `rg`。
- 索引缺失或过期时运行 `codegraph init .` 或 `codegraph sync .`。
<!-- CODEGRAPH_END -->

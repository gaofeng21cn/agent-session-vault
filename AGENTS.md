# Agent Session Vault Repository Guide

This repository owns the local-first projection, Tokscale aggregation, stable
analytics recovery, and full-fidelity Codex archive jobs that run with OPL
Fleet. OPL Fleet is the only multi-machine node, network, admission, dispatch,
and artifact-transport control plane.

## Working Rules

- Apply the user-level `~/.codex/TASTE.md`, then this repository's current
  source, configuration parser, CLI help, tests, and runtime readbacks.
- Use [`docs/README.md`](docs/README.md) as the documentation ownership map.
  Update the owning document in the same change as a behavior change; do not
  create dated plans, duplicate guides, or compatibility documentation.
- Keep one cross-machine path: `agent-session-vault sync fleet --json`. Fleet
  owns node inventory, routes, fresh admission, dispatch, and artifact return.
- Keep one Tokscale view. It contains local and Fleet projections plus managed
  local extras, uses `projection_home` as `HOME`, and excludes live client
  roots. Do not add a second selectable view.
- Preserve imported projection history when upstream files disappear. A
  projection is analytics input, not a full conversation backup and not
  deletion authority.
- Treat the stable layer as a restore copy. Its default profile protects
  analytics continuity; live-session migration remains explicit and requires
  quiescent clients.
- Keep full-fidelity Codex archives independent from projections and stable
  analytics. Archive cycles never prune; restore stays in staging; local prune
  requires a digest-protected plan and all runtime evidence.
- Treat Tokscale as a downstream exporter. Never patch it or run it outside
  the repository entrypoint. When package currentness matters, check it fresh
  or pass `AGENT_SESSION_VAULT_TOKSCALE_PACKAGE=<package>` explicitly.
- Prefer these repository entrypoints over ad hoc scripts:
  `agent-session-vault config --json`,
  `agent-session-vault sync fleet --json`,
  `agent-session-vault tokscale env --json`,
  `agent-session-vault tokscale exec -- submit -c codex,gemini,openclaw --dry-run`,
  `agent-session-vault ops daily-tokscale --json`, and
  `agent-session-vault ops archive-cycle --due-only --deep --json`.
- Keep real paths, machine identities, sessions, projections, archives,
  receipts, and logs outside the repository.

## Verification

- Default validation: `python3 -m pytest`.
- Run `ruff check .` for Python changes.
- For documentation changes, also verify CLI help, local links, retired-term
  absence, bilingual contract parity, and `git diff --check`.

<!-- CODEGRAPH_START -->
## CodeGraph

- 本仓库使用本地 `.codegraph/` 索引；该目录不得纳入 Git。
- 定义、调用、影响范围和代码路径等结构检索优先使用 CodeGraph；字面文本检索使用 `rg`。
- 索引缺失或过期时运行 `codegraph init .` 或 `codegraph sync .`。
<!-- CODEGRAPH_END -->

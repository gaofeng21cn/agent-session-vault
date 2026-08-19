# Documentation Ownership and Lifecycle

This file owns only the documentation map and lifecycle rules. Product
introduction belongs in the root READMEs; operations, configuration,
architecture, Agent routing, and repository execution each have a separate
owner.

## Current Document Set

| Document | Sole responsibility |
| --- | --- |
| [`README.md`](../README.md), [`README.zh-CN.md`](../README.zh-CN.md) | Product scope, installation, shortest supported paths, and links |
| [`docs/README.md`](README.md) | Documentation ownership and lifecycle |
| [`docs/en/OPERATIONS.md`](en/OPERATIONS.md), [`docs/zh/OPERATIONS.md`](zh/OPERATIONS.md) | Human procedures, command sequences, receipts, and operational safety |
| [`docs/en/CONFIGURATION.md`](en/CONFIGURATION.md), [`docs/zh/CONFIGURATION.md`](zh/CONFIGURATION.md) | Configuration schema, defaults, constraints, and path effects |
| [`docs/en/ARCHITECTURE.md`](en/ARCHITECTURE.md), [`docs/zh/ARCHITECTURE.md`](zh/ARCHITECTURE.md) | Ownership boundaries, data flow, storage domains, and invariants |
| [`skills/agent-session-vault/SKILL.md`](../skills/agent-session-vault/SKILL.md) | Agent task routing, authorization boundaries, and prohibited shortcuts |
| [`AGENTS.md`](../AGENTS.md) | Repository-local implementation and verification rules |

The English and Chinese files in each pair describe the same contract. Neither
language is a historical archive for the other.

## Authority

Current behavior is established by source, configuration parsing, CLI help,
tests, and command readback. Documents explain that behavior but do not create
an alternative runtime contract. When they disagree, fix the owning document
in the same change that establishes the code behavior.

## Lifecycle Rules

1. Give every new fact one owning document. Other documents link to it instead
   of copying its detailed explanation.
2. Update or remove affected documentation in the same change as a command,
   schema, data-flow, or safety-boundary change.
3. Remove retired commands, modules, options, tests, and compatibility paths
   from current documentation. Git history is the archive; the current tree
   describes only the supported product.
4. Keep dated plans, implementation diaries, investigation logs, and release
   history in issues, pull requests, or releases, not under `docs/`.
5. Replace accumulated milestone lists with the current decision, current
   workflow, or current invariant. Do not append another status section.
6. Keep the root READMEs short. Complete command procedures belong only in
   `OPERATIONS.md`; field-by-field detail belongs only in `CONFIGURATION.md`.
7. Review both language variants together and verify every documented command
   against current CLI help before merging.

Documents with no current product responsibility are deleted rather than moved
to a second in-repository archive. Their prior content remains recoverable from
Git history without contaminating current search results.

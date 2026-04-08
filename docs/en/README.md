# Documentation

This repository has two layers of documentation:

- the root [README](../../README.md) for public overview and quick start
- this `docs/en` tree for configuration, architecture, and agent-oriented usage

Recommended reading order:

1. [Root README](../../README.md)
2. [Configuration Guide](CONFIGURATION.md)
3. [Architecture Guide](ARCHITECTURE.md)
4. [Agent Guide](AGENT_GUIDE.md)

If your immediate goal is Tokscale:

1. read the root [README](../../README.md)
2. copy the example config from [`config/agent-session-vault.example.toml`](../../config/agent-session-vault.example.toml)
3. define your machines and roots
4. run `agent-session-vault sync auto <machine>`
5. run `agent-session-vault tokscale exec ...`

If your immediate goal is storage tiering or archive planning:

1. read the [Architecture Guide](ARCHITECTURE.md)
2. inspect `archive` commands from the root README
3. add retention rules to your config

Public documents in this repository:

- [Configuration Guide](CONFIGURATION.md)
- [Architecture Guide](ARCHITECTURE.md)
- [Agent Guide](AGENT_GUIDE.md)

Chinese documentation:

- [中文文档索引](../zh/README.md)

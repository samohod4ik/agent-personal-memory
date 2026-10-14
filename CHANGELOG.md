# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-14 — Initial extraction

### Added

- Core library: AES-256-GCM per-record DEKs wrapped by a file KEK,
  SQLite (WAL + secure_delete) store with v1→v4 migrations, FTS5 ranking,
  authorized reads with byte-identical denials.
- Owner flows: remember/learn, candidate review, correct, two-phase
  forget/do-not-remember, explain, bilingual EN/RU UX, owner export.
- Physical erasure destroying DEKs with anonymized tombstone receipts.
- Two-phase confirm-gate: preview carries no nonce; nonce lives only in
  the injected send channel, single-use, TTL- and binding-bound.
- Optional thin stdio MCP facade (`[mcp]` extra): fixed startup identity,
  21 tool schemas, operator outbox at `<data_dir>/confirmations.jsonl`
  (0600, `O_NOFOLLOW`, TTL-pruned), nonce-only `personal_memory_confirm`.
- Agent Skill (`skills/agent-personal-memory/SKILL.md`).
- Offline benchmark corpus (10 canary cases) and hardened hygiene scanner.

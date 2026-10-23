# Changelog

## 0.3.0 — layered memory

- Add opt-in versioned journal, exact evidence, bounded core/recall, period hierarchy, guarded consolidation and Markdown export.
- Preserve original-source lineage and erase targeted spans/projections through existing trusted deletion.
- Add transactional schema v6 upgrade; old-code rollback needs the matching previous DB backup.
- Add Windows tzdata dependency; retain optional plaintext FTS caveat.
- Credit Iva, agent-second-brain and autograph by Serge Shima (smixs).


All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0] - Unreleased

### Added

- Stem-aware RU/EN search (`text_norm`): casefold + `ё`→`е`
  normalization, Snowball stemming for Russian and English via the new
  mandatory `snowballstemmer>=3.0.1,<4` dependency (BSD-3-Clause),
  stop-word filtering for content queries, and protected tokens that are
  never stemmed (digits, mixed-script, UUIDs, dates, originally-uppercase
  tokens of ≤6 chars). Matching is substring-or-stem, so every previous
  hit is still found.
- FTS index v2: `memory_claim_fts_index` gains `body_stem` alongside
  `body`, with a `memory_fts_meta` normalizer version. The index is kept
  in sync inside the write transaction on create/confirm/correct/
  accept/retract/erase; restricted and non-active claims are never
  indexed; an old-format or version-mismatched index is treated as
  absent (native-ranking fallback). `build_claim_fts_index` rebuilds v2
  per tenant.
- Reconciliation (issue #2): `propose_reconciliations` compares each new
  memory with up to five confirmed memories of the same owner via a
  pluggable `ConflictJudge` (`judge(new_text, old_text) -> Relation`,
  `judge_id`); the default `SlotRuleJudge` (`slot_rule.v1`) is
  deterministic and conservative. A contradiction creates a pending
  `supersedes` link (new → old) and keeps the new memory a `candidate`.
- Five new tools (26 total): `list_memory_reconciliations`,
  `accept_memory_reconciliation`, `reject_memory_reconciliation`,
  `personal_memory_history`, `retract_personal_memory`. The `states`
  enum in `query_claims`/`rank_claims` schemas gains `retracted`.
- Claim history (issue #3): `memory_claim_versions.change_reason`
  (`created`/`promoted`/`changed`/`retracted`, inferred from the audit
  log for pre-v5 versions), owner-facing `personal_memory_history` over
  the version chain and accepted links (≤20 hops), a metadata-only
  `history` field in `explain_claim`, and link rows in
  `export_owner_data`.
- `retracted` terminal lifecycle state: excluded from every default
  read, readable by id, auto-rejects pending links that touch it, erased
  by `forget_personal_memory` like any other claim.
- `docs/adr/0003-reconciliation-and-history.md`.

### Changed

- Schema version 4→5 (`memory_claim_links`, `change_reason`,
  `MemoryState.RETRACTED`); the migration runs automatically on open and
  is idempotent. **A v5 database cannot be opened by 0.1.x builds — back
  up `<data_dir>` before upgrading.**
- `remember_personal_insight` results and the persisted items of
  `learn_from_turn` carry a `reconciliations` key; in `auto_insight`
  mode a new memory with pending links stays `candidate` instead of
  auto-confirming.
- Default reads (`query_claims` with `states=None`, ranking, lists,
  context assembly) bind an explicit active-state allowlist
  (`candidate` + `confirmed`), so `superseded` and `retracted` are
  excluded everywhere by default.
- `remember`/`learn` accept an optional `judge=` argument so a host can
  inject a custom `ConflictJudge`; none ships with the package.

### Fixed

- Stop-word-only query false positives are gone (an all-stop-word query
  keeps the previous behaviour).
- The derived FTS index no longer goes stale after writes — it is
  synced inside the same transaction (a stale or old-format table is
  treated as absent until rebuilt).

### Security

- `docs/threat-model.md` now documents `body_stem` as a second
  plaintext-derived copy when an FTS index is built, the plaintext
  link/change-reason metadata, ciphertext retention for `retracted`
  memories, and the accepted reconciliation edge cases (orphaned
  confirmation source, raising custom judges, expired candidates).

### Upgrade notes

- **Back up `<data_dir>` before upgrading**: the v4→v5 migration is
  one-way; a v5 database cannot be opened by 0.1.x builds.
- Run `build_claim_fts_index` once per tenant to get stemmed FTS
  ranking; until then `rank_claims_fts` falls back to native ranking
  automatically.
- New mandatory dependency `snowballstemmer>=3.0.1,<4` is installed
  with the package.

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

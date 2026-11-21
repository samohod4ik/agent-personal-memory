# Threat model — agent-personal-memory

Scope: the `agent_personal_memory` Python package (v0.3.0) and its optional
MCP stdio server. This document describes what the shipped controls do —
and, just as importantly, what they do not do. Companion documents:
[adr/0002-core-boundary.md](adr/0002-core-boundary.md) (extraction
boundary), [adr/0003-reconciliation-and-history.md](adr/0003-reconciliation-and-history.md)
(reconciliation links, history, retraction) and
[adr/0008-physical-erasure-and-tombstones.md](adr/0008-physical-erasure-and-tombstones.md)
(erasure design, ported verbatim from the source project).

## Assets

| Asset | Where it lives | Protection |
| --- | --- | --- |
| Owner memories (claim content, explanations) | `memory_claim_versions` fields in `<data_dir>/personal_memory.db` | AES-256-GCM per field |
| Source snapshots, locators, metadata | `memory_source_versions` fields in the same DB | AES-256-GCM per field |
| Per-record DEKs (256-bit data keys) | `memory_keys.wrapped_dek` rows | Wrapped by the KEK; destroyed on erasure |
| KEK (256-bit key-encryption key) | `<data_dir>/master.key` — **a plaintext file**, created mode 0600 | OS file permissions only |
| SQLite DB + WAL files | `<data_dir>/personal_memory.db{,-wal,-shm}` | Field-level ciphertext + host protection |
| Derived FTS index | `memory_claim_fts_index` table inside the DB | **Plaintext** claim text (`body`) plus a second plaintext-derived copy (`body_stem`) — opt-in, see below |
| Reconciliation links | `memory_claim_links` rows | **Plaintext** metadata: claim ids, `relation`, `status`, `to_version`, `origin` (judge id), timestamps — no claim text |
| Version change reasons | `memory_claim_versions.change_reason` | **Plaintext** single-token reason (`created`/`promoted`/`changed`/`retracted`) |
| Source `external_ref` values | `memory_sources.keyed_lookup_hash` | HKDF-derived lookup key + HMAC; no plaintext refs |
| Confirm-gate nonces | In-memory `DeleteRegistry` (TTL 600 s); under MCP also inside outbox `body` lines | One-time, bound to the exact `SessionBinding` |
| Operator outbox | `<data_dir>/confirmations.jsonl` (MCP only) | OS file permissions (0600); intended for the operator's eyes; TTL-expired lines are pruned on each append |

## Actors

- **Owner/operator** — the human the memory belongs to and the only
  principal allowed to authorize destructive operations.
- **Model/agent** — calls the owner-flow APIs (or MCP tools). Trusted to
  read what it is authorized for; **not** trusted to authorize deletion —
  that is what the confirm-gate is for.
- **Other principals** — additional `Actor`s sharing one store. Tenant
  isolation is enforced at the query layer: a foreign tenant's records are
  indistinguishable from absent ones (identical `NotFoundError`, byte-identical
  denial payloads).
- **Host process** — the application embedding the library. It is fully
  trusted: it chooses the `Actor` it passes, holds the KEK while running,
  and sees decrypted plaintext. The library cannot defend against it.
- **Host-level adversary** — another process or OS user, a stolen disk, a
  copied backup. This is the boundary the at-rest encryption addresses.

## Trust boundaries

1. **Host → library.** `Actor` and `SessionBinding` are asserted by the
   caller, not authenticated by the library. If the host binds the wrong
   identity, isolation cannot help — correct binding is the host's job.
2. **Process → disk.** The encryption boundary. Everything the store
   persists is ciphertext except the artifacts listed under *Honest
   limits* (FTS index, schema/audit/deletion-manifest metadata, tombstone
   lifecycle fields).
3. **Model → operator channel.** The confirm-gate boundary. A nonce crosses
   only through the operator-delivered `send()` payload — or, under MCP,
   the operator-only outbox file — never through a model-visible return
   value.

## Cryptography scope

- AES-256-GCM (via `cryptography`): a fresh random nonce per field
  encryption; a fresh random 256-bit DEK per record version; the DEK is
  wrapped by the KEK; AAD binds each ciphertext to
  `tenant_id / entity_kind / entity_id / version / field`, so moving a blob
  between tenants, records, versions, or fields fails authenticated
  decryption.
- Source dedup/lookup uses an HKDF-derived lookup key plus HMAC, so
  `external_ref` values are stored as keyed hashes — idempotent
  re-registration works without storing refs in plaintext.
- **At-rest only.** Decrypted values live in process memory while in use.
  Nothing protects them from the host process, a debugger, a core dump, or
  the OS page/swap file.
- **KEK custody is the host's responsibility.** `master.key` is a plaintext
  256-bit file created with mode 0600. Anyone who copies `master.key`
  together with the database can decrypt everything. Protect `<data_dir>`
  with OS user separation and/or full-disk encryption; do not sync or
  commit it. The package intentionally does not implement passphrase
  wrapping or KMS integration — a host that needs those wraps the data
  directory itself.
- **Snapshot/backup retention.** Encrypted backups may retain ciphertext
  until the recorded retention policy expires — this sentence is carried
  verbatim in every `ErasureReceipt` (`backup_retention_note`) so the
  caveat travels with the data.

## Erasure: what it guarantees and what it does not

A confirmed erasure (`erase_bound_claims`, or `erase_claim`/`erase_source`
under it) guarantees, inside the live database:

- the wrapped DEKs are destroyed (zeroblob-overwritten, rows removed) — the
  ciphertext becomes unrecoverable even if residue survives;
- the encrypted field blobs are zeroblob-overwritten then nulled;
- support edges and derived FTS rows for the target are deleted;
- `deleted_at`/`deletion_state` tombstones are set, the claim versions are
  marked `deleted`, and an **anonymized** receipt is appended to
  `memory_deletion_manifest` (`receipt_id`, `operation`, `target_kind`,
  `outcome`, `erased_at` — no content, no external refs);
- post-commit compaction runs `secure_delete=ON` + `wal_checkpoint
  (TRUNCATE)` + `VACUUM`;
- reads afterwards fail closed: erased ids are byte-identical `NotFoundError`;
  an erased source may only re-register as a new source id (its
  `keyed_lookup_hash` is wiped).

It does **not** guarantee:

- removal from backups, filesystem snapshots, or OS-level copies taken
  before the erasure — those may retain ciphertext until your retention
  policy removes them (see *KEK custody* — ciphertext without destroyed
  DEKs is still protected, but ciphertext in a backup made while the DEK
  was live is only as safe as that backup's KEK story);
- overwriting of every physical remnant — wear-leveling, page/swap
  remnants, and freed pages below SQLite's `secure_delete`/`VACUUM`
  semantics are outside its reach;
- removal of the tombstone — deletion receipts persist by design so the
  owner retains an audit trail of what was erased and when.

## The confirm-gate threat model

The threat being addressed: **a model must not be able to authorize its own
destructive operation**, whether by hallucination, prompt injection, or a
misleading summary.

Mechanism:

1. Model-facing calls (`forget_personal_memory`, `do_not_remember`,
   `reject_candidate_memory`, and `create_delete_preview` underneath)
   return a **non-authorizing** payload: `request_id`, matched item
   summaries, human-readable text. No nonce exists in it.
2. The nonce is generated inside `DeleteRegistry`, embedded solely in the
   `send(text)` payload, and registered for consumption only after `send()`
   returns truthy. A raised exception, a falsy return, or an awaitable
   return is a failed send — no live nonce. Because the payload text is the
   exact preview, the operator confirms *this* deletion, not a paraphrase.
3. `consume_confirm(nonce, binding)` requires the nonce plus a byte-identical
   `SessionBinding` (platform, user_id, session_key, session_id, chat_id,
   thread_id, profile). Nonces are single-use and expire after 600 s. The
   nonce check runs through `secrets.compare_digest` — belt-and-braces
   constant-time comparison on top of an already hash-keyed dict lookup.
   An observer who merely sees a delivered preview in a shared channel cannot
   consume another user's grant — the bindings differ.
4. `consume_delete_request` re-checks every target (claim id, version,
   version id, content hash) inside one transaction; any drift denies the
   whole batch. `confirm`/`confirmed` booleans in tool arguments are
   rejected by `reject_legacy_confirm`.

Under MCP the same gate holds end-to-end: the facade delivers the
nonce-bearing text to `<data_dir>/confirmations.jsonl` (plus a nonce-free
stderr notice), tool results are defensively scrubbed of nonce-shaped keys,
and `personal_memory_confirm` accepts only a `nonce` argument. The model
may call it but cannot mint or guess a valid nonce. A nonce is therefore
**operator-channel data, not model data**: treat a nonce appearing in any
model-visible output as a breach and treat `confirmations.jsonl` like a
secret file (0600-equivalent custody).

## Supply chain

- Runtime dependencies: `cryptography>=42` and
  `snowballstemmer>=3.0.1,<4` (BSD-3-Clause; deterministic stemming for
  the search normalization layer).
- Optional `[mcp]` extra: `mcp>=1.28.1` (floor raised past the
  CVE-affected 1.x line; the facade adapts to both the 1.x decorator and
  2.x constructor SDK APIs).
- Storage is stdlib `sqlite3`; tests and `bench/` run with no network.
- The repo carries `tools/hygiene_scan.py` (CI-enforced) scanning tracked
  files for host paths, secret-shaped literals, and source-host internals.

## Honest limits

- **The library does not authenticate its caller.** `Actor` is asserted by
  the host; a host that passes the wrong actor grants that actor a
  well-formed tenant. Identity correctness is out of scope.
- **No defense against the host or same-UID processes.** Whoever can read
  the process memory, the KEK file, or ptrace the process can get
  plaintext. The encryption boundary starts at the disk.
- **The FTS index stores plaintext — twice.** `build_claim_fts_index`
  materializes decrypted claim text into `memory_claim_fts_index` for
  ranking; in the v2 format the table holds `body` (the claim text)
  **and** `body_stem`, a second plaintext-derived copy of the same text
  reduced to Snowball stems. It is opt-in and rebuildable, erasure
  purges both columns' rows, restricted claims are never indexed, and
  `rank_claims` works without it — but if you need strictly ciphertext
  at rest, do not build it.
- **Metadata is plaintext.** Schema shape, audit rows (action/target-kind/
  outcome/reason — no content), lifecycle fields, timestamps, principal
  ids, the deletion manifest, `memory_claim_versions.change_reason`, and
  `memory_claim_links` rows (claim ids, `relation`, `status`,
  `to_version`, `origin` — the judge id — and timestamps) are
  unencrypted. These leak existence and activity patterns — including
  that a replacement was proposed or resolved and by which judge — not
  contents.
- **A retracted memory keeps its ciphertext.** `retract_personal_memory`
  removes a claim from every default read (query, ranking, FTS, context,
  lists) and auto-rejects pending links that touch it, but it
  deliberately preserves the encrypted content and its DEKs — the
  record stays readable by id until `forget_personal_memory` physically
  erases it. Retraction is a lifecycle state, not destruction.
- **`explain_claim` mirrors the by-id invisibility rules.** Restricted
  and expired claims return the byte-identical not-found denial, and its
  `history` field carries metadata-only entries (no `content` key).
- **Reconciliation edge cases are documented, not crash-atomic across
  steps.** Accepted limitations: `accept_memory_reconciliation`
  registers the `user_confirmation` source in its own transaction before
  resolving the link, so a stale-link `ConflictError` (or a race) can
  leave that source row orphaned; a custom judge that raises after the
  new candidate was committed leaves the candidate stored with no links;
  accepting a link whose new claim is already `confirmed` — or whose
  candidate version has since expired — resolves exactly like
  `confirm_claim` (promotion clears `expires_at`); rejecting a link
  registers no source; and accept preserves the old memory's
  `expires_at` on its terminal `superseded` version while
  `retract_personal_memory` clears it on the `retracted` one.
- **`confirmations.jsonl` (MCP) holds nonce-bearing text** — lines whose
  nonce TTL has passed are pruned on the next append, so the file is a
  transient delivery channel, not an audit log. Expired previews authorize
  nothing, but a live line contains preview text of what is about to be
  deleted. The outbox assumes a single writer per `data_dir` (one server
  process); a concurrent append could be lost during a prune rewrite —
  sharing one `data_dir` across live server processes is unsupported.
- **No multi-user UX or service hardening.** No authentication server, no
  rate limiting, no sandboxing — this is an embeddable library; the host
  provides the operating environment.
- **Erasure ≠ legal-grade guaranteed destruction.** It is a strong
  crypto-shredding + physical-deletion implementation with explicit
  backup/OS-copy caveats, as enumerated above.

## Layered-memory addition (0.3)

New journal capture is explicit and host-only. Identity, role and origin must be
set after host authentication; none is a model tool selector. Exact spans are
data, never instructions. Core uses current confirmed nonrestricted facts;
forwarded/assistant/derived events cannot be promoted as direct owner facts.
Projection bodies use wrapped DEKs; optional FTS remains plaintext.
[Layered contract](layered-memory.md) describes bounded source opening, event
tombstones, span erasure, uncertain paid-call outcomes, retention and the limits
of semantic redaction, external exports and old backups.

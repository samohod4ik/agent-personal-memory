# agent-personal-memory

Local-first, encrypted personal memory for AI agents — a Python library,
an optional MCP stdio server, and an Agent Skill. MIT-licensed.

Russian mirror: [README.ru.md](README.ru.md)

## What it is

`agent_personal_memory` is a single-owner memory store that an agent host
embeds directly. Records live in one directory (`<data_dir>`): a SQLite
database (`personal_memory.db`, WAL + `secure_delete`) and a 256-bit
key-encryption key file (`master.key`, created mode 0600). There is no
server, no network call, and no telemetry.

- **Encrypted at rest.** Every stored field (claim content, explanation,
  source snapshot/locator/metadata) is AES-256-GCM-encrypted with a fresh
  random per-record DEK wrapped by the file KEK. Ciphertext is bound by AAD
  to tenant / entity / version / field, so swapping a blob between records
  fails authenticated decryption. Source `external_ref` values are stored as
  keyed HMAC lookup hashes.
- **Explicit identity.** Every call takes an `Actor(platform, user_id,
  display_name)` constructed by the host; nothing reads ambient session
  state. Cross-tenant reads fail closed: missing, foreign, and erased
  records return the same `NotFoundError`.
- **Confirm-gated destructive operations.** Forgetting is two-phase: the
  model-facing call produces only a non-authorizing preview; the one-time
  nonce is generated inside the gate, delivered over an injected operator
  channel, and is single-use, TTL-bound, and tied to the exact
  `SessionBinding`. A model-supplied `confirm` boolean is rejected.
- **Physical erasure.** Confirmed deletion destroys the wrapped DEKs and
  encrypted blobs, drops support edges and FTS rows, compacts the database,
  and leaves only an anonymized tombstone receipt.
- **Deterministic recall.** SQLite FTS5 (tenant scope inside the `MATCH`
  query) plus stem-aware RU/EN lexical/entity/recency ranking and
  tenant-local BM25 — no embeddings, no LLM in the loop.
- **Owner-resolved conflicts.** When a new memory contradicts a confirmed
  one, a deterministic judge proposes a `supersedes` link instead of
  silently overwriting; only the owner's accept promotes the new memory
  and supersedes the old. Full per-version history (`change_reason`,
  accepted links both directions) and a `retracted` state for
  facts that stopped being true but should stay on record.
- **Explainability and export.** `explain_claim` / `explain_why_remembered`
  report provenance, lifecycle state, and supporting sources;
  `export_owner_data` produces a full owner export. Owner-facing text is
  bilingual (EN/RU).

See [docs/threat-model.md](docs/threat-model.md) for what this does and
does not protect, and
[skills/agent-personal-memory/SKILL.md](skills/agent-personal-memory/SKILL.md)
for the embedding guide aimed at agents.

## Install

Requires Python 3.10+. Runtime dependencies: `cryptography>=42` and
`snowballstemmer>=3.0.1,<4` (BSD-3-Clause).

```bash
pip install .            # from a checkout
pip install ".[mcp]"     # with the optional MCP server
# or straight from the repository if you have access:
pip install "git+ssh://git@github.com/samohod4ik/agent-personal-memory.git"
```

### Upgrading from 0.1.x

The v4→v5 schema migration runs automatically on first open and is
idempotent — but a database migrated to v5 **cannot** be opened by 0.1.x
builds. Back up `<data_dir>` before upgrading. To get stemmed FTS
ranking, rebuild the derived index once per tenant with
`build_claim_fts_index` — until then `rank_claims_fts` falls back to
native ranking automatically.

## Quickstart (< 15 minutes)

The snippet below is executed verbatim by `tests/test_readme_quickstart.py`.

```python
import re
import tempfile

from agent_personal_memory import (
    Actor,
    CONFIRM_REGISTRY,
    PersonalMemoryStore,
    SessionBinding,
    consume_delete_request,
    forget_personal_memory,
    list_personal_memories,
    remember_personal_insight,
)

# data_dir resolution: explicit arg -> PERSONAL_MEMORY_DATA_DIR ->
# ~/.agent-personal-memory
with tempfile.TemporaryDirectory(prefix="pm-quickstart-") as data_dir:
    store = PersonalMemoryStore(data_dir=data_dir)

    # Identity is explicit: the host constructs the Actor.
    owner = Actor(platform="cli", user_id="owner-1", display_name="Owner")

    # remember: under the default auto_insight mode this lands confirmed.
    saved = remember_personal_insight(
        owner, "I prefer morning standups", store=store
    )
    claim_id = str(saved["claim_id"])

    # list: the owner-facing inventory.
    inventory = list_personal_memories(owner, store=store)
    assert inventory["count"] >= 1

    # forget, phase 1: a non-authorizing preview -- request_id plus
    # human-readable confirmation text, never a nonce.
    binding = SessionBinding(
        platform="cli",
        user_id="owner-1",
        session_key="cli:owner-1",
        session_id="session-1",
        chat_id="cli-direct",
    )
    preview = forget_personal_memory(
        owner, claim_id, store=store, session_binding=binding
    )
    assert preview["preview"]
    assert "nonce" not in preview

    # forget, phase 2: deliver the preview over an operator channel via an
    # injected send(text) -> bool. The nonce is generated inside the gate,
    # exists solely inside the send payload, and is registered for
    # consumption only after send() returns truthy.
    delivered = []

    def send(text):
        delivered.append(text)  # a real host prints this to the operator
        return True

    CONFIRM_REGISTRY.open_preview(
        str(preview["request_id"]), binding, send=send
    )
    nonce = re.search(r"/memory_confirm (\S+)", delivered[-1]).group(1)

    # forget, phase 3: only the operator-held nonce authorizes erasure.
    record = CONFIRM_REGISTRY.consume_confirm(nonce, binding)
    result = consume_delete_request(record, store=store)
    assert result["erased_claim_ids"] == [claim_id]
    print(result["confirmation"])
```

## Search normalization

Recall is deterministic and bilingual. Query text and stored text are
normalized the same way — casefold, `ё`→`е`, Snowball stemming for
Russian and English (via the mandatory `snowballstemmer` dependency) —
so «какая видеокарта у меня» also finds a fact stored as «видеокартой».
A token matches when it appears as a substring (the 0.1.x behaviour)
**or** its stem equals a stored stem, so every previous hit still
matches; stop-word-only false positives are gone, while a query made
entirely of stop words keeps the old behaviour. Identifiers are never
stemmed: tokens with digits, mixed Cyrillic/Latin tokens, UUIDs, dates,
and originally-uppercase tokens of ≤6 characters — "RTX 3060" still
matches exactly.

Known limits: different Snowball roots are not unified («созваниваться»
does not match «созвоны» — pinned by an expected-failure test), and the
`query_claims`-bounded surfaces keep a 500-row cap. The optional FTS
index is v2: `memory_claim_fts_index` holds `body` plus `body_stem`
(a stemmed plaintext copy — see the threat model) and a normalizer
version in `memory_fts_meta`; it is kept in sync inside the write
transaction on create/confirm/correct/accept/retract/erase, restricted
and non-active claims are never indexed, and an old-format or
version-mismatched index is treated as absent (native-ranking fallback)
until `build_claim_fts_index` rebuilds it per tenant.

## Reconciliation

When a new memory contradicts a confirmed one, the library does not pick
a winner — the owner does. `remember_personal_insight` results and the
persisted items of `learn_from_turn` carry `reconciliations`: pending `supersedes`
links (new → old) proposed by a `ConflictJudge` against up to five
confirmed memories of the same owner. The default `SlotRuleJudge`
(`slot_rule.v1`) is deterministic and conservative — it compares a few
personal slots (home, name, name-style; RU and EN) and prefers false
negatives; a host can inject any judge (e.g. an LLM) via `judge=`. A
contradiction keeps the new memory `candidate` and the old one
`confirmed`; `accept_memory_reconciliation` promotes the new memory and
supersedes the old — after re-checking the old memory did not change
since the proposal — and `reject_memory_reconciliation` dismisses it.

```python
import tempfile

from agent_personal_memory import (
    Actor,
    PersonalMemoryStore,
    accept_memory_reconciliation,
    list_memory_reconciliations,
    personal_memory_history,
    rank_claims,
    remember_personal_insight,
)

with tempfile.TemporaryDirectory(prefix="pm-reconcile-") as data_dir:
    store = PersonalMemoryStore(data_dir=data_dir)
    owner = Actor(platform="cli", user_id="owner-1", display_name="Owner")

    first = remember_personal_insight(owner, "I live in Kazan", store=store)
    assert first["state"] == "confirmed"
    assert first["reconciliations"] == []

    second = remember_personal_insight(
        owner, "I moved to Perm, I no longer live in Kazan", store=store
    )
    # The deterministic judge flags a contradiction: the new memory
    # stays a candidate and a pending supersedes link (new -> old) is
    # proposed instead of overwriting anything.
    assert second["state"] == "candidate"
    link = second["reconciliations"][0]
    assert link["relation"] == "supersedes"
    assert link["status"] == "pending"

    pending = list_memory_reconciliations(owner, store=store)
    assert pending["count"] == 1

    # Only the owner resolves it: accept promotes the new memory and
    # supersedes the old one.
    resolved = accept_memory_reconciliation(
        owner, link["link_id"], store=store
    )
    assert resolved["status"] == "accepted"
    assert resolved["new_state"] == "confirmed"
    assert resolved["old_state"] == "superseded"

    hits = rank_claims(owner, "where do I live", store=store)
    assert str(hits[0]["claim_id"]) == str(second["claim_id"])

    # History walks the version chain plus the accepted link.
    history = personal_memory_history(
        owner, claim_id=str(first["claim_id"]), store=store
    )
    assert history["current_claim_id"] == str(second["claim_id"])
    assert {entry["claim_id"] for entry in history["entries"]} == {
        str(first["claim_id"]),
        str(second["claim_id"]),
    }
    print(f"current memory: {history['current_claim_id']}")
```

## History and retraction

`personal_memory_history` (by `claim_id` or `query`) walks a memory's
version chain and every accepted replacement around it — chronological
entries with state, `change_reason` (`created` / `promoted` / `changed`
/ `retracted`), timestamps, and content for the owner. The promotion
step folds into the creation entry by default (`collapse_promoted`);
`current_claim_id` points at the newest memory in the chain. Versions
written by 0.1.x databases get a reason inferred from the audit log
(`inferred: true`). `explain_claim` carries the same entries
metadata-only. When a fact stops being true but the record should stay,
`retract_personal_memory` appends a terminal `retracted` version: the
memory leaves every default read, stays readable by id and in history,
any pending links touching it are auto-rejected, and its content
remains encrypted until `forget_personal_memory` erases it.

## API map

All names below are importable from `agent_personal_memory` (the canonical
surface is `src/agent_personal_memory/__init__.py`). Mutating and
confirm-gated flows take `store` as a required keyword argument; read
helpers still default `store` to `None`.

| Area | Name | What it does |
| --- | --- | --- |
| Store / identity | `PersonalMemoryStore(data_dir=None)` | Opens or creates the store directory, runs schema migrations, loads or creates `master.key` |
| | `Actor(platform, user_id, display_name="")` | Frozen tenant identity; the host builds it, the model never supplies it |
| | `create_candidate(store, actor, ...)` | Unbound `PersonalMemoryStore.create_candidate`; always creates a draft (candidate) claim |
| Remember / learn | `remember_personal_insight` | Store an explicit durable fact or preference; lands `confirmed` or `candidate` per the owner's memory mode |
| | `learn_from_turn` | Rule-based durable-insight extraction from an owner turn (persists only in `auto_insight` mode) |
| | `get_memory_mode` / `set_memory_mode` | Per-owner mode: `auto_insight` (default) or `confirm_required` |
| Review | `list_candidate_memories` | List drafts awaiting owner review |
| | `confirm_candidate_memory` | Promote a candidate to `confirmed` via a user-confirmation source |
| | `reject_candidate_memory` | Preview candidate erasure (confirm-gated) |
| Reconciliation | `list_memory_reconciliations` | List replacement links (default `pending`) with owner-visible old/new texts |
| | `accept_memory_reconciliation` | Owner-accept a pending `supersedes` link: promotes the new memory, supersedes the old (stale-checked) |
| | `reject_memory_reconciliation` | Mark a pending link rejected; both memories stay unchanged |
| | `propose_reconciliation` / `propose_reconciliations` | Manual link proposal / judge-driven pass; `ConflictJudge`, `SlotRuleJudge`, `Relation` form the judge seam (`judge=` kwarg) |
| History / lifecycle | `personal_memory_history` | Chronological version+link history for one claim (`claim_id` or `query`; `collapse_promoted` default on) |
| | `retract_personal_memory` | Terminal `retracted` state: hidden from default reads, readable by id, content stays encrypted until forget |
| Read / rank | `list_personal_memories` | Owner-facing inventory ("what do you remember?"), localized EN/RU |
| | `query_claims` / `query_sources` | Authorized metadata queries; closed-vocabulary filters apply before any decrypt |
| | `get_authorized_claim` / `get_authorized_source` | Single-record reads; absent, foreign, and erased ids are identical `NotFoundError` |
| | `rank_claims` / `rank_claims_fts` | Deterministic lexical/entity/recency ranking; the FTS variant uses the tenant-scoped FTS5 index (falls back when absent) |
| | `build_claim_fts_index` / `fts_index_present` | Manage the optional derived FTS5 index (holds plaintext — see the threat model) |
| | `score_locality` / `bm25_scores` / `apply_snippet_acl` | Tenant-local BM25 helpers and the snippet ACL used by the bench |
| | `assemble_context` | Bounded `personal` + `session` context bundle for a prompt |
| Explain / export | `explain_claim` / `explain_why_remembered` | Provenance, lifecycle state, and supporting sources for a claim |
| | `export_owner_data` / `export_personal_memory` | Full owner export (UTF-8 JSON bytes / localized summary) |
| Mutations | `correct_personal_memory` | Append a corrected claim version (the old one becomes `superseded`) |
| | `forget_personal_memory` | Non-authorizing deletion preview for a claim (confirm-gated) |
| | `do_not_remember` | Preview erasure by `claim_id` and/or matching text (up to 10 claims) |
| Confirm-gate | `SessionBinding` | Host-provisioned authenticated-session binding the gate checks verbatim |
| | `create_delete_preview` | Registry-backed preview; returns no nonce |
| | `CONFIRM_REGISTRY` (`DeleteRegistry`) | `open_preview(request_id, binding, send=)` delivers the nonce; `consume_confirm(nonce, binding)` spends it once; `reset_registry` for tests |
| | `consume_delete_request` | Executes the bound erasure in one transaction after re-checking every target |
| | `reject_legacy_confirm` | Denies model-supplied confirm booleans |
| Schemas / engines | `TOOL_SCHEMAS` + per-tool `*_SCHEMA` constants | Canonical tool schemas — the MCP `tools/list` surface; none accepts identity selectors |
| | `NativeMemoryEngine`, `Mem0Adapter`, `persist_shadow_candidates`, `purge_shadow_projection` | Engine protocol, native implementation, and the protocol-only Mem0 shadow layer (no mem0 calls) |
| Errors / types | `NotFoundError`, `ConflictError`, `ValidationError`, `TrustedDeleteDenied`, `SchemaVersionError`, `MemoryState`, `ErasureReceipt`, `DeleteRequest`, `ConfirmNonce`, `BoundTarget`, `DeleteRegistry` | Raised or returned by the flows above |

## MCP server (optional)

```bash
pip install "agent-personal-memory[mcp]"

agent-personal-memory-mcp \
    --actor-platform cli \
    --actor-user-id <user-id> \
    --session-key <key> \
    --session-id <id> \
    --chat-id <chat> \
    [--actor-display-name <name>] [--thread-id <t>] [--profile <p>] \
    [--data-dir <dir>]
```

`python -m agent_personal_memory.mcp` accepts the same flags. `tools/list`
mirrors `TOOL_SCHEMAS` verbatim; `tools/call` dispatches to the owner flows
above.

**Fixed identity.** The five binding flags provision the `Actor` and the
operator `SessionBinding` once at startup; a missing or placeholder binding
fails fast (exit 2). Tool arguments can never carry actor, tenant, or
session selectors — extra fields are rejected.

**Operator outbox (confirm-gate over MCP).** A destructive call
(`forget_personal_memory`, `do_not_remember`, `reject_candidate_memory`)
returns only the non-authorizing preview. The facade then delivers the
nonce-bearing preview text to the operator-only outbox
`<data_dir>/confirmations.jsonl` — one JSON line
`{"ts", "request_id", "body"}` — plus a nonce-free notice on stderr
(stdout carries JSON-RPC only). The one-time nonce never appears in a tool
result. The operator reads the outbox, finds `/memory_confirm <nonce>`
inside `body`, and calls the `personal_memory_confirm` tool, whose only
argument is `nonce`. Ignoring a preview is the cancel path — the nonce
expires on its own (TTL 600 s). Lines whose nonce TTL has passed are
pruned on the next append: the outbox is a transient delivery channel,
not an audit log.

Ported-text note: preview strings still mention `/memory_delete
<request_id>` and `/memory_cancel <request_id>` — commands from the source
host that do not exist on this MCP surface. The outbox line already *is*
the delivered preview; SKILL.md documents the mapping.

## How it differs

Verifiable architecture and data-control differences — not benchmark
claims. Comparison facts reflect public docs/repos as of the October 2026
landscape review; this space moves fast.

| | agent-personal-memory | Mem0 (`mem0ai`) | Letta | basic-memory |
| --- | --- | --- | --- | --- |
| Packaging | Embeddable Python library + optional local stdio MCP + Agent Skill | Python library + optional Docker server + hosted platform; the official MCP endpoint is cloud-hosted | Standalone stateful agent runtime (current implementation is TypeScript), not a memory library | Local MCP server over Markdown files + a SQLite index |
| Records at rest | AES-256-GCM per-record DEKs wrapped by a file KEK | Not encrypted at rest | Plaintext Markdown (MemFS) in a git repo | Plaintext Markdown on disk |
| In the loop | Deterministic rules + SQLite FTS5/BM25; no LLM, no embeddings | LLM + embedder + vector/graph store required for extraction and retrieval | Runtime-managed memory | Local embeddings (FastEmbed) |
| Destructive ops | Two-phase operator confirm-gate; the nonce never reaches the model | Direct delete tool call | Runtime tools | Direct delete tool call |
| Tenant model | Explicit `Actor` per call; tool schemas carry no identity selectors | User ids passed in API/tool args | Single-agent runtime | Workspace-based |
| Telemetry | None — the package makes no network calls | Opt-out telemetry enabled by default | — | — |
| License | MIT | Apache-2.0 | Apache-2.0 | AGPL-3.0 |

Honest trade-offs: this package does **no** semantic/vector search and no
LLM-driven extraction (ranking is lexical/BM25, extraction is rule-based
EN/RU), and neither graph memory nor a managed tier. Choose it when the
priority is a small auditable dependency surface (two runtime
dependencies),
ciphertext at rest, and human-confirmed deletion — not recall breadth.

## Security notes

- At-rest encryption only; decrypted values live in process memory while
  in use. `master.key` is a plaintext KEK file — whoever holds it plus the
  database can decrypt everything, so protect `<data_dir>` with OS
  permissions/full-disk encryption and never sync or commit it.
- Mutating owner flows (`remember_personal_insight`, `learn_from_turn`,
  `correct_personal_memory`, `forget_personal_memory`, `do_not_remember`,
  `confirm_candidate_memory`, `reject_candidate_memory`) take `store` as a
  required keyword argument — a write can never hit an implicit default
  store. The reconciliation/history helpers
  (`list_memory_reconciliations`, `accept_memory_reconciliation`,
  `reject_memory_reconciliation`, `personal_memory_history`,
  `retract_personal_memory`) require `store=` the same way. Read helpers still accept `store=None`, which opens — and creates
  if missing — the default data dir (`PERSONAL_MEMORY_DATA_DIR`, else
  `~/.agent-personal-memory`); in embedded use pass an explicit `store=`
  everywhere so a forgotten argument cannot read the host's default
  location instead of the intended store.
- Erasure is physical inside the live database but cannot reach backups,
  snapshots, or OS-level copies made before it (the `ErasureReceipt`
  carries an explicit backup-retention note).
- The optional FTS index (`build_claim_fts_index`) stores plaintext claim
  text inside the DB for ranking — `body` plus a second plaintext-derived
  `body_stem` column; skip it if you need strictly ciphertext at rest —
  `rank_claims`/`rank_claims_fts` work without it.
- Supply chain: runtime dependencies `cryptography>=42` and
  `snowballstemmer>=3.0.1,<4`; the `mcp` SDK ships only under the `mcp`
  extra. Tests and bench need no network.
- Full details: [docs/threat-model.md](docs/threat-model.md).

## Provenance and license

Core extracted from the host project's `plugins/personal_memory` package at
source snapshot `f0f6908` under the extraction boundary recorded in
[docs/adr/0002-core-boundary.md](docs/adr/0002-core-boundary.md). Every
ported file, its source path, and every deviation are mapped in
[docs/PROVENANCE.md](docs/PROVENANCE.md).

License: [MIT](LICENSE) — Copyright (c) 2026 samohod4ik.

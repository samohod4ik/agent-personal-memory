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
  fails authenticated decryption. Source `external_ref`s are stored as
  keyed HMAC lookup hashes.
- **Explicit identity.** Every call takes an `Actor(platform, user_id,
  display_name)` constructed by the host; nothing reads ambient session
  state. Cross-tenant reads fail closed: missing, foreign, and erased
  records return the same `NotFoundError`.
- **Confirm-gated destructive operations.** Forgetting is two-phase: the
  model-facing call produces only a non-authorizing preview; the one-time
  nonce is generated inside the gate, delivered over an injected operator
  channel, and is single-use, TTL-bound, and bound to the exact
  `SessionBinding`. A model-supplied `confirm` boolean is rejected.
- **Physical erasure.** Confirmed deletion destroys the wrapped DEKs and
  encrypted blobs, drops support edges and FTS rows, compacts the database,
  and leaves only an anonymized tombstone receipt.
- **Deterministic recall.** SQLite FTS5 (tenant scope inside the `MATCH`
  query) plus lexical/entity/recency ranking and tenant-local BM25 — no
  embeddings, no LLM in the loop.
- **Explainability and export.** `explain_claim` / `explain_why_remembered`
  report provenance, lifecycle state, and supporting sources;
  `export_owner_data` produces a full owner export. Owner-facing text is
  bilingual (EN/RU).

See [docs/threat-model.md](docs/threat-model.md) for what this does and
does not protect, and
[skills/agent-personal-memory/SKILL.md](skills/agent-personal-memory/SKILL.md)
for the embedding guide aimed at agents.

## Install

Requires Python 3.10+. The sole runtime dependency is `cryptography>=42`.

```bash
pip install .            # from a checkout
pip install ".[mcp]"     # with the optional MCP server
# or straight from the (private) repository if you have access:
pip install "git+ssh://git@github.com/samohod4ik/agent-personal-memory.git"
```

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

## API map

All names below are importable from `agent_personal_memory` (the canonical
surface is `src/agent_personal_memory/__init__.py`).

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
expires on its own (TTL 600 s).

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
EN/RU), and has no graph memory and no managed tier. Choose it when the
priority is a small auditable dependency surface (one runtime dependency),
ciphertext at rest, and human-confirmed deletion — not recall breadth.

## Security notes

- At-rest encryption only; decrypted values live in process memory while
  in use. `master.key` is a plaintext KEK file — whoever holds it plus the
  database can decrypt everything, so protect `<data_dir>` with OS
  permissions/full-disk encryption and never sync or commit it.
- Erasure is physical inside the live database but cannot reach backups,
  snapshots, or OS-level copies made before it (the `ErasureReceipt`
  carries an explicit backup-retention note).
- The optional FTS index (`build_claim_fts_index`) stores plaintext claim
  text inside the DB for ranking; skip it if you need strictly ciphertext
  at rest — `rank_claims`/`rank_claims_fts` work without it.
- Supply chain: sole runtime dependency `cryptography>=42`; the `mcp` SDK
  ships only under the `mcp` extra. Tests and bench need no network.
- Full details: [docs/threat-model.md](docs/threat-model.md).

## Provenance and license

Core extracted from the host project's `plugins/personal_memory` package at
source snapshot `f0f6908` under the extraction boundary recorded in
[docs/adr/0002-core-boundary.md](docs/adr/0002-core-boundary.md). Every
ported file, its source path, and every deviation is mapped in
[docs/PROVENANCE.md](docs/PROVENANCE.md).

License: [MIT](LICENSE) — Copyright (c) 2026 samohod4ik.

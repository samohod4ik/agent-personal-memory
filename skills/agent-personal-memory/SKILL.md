---
name: agent-personal-memory
description: "Local-first encrypted personal memory for AI agents. Use when an agent needs to remember durable owner-stated facts, preferences, and identity, recall them through authorized reads, run the candidate-to-confirmed review lifecycle, or honor forget and erase requests through the two-phase operator confirm-gate. Do not use for secrets, credentials, ephemeral session state, or third-party data."
license: MIT
---

# Agent Personal Memory

## What it is

`agent_personal_memory` is local-first, encrypted personal memory for AI
agents — a single-owner SQLite store where records are encrypted at rest
with AES-256-GCM (per-record data keys wrapped by a file-held master key),
every read is tenant-scoped and authorized before decrypt, and deletion is
physical rather than a soft flag. It ships as a Python library — the
canonical public surface is `src/agent_personal_memory/__init__.py` — and as an
optional thin MCP stdio server (`agent-personal-memory-mcp`, or
`python -m agent_personal_memory.mcp`, under the `mcp` extra).

Identity is always explicit: every call takes an `Actor(platform, user_id,
display_name)` constructed by the host. Tool schemas accept no actor,
tenant, user, or platform selectors — the host binds identity, never the
model.

## When to remember

Store memory only for the authenticated owner, and only for durable content
the owner stated or clearly consented to:

- **Owner-stated durable facts and identity** — name, location, biography,
  stable roles ("my name is …", "I live in …", "call me …").
- **Preferences and style** — likes and dislikes, diet, communication
  style, stated decisions and tasks.
- **Explicit requests** — "remember that …" / «запомни…» go through
  `remember_personal_insight` (library and tool).
- **Passive learning** — `learn_from_turn(actor, turn_text, store=...)`
  extracts durable insights from an owner turn, but only in `auto_insight`
  memory mode (the default); in `confirm_required` mode it stores nothing.
- **Corrections** — when the owner fixes a remembered fact, prefer
  `correct_personal_memory`: it registers a user-confirmation source and
  creates a new claim version (the old version becomes `superseded`),
  keeping history honest.

### Candidate vs confirmed lifecycle

Claims move through a closed lifecycle (`MemoryState`): `candidate` →
`confirmed` → `superseded` / `retracted` / `deleted`. Drafts come before
facts:

- `create_candidate` (the unbound `PersonalMemoryStore.create_candidate`,
  called as `create_candidate(store, actor, ...)`) always lands a draft.
- `remember_personal_insight` respects the owner's memory mode:
  `auto_insight` saves the claim confirmed; `confirm_required` saves a
  candidate for review.
- Review drafts with `list_candidate_memories`, promote with
  `confirm_candidate_memory`, drop with `reject_candidate_memory`
  (destructive — the confirm-gate rule below applies).
- Reads: `list_personal_memories`, `query_claims`, `query_sources`,
  `get_authorized_claim`, `get_authorized_source`, `rank_claims`,
  `assemble_context`, `explain_why_remembered`, `explain_claim`.
- The mode is per-owner: `get_memory_mode` / `set_memory_mode`
  (`auto_insight` | `confirm_required`).

## When memories conflict (reconciliation)

A new memory that contradicts a confirmed one is **not** auto-resolved:
`remember_personal_insight` results and the persisted items of
`learn_from_turn` carry a `reconciliations` list of pending `supersedes`
links (new → old) proposed by a deterministic judge. The new memory
stays a `candidate` and the old one stays `confirmed` until the
**owner** decides.

When a remember/learn result has a non-empty `reconciliations` list:

1. Tell the owner what the proposal is — `list_memory_reconciliations`
   returns the pending links under a localized `heading` with the new
   and old texts.
2. Ask whether the new fact should replace the old one, then call
   `accept_memory_reconciliation` (promotes the new memory, supersedes
   the old) or `reject_memory_reconciliation` (dismisses the link; both
   memories stay unchanged).
3. **Never accept without the owner's explicit agreement.** A pending
   link changes nothing about reads — leaving it pending is always
   safe, and a rejected proposal for the same pair never comes back.

An accept can fail with `ConflictError` if the old memory changed since
the proposal — re-list the links and ask again rather than retrying
blindly.

## History and retraction

- **"Where did I live before?" / "what did you remember about X?"** —
  use `personal_memory_history` (by `claim_id`, or `query` when you do
  not know the id). It walks the version chain plus accepted
  replacements in both directions; `current_claim_id` is the newest
  memory in the chain. `explain_claim` carries the same entries
  metadata-only.
- **"That's no longer true, but keep the record"** — prefer
  `retract_personal_memory` over `forget_personal_memory`: the memory
  leaves every default read but stays readable by id and in history,
  pending links touching it are auto-rejected, and its content stays
  encrypted. Use `forget_personal_memory` when the owner wants the
  content **erased**, not just retired.

## When NOT to remember (privacy do's and don'ts)

- **Never store secrets or credentials** — passwords, passphrases, API
  keys, private keys, session tokens, OTPs, PINs, card numbers or CVV,
  passport/SSN data. The content filters (`turn_has_blocked_secret`,
  `insight_content_allowed`) reject this material and
  `remember_personal_insight` raises rather than persist it — do not work
  around the refusal. Secrets belong to the OS keychain or environment
  variables, never to memory.
- **Never store ephemeral session state** — scratch reasoning, one-off task
  context, transient errors, pending confirmations, live nonces.
- **Never store data the owner did not consent to.** Do not ingest other
  people's messages or personal details as owner memory; third-party
  private data is blocked by the filters and is never a fact "about the
  owner".
- **Never treat another chat participant's statements as the owner's
  facts.** Memory is bound to the explicit `Actor`, not to the room.
- **Do not duplicate.** `remember_personal_insight` rejects content whose
  normalized form is already remembered; query first when unsure.

## When to forget

Erase when:

- **The owner asks** — "forget this", "don't remember X", "erase that":
  `forget_personal_memory` (by `claim_id`) or `do_not_remember` (by
  `claim_id` and/or matching content text, up to ten matched claims).
- **A fact is stale or contradicted** and the owner wants it gone rather
  than corrected — otherwise prefer `correct_personal_memory` (same-claim
  fix, preserves the version chain) or `retract_personal_memory`
  (retire the claim, keep its encrypted record and history).
- **A candidate is rejected** — `reject_candidate_memory`.

Erasure is **physical**: after the confirm-gate consumes the request,
`consume_delete_request` destroys the wrapped data keys and encrypted
blobs, drops support edges and full-text rows, and leaves only anonymized
tombstone receipts (`ErasureReceipt`: `receipt_id`, `erased_at`) in the
deletion manifest. An erased claim reads back as indistinguishable
not-found; its source may only re-register as a new source id.

## The confirmation rule (critical)

Every destructive owner-flow operation is **two-phase**:

1. **Preview** — `forget_personal_memory`, `do_not_remember` and
   `reject_candidate_memory` (all built on `create_delete_preview`) return
   a **non-authorizing** payload: `request_id`, matched item summaries and
   human-readable confirmation text. It never contains a nonce.
2. **Deliver** — the host must deliver the exact preview text to the human
   operator over an authenticated channel. In the library that is
   `CONFIRM_REGISTRY.open_preview(request_id, binding, send=...)`: the
   nonce is generated inside the gate, embedded solely in the `send(text)`
   payload, and registered for consumption only after `send` returns
   truthy. A failed send leaves no live nonce.
3. **Consume** — only the operator's returned nonce authorizes erasure:
   `CONFIRM_REGISTRY.consume_confirm(nonce, binding)` yields the request
   record, then `consume_delete_request(record, store=store)` executes the
   bound erasure inside one transaction after re-checking the targets.

Hard rules:

- **The model never sees a nonce in a tool result.** If a nonce ever
  appears in a model-visible payload, that flow is broken — do not use it,
  do not proceed.
- A model-supplied `confirm`/`confirmed` boolean can never authorize
  deletion; the library enforces this with `reject_legacy_confirm`.
- Nonces are one-time, TTL-bound (600 s), and bound to the exact
  `SessionBinding`; wrong-actor, wrong-session, replayed, or expired
  attempts are all denied.

### Operator outbox flow under MCP

Under the MCP server the delivery step folds into the preview call: after
the preview is created, the nonce-bearing preview text is appended to the
**operator-only outbox** `<data_dir>/confirmations.jsonl` (one JSON line
per send: `{"ts", "request_id", "body"}`) plus a nonce-free notice on
stderr — stdout carries JSON-RPC only. The operator reads the outbox, finds
the `/memory_confirm <nonce>` line inside `body`, and calls the
`personal_memory_confirm` tool with that nonce. The model may call
`personal_memory_confirm`, but it cannot mint or guess a valid nonce.
Outbox lines whose nonce TTL (600 s) has passed are pruned on the next
append — the file is a transient delivery channel, not an audit log.

Command mapping under MCP: the preview `confirmation`/`delete_command`
fields and the outbox `body` are ported verbatim from the library, so they
still mention `/memory_delete <request_id>` and
`/memory_cancel <request_id>` — source-host commands that do not exist on
this MCP surface. The mapping is: `/memory_delete <request_id>` ("open the
exact preview") is already done — the outbox line *is* the delivery, and the
operator reads the nonce from `<data_dir>/confirmations.jsonl` and calls
`personal_memory_confirm`; `/memory_cancel <request_id>` maps to simply
ignoring the preview — the nonce expires on its own via the registry TTL
(600 s).

## Embedding the library

```python
from agent_personal_memory import (
    Actor,
    PersonalMemoryStore,
    assemble_context,
    learn_from_turn,
    remember_personal_insight,
)

store = PersonalMemoryStore(data_dir="<data_dir>")  # else PERSONAL_MEMORY_DATA_DIR, else ~/.agent-personal-memory
actor = Actor(platform="cli", user_id="<user-id>", display_name="Owner")

# explicit durable fact (confirmed under the default auto_insight mode)
remember_personal_insight(actor, "I prefer morning standups", store=store)

# passive extraction from an owner turn (auto_insight mode only)
learn_from_turn(actor, "call me Alex; I live in Lisbon", store=store)

# bounded recall for a prompt
bundle = assemble_context(actor, memory_store=store, query_text="schedule")
```

Mutating owner flows — `remember_personal_insight`, `learn_from_turn`,
`correct_personal_memory`, `forget_personal_memory`, `do_not_remember`,
`confirm_candidate_memory`, `reject_candidate_memory` — take `store` as a
**required** keyword argument: a write can never hit an implicit default.
The reconciliation/history helpers (`list_memory_reconciliations`,
`accept_memory_reconciliation`, `reject_memory_reconciliation`,
`personal_memory_history`, `retract_personal_memory`) require `store=`
the same way.
Read helpers still default `store` to `None`, which opens — and creates if
missing — the default data dir (`PERSONAL_MEMORY_DATA_DIR`, else
`~/.agent-personal-memory`); in embedded use always pass the constructed
`PersonalMemoryStore` explicitly so a forgotten `store=` cannot read the
host's default location.

Destructive flows additionally need a fixed `SessionBinding` provisioned by
the host:

```python
from agent_personal_memory import (
    CONFIRM_REGISTRY,
    SessionBinding,
    consume_delete_request,
    forget_personal_memory,
)

binding = SessionBinding(
    platform="cli",
    user_id="<user-id>",
    session_key="<key>",
    session_id="<id>",
    chat_id="<chat>",
)
preview = forget_personal_memory(
    actor, "<claim-id>", store=store, session_binding=binding,
)
# preview carries request_id + confirmation text only; deliver the
# nonce-bearing preview to the operator via a send(text) -> bool callable:
CONFIRM_REGISTRY.open_preview(
    preview["request_id"], binding, send=deliver_to_operator,
)
record = CONFIRM_REGISTRY.consume_confirm(nonce_from_operator, binding)
result = consume_delete_request(record, store=store)
```

## Running the MCP server

```bash
pip install "agent-personal-memory[mcp]"

agent-personal-memory-mcp \
    --actor-platform cli \
    --actor-user-id <user-id> \
    --session-key <key> \
    --session-id <id> \
    --chat-id <chat> \
    --data-dir <data_dir>
```

`python -m agent_personal_memory.mcp` takes the same flags (plus optional
`--actor-display-name`, `--thread-id`, `--profile`). The five binding flags
provision the fixed operator identity and `SessionBinding` at startup; a
missing or placeholder binding fails fast at launch, and tool arguments can
never carry identity fields — extra keys are rejected.

## Tool surface (MCP `tools/list` mirrors `TOOL_SCHEMAS`)

| Tool | Role |
| --- | --- |
| `remember_personal_insight` | Store a durable owner fact/preference (confirmed or candidate per mode) |
| `get_memory_mode` / `set_memory_mode` | Read or set the owner memory mode (`auto_insight` / `confirm_required`) |
| `list_candidate_memories` | List drafts awaiting review |
| `confirm_candidate_memory` | Promote a candidate to confirmed |
| `reject_candidate_memory` | Preview the erasure of a candidate (confirm-gate) |
| `list_personal_memories` | Owner-facing inventory ("what do you remember?") |
| `query_claims` / `query_sources` | Authorized metadata queries (filters run before any decrypt) |
| `get_authorized_claim` / `get_authorized_source` | Single-entity reads; missing, foreign, and erased ids are identical not-found |
| `rank_claims` | Deterministic lexical/entity/recency ranking over authorized claims |
| `assemble_context` | Bounded personal+session context bundle for a prompt |
| `explain_why_remembered` / `explain_claim` | Provenance and lifecycle explanations (EN/RU) |
| `correct_personal_memory` | Replace claim content with a new version |
| `export_owner_data` / `export_personal_memory` | Full owner export (JSON bytes / localized) |
| `list_memory_reconciliations` | List pending (or accepted/rejected) replacement links under a localized `heading` with old/new texts |
| `accept_memory_reconciliation` / `reject_memory_reconciliation` | Owner resolution of a pending `supersedes` link |
| `personal_memory_history` | Chronological version+link history for one claim (`claim_id` or `query`) |
| `retract_personal_memory` | Retire a claim (terminal `retracted` state; content stays encrypted until forget) |
| `forget_personal_memory` / `do_not_remember` | Non-authorizing deletion previews (confirm-gate) |
| `personal_memory_confirm` | Consume a one-time nonce — the only argument is `nonce` |

## Library API quick reference

`agent_personal_memory` exports, among others:

- **Identity/store**: `Actor`, `PersonalMemoryStore`, `SessionBinding`,
  `BoundTarget`.
- **Write/learn**: `create_candidate`, `learn_from_turn`,
  `remember_personal_insight`, `get_memory_mode`, `set_memory_mode`.
- **Review**: `list_candidate_memories`, `confirm_candidate_memory`,
  `reject_candidate_memory`.
- **Reads**: `query_claims`, `query_sources`, `get_authorized_claim`,
  `get_authorized_source`, `rank_claims`, `assemble_context`.
- **Explain/export**: `explain_why_remembered`, `explain_claim`,
  `export_owner_data`, `export_personal_memory`.
- **Mutations**: `correct_personal_memory`, `forget_personal_memory`,
  `do_not_remember`, `retract_personal_memory`.
- **Reconciliation/history**: `list_memory_reconciliations`,
  `accept_memory_reconciliation`, `reject_memory_reconciliation`,
  `personal_memory_history`, `propose_reconciliation`,
  `propose_reconciliations`, `ConflictJudge`, `SlotRuleJudge`,
  `Relation`.
- **Confirm-gate**: `create_delete_preview`,
  `CONFIRM_REGISTRY.open_preview`, `CONFIRM_REGISTRY.consume_confirm`,
  `consume_delete_request`, `reject_legacy_confirm`; `DeleteRegistry` and
  `reset_registry` for hosts/tests that manage their own registry.

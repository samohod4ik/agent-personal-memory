# ADR-0003: Reconciliation links, claim history, and retraction

- Status: Accepted (implemented in the pm-oss-memory-reconcile lane; issues #2 and #3)
- Date: 2026-10-19
- Source: new in this repository — the upstream snapshot `f0f6908` has no
  counterpart for these surfaces.

## Context

In 0.1.x a memory was an append-only version chain on a single claim:
`correct_personal_memory` superseded the previous version of the *same*
claim, and `forget_personal_memory` erased the record through the
confirm-gate. Two gaps remained:

- When the owner stated a *new* fact that contradicted an *old* one
  ("I live in Kazan" -> "I moved to Perm"), nothing linked the two
  records. Both stayed `confirmed`, and recall could surface either.
- There was no way to retire a fact while keeping its history, and no
  owner-facing view of how a memory evolved over time and across
  replacements.

The design must stay deterministic — no model, no embeddings in the
shipped loop — tenant-scoped, and compatible with the versioned,
per-version-DEK storage and the erasure model recorded in ADR-0008.
Schema version becomes 5.

## Decision

### Two records plus a link

A proposed replacement is modeled as **two separate claims joined by one
`memory_claim_links` row** (`relation='supersedes'`, direction new -> old).
`relation` is `CHECK`-constrained to the single value `supersedes`: finer
relation vocabularies would invite semantics the store cannot verify.
The row carries `status` (`pending` / `accepted` / `rejected`), the
target version it was proposed against (`to_version`), the proposing
`origin` (the judge id), and `created_at` / `resolved_at` timestamps,
with `UNIQUE(tenant_id, from_claim_id, to_claim_id)`. BEFORE
INSERT/UPDATE triggers abort any row whose tenant does not match both
claims, in the same style as the source-version tenant triggers.

Both records keep their own version chain, support sources, keys, and
erasure lifecycle; the link adds no content of its own. Link rows and
the new `memory_claim_versions.change_reason` column are **plaintext
metadata by design** — ids, relation, status, version numbers, origin,
timestamps, a single-token reason — never claim text, like the audit
log and the deletion manifest.

### Proposal: a pluggable, conservative judge

`propose_reconciliations` runs for every newly persisted memory in both
memory modes (`auto_insight` and `confirm_required`). It selects at most
five `confirmed` claims of the same tenant — claims sharing the detected
slot first, then descending stem overlap — and asks a `ConflictJudge`
(`judge(new_text, old_text) -> Relation`, plus a `judge_id` attribute
recorded as the link `origin`) about each pair. Only a `contradicts`
verdict creates a pending link.

The shipped default is `SlotRuleJudge` (`judge_id` `slot_rule.v1`), a
deterministic rule judge: it recognizes a small closed set of personal
slots (`home`, `name`, `name_style`; RU and EN phrasings), compares up
to three non-stop stems of each slot value, ignores negated clauses,
and maps temporary markers ("this week", "сегодня", ...) to
`temporary`, which never supersedes. It deliberately prefers false
negatives: contradictions its rules do not model are reported
`unrelated` or `refines`, and no link is proposed. A host may inject any
judge — including an LLM-backed one — through the `judge=` argument;
none ships with the package.

A `contradicts` verdict keeps the new memory a `candidate` — it is not
auto-confirmed even in `auto_insight` mode — while the old memory stays
`confirmed`; `remember`/`learn` results carry the proposed links under
`reconciliations`. Re-proposing the same ordered pair returns the
existing link row in its current status, so a rejected link never comes
back. A manual `propose_reconciliation` exists (`origin='manual'`) for
hosts that want to link a pair directly.

### Resolution is owner-confirmed and staleness-checked

`accept_memory_reconciliation` registers a `user_confirmation` source,
then in a single transaction re-checks that the old memory is unchanged
since the proposal — its newest version still equals `to_version` — and
is still `confirmed` and non-restricted. Otherwise it raises
`ConflictError` and nothing else changes. On success it promotes the
new memory (`change_reason` `promoted`; a no-op if the owner already
confirmed it), appends a terminal `superseded` version to the old
memory (`change_reason` `changed`, content re-encrypted under a fresh
DEK), and marks the link `accepted`. `reject_memory_reconciliation`
only flips the link to `rejected`; both claims stay as they are.
Unknown, foreign, and already-resolved link ids all raise the identical
`NotFoundError("Claim link not found")`.

### History and the `retracted` state

Every new version records `change_reason` (`created` / `promoted` /
`changed` / `retracted`). Versions written before schema v5 get a reason
inferred from the audit log at read time, flagged `inferred: true`
(`unknown` when nothing matches).

`personal_memory_history` returns chronological entries over the claim's
version chain plus the connected component of *accepted* links traversed
in both directions — bounded to 20 hops, tenant-scoped, and skipping
linked memories the owner cannot see (restricted, expired, deleted).
The `promoted` step folds into the creation entry by default
(`collapse_promoted`, surfaced as `promoted_at`); `current_claim_id` is
the newest memory reached by following `supersedes` links forward.
`explain_claim` exposes the same entries metadata-only — no `content`
key — inside its `history` field.

`retract_personal_memory` appends a terminal `retracted` version. A
retracted memory leaves every default read (query, ranking, FTS,
context assembly, lists) while remaining readable by id — so history
navigation and a later `forget` still work — and pending links touching
it are auto-rejected in the same transaction. Retraction is not
erasure: ciphertext and its DEKs persist until
`forget_personal_memory` physically erases them.

## Alternatives considered

- **Overwrite in place** (extend the correction model to cross-claim
  replacements): loses the old record as a navigable record, conflates
  a same-claim correction with a cross-claim replacement, and makes
  "what did the agent believe before" unanswerable across claims.
- **Merged record** (fold the old claim's versions into the new claim):
  breaks per-claim provenance, sources, and erasure identity — a later
  `forget` could not be scoped to one side, and history would conflate
  two differently-sourced facts.
- **Auto-resolved links** (let the judge supersede immediately): a
  deterministic false positive would silently destroy a still-valid
  confirmed memory; owner confirmation keeps the blast radius at zero
  for pending links — reads are unchanged until the owner resolves one.
- **Model-in-the-loop judging as the default**: rejected — the package
  ships no LLM and an auditable judge; the `judge=` seam keeps the
  option open for hosts.

## Consequences

- Schema version 5; the v4 -> v5 migration runs automatically on open
  and is idempotent, and a v5 database **cannot** be opened by 0.1.x
  builds (back up the data directory before upgrading).
- Reads stay fail-safe: a pending link changes nothing; only an
  owner-accepted link changes which memory is `confirmed`.
- Accepted limitations, documented in `docs/threat-model.md`: an
  orphaned `user_confirmation` source is possible when accept fails
  after source registration; a raising custom judge leaves the new
  candidate stored without links; link rows and `change_reason` are
  plaintext metadata.
- `retracted` widens `MemoryState` but stays out of
  `ACTIVE_READ_STATES`, so default reads treat it like `superseded` —
  absent — while by-id reads still succeed for history and erasure.

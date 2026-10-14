# ADR-0002: Core extraction boundary for agent-personal-memory

- Status: Accepted (drafted in PH-1 T1; finalized in PH-4 T11)
- Date: 2026-10-13
- Source: Hermes `plugins/personal_memory` snapshot `origin/main@f0f6908` (read-only)

## Context

The personal-memory feature currently lives inside the host application as
`plugins/personal_memory`. Per the packaging decision (ADR-0001) the core is
extracted into a standalone MIT-licensed package `agent-personal-memory`:
a Python library as the canonical surface, plus a thin MCP facade and an
Agent Skill. The package must not depend on the host: no host-framework
imports, no host environment variables, no host-absolute paths, no plugin
registration, no host network or messaging surfaces.

## Decision

### Inside the boundary (ported into `agent_personal_memory`)

- Persistence and crypto core: `types`, `lifecycle`, `crypto`, `store`,
  `validation`, `erasure`.
- Authorized read layer: `query`, `ranking`, `fts_index`, `score_locality`.
- Owner-flow layer: `export`, `insight_extract`, `learn` (without the host
  hook tail), `review`, `mutations`, `ux`, `locale` (EN + RU).
- Engines: `engines/native`, `engines/mem0_shadow` (protocol only; no
  registration into a host plugin system).
- Surface definitions: `schemas` (canonical tool JSON schemas), `assembler`
  (personal and session sections only).
- Confirm-gate core extracted from `trusted_delete`: the two-phase
  preview/consume registry and session binding, with host delivery replaced
  by an injected `send(text) -> bool` transport supplied by the caller.

### Confirm-gate split (`trusted_delete` → `confirmation`)

The source `trusted_delete.py` couples the confirm-gate core to the host in
two places: session resolution (env lookups and inbound-event/session-store
introspection) and transport delivery (adapter lookup, metadata plumbing,
async send callbacks). Only the gate core crosses the boundary:

- `SessionBinding` is constructed by the **host** and passed in explicitly;
  the library never resolves a session itself. The source's env-based and
  event-based binding builders stay in Hermes.
- Delivery is an injected ``send(text) -> bool`` callable supplied per
  `open_preview` call. The two-phase nonce discipline is preserved exactly:
  `create_delete_preview` returns only non-authorizing fields (`request_id`,
  matched items, human-readable text — never a nonce); the nonce is
  generated inside `DeleteRegistry.prepare_preview`, embedded solely in the
  `send()` payload text, and registered via `mark_preview_sent` only after
  `send()` returns truthy. A raised exception, a falsy return, or a
  coroutine return (an `async def` transport cannot confirm synchronous
  delivery) is a failed send and leaves no usable nonce.
- `consume_delete_request` and `create_delete_preview` require an explicit
  `store` argument; the source's default-constructed store is removed so a
  consume can never silently run against a different database than the
  preview's.
- Registry lifecycle is explicit: the module exposes `REGISTRY` plus a
  `reset_registry()` test/host hook, and `DeleteRegistry` takes an
  injectable `monotonic` clock, TTL and bound (`max_items`).

The inbound-command seam stays in Hermes: `pre_gateway_dispatch`, inbound
event trust checks, adapter delivery, command handler stubs. It is the
host's job to parse user commands, build the `SessionBinding`, inject a
`send` that can reach only the authenticated user channel, and call
`consume_confirm`/`consume_delete_request` on success.

**MCP command mapping.** The ported preview texts still mention the source
host's chat commands (`/memory_delete <request_id>`,
`/memory_confirm <nonce>`, `/memory_cancel <request_id>`). On the MCP
surface these commands do not exist; the operator outbox flow replaces
them: the nonce-bearing preview text is appended to
`<data_dir>/confirmations.jsonl` (plus a nonce-free stderr notice) instead
of being opened via `/memory_delete`, the operator reads the nonce from the
outbox and supplies it to the `personal_memory_confirm` tool (the named
consume surface, nonce-only argument), and `/memory_cancel` maps to simply
ignoring the preview — the nonce expires via the registry TTL (600 s). The
mapping is documented for agents in `skills/agent-personal-memory/SKILL.md`.
Keeping the ported strings verbatim was deliberate: changing them would
break the ported wording tests and the canonical preview text is the exact
payload the operator confirms; only the transport differs.

Cross-cutting rules:

- The actor is an explicit argument (`Actor`); nothing inside the library
  reads session state or ambient environment.
- `data_dir` resolution order: explicit argument, then
  `PERSONAL_MEMORY_DATA_DIR`, then `~/.agent-personal-memory`. All host
  environment lookups are severed.
- Crypto and erasure invariants are ported without weakening. The two-phase
  confirm-gate semantics are preserved: the nonce exists only inside the
  send() payload and is registered solely after a successful send.
- Every deviation from the `f0f6908` source is recorded in
  `docs/PROVENANCE.md`.

### Outside the boundary

The following surfaces stay in Hermes and are not extracted:

| Surface | Reason it stays in Hermes |
| --- | --- |
| Plugin registration and `plugin.yaml` manifest | host plugin lifecycle; stays in Hermes |
| Host gateway tools / `session_context` wiring | gateway internals; stays in Hermes |
| `trusted_delete` inbound seam (dispatch hook, inbound event trust, adapter delivery, env session lookup) | host session/transport coupling; stays in Hermes |
| `inject.py` auto-inject hooks | host prompt-assembly hook; stays in Hermes |
| `learn.py` hook tail | host hook coupling; stays in Hermes |
| `adapters/travel`, `travel_compute` | travel-domain feature; stays in Hermes |
| `peer_import` | peer-sync feature; stays in Hermes |
| `miniapp_*` modules | Mini App UI surface; stays in Hermes |
| `night_cycle1_tool_descriptions` | night-cycle tooling; stays in Hermes |
| `spikes/` | experiments; stays in Hermes |

## Consequences

- The package is installable and testable without the host repository;
  its sole runtime dependency is `cryptography` (`mcp` ships only in the
  `mcp` extra).
- Any future widening of the boundary requires amending this ADR and
  recording the delta in `docs/PROVENANCE.md`.
- Retained compatibility strings (format identifiers, logger names) are
  intentional and listed in PROVENANCE.md; they are not coupling.

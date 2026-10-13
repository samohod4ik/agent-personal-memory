# ADR-0002: Core extraction boundary for agent-personal-memory

- Status: Accepted-in-plan (drafted in PH-1 T1; to be finalized in PH-4)
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

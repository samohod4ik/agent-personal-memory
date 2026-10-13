# Provenance

Extracted from Hermes `plugins/personal_memory` @ `f0f6908`.

Source snapshot: `origin/main@f0f6908` (read-only). The extraction boundary
is defined in [adr/0002-core-boundary.md](adr/0002-core-boundary.md). Every
deviation from the source is recorded in the table below; the boundary ADR
carries the rationale.

## File map

| File | Source path | Source SHA | Status (ported/adapted/dropped) | Sanctioned deltas |
| --- | --- | --- | --- | --- |
| `src/agent_personal_memory/types.py` | `plugins/personal_memory/types.py` | `f0f6908` | ported | none — verbatim (retains `telegram:` external_ref label shape) |
| `src/agent_personal_memory/lifecycle.py` | `plugins/personal_memory/lifecycle.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/crypto.py` | `plugins/personal_memory/crypto.py` | `f0f6908` | ported | none — verbatim (retains `hermes.personal_memory.lookup.v1` HKDF/AAD info string) |
| `src/agent_personal_memory/store.py` | `plugins/personal_memory/store.py` | `f0f6908` | adapted | `default_data_dir` resolution changed to explicit arg → `PERSONAL_MEMORY_DATA_DIR` → `~/.agent-personal-memory`; dropped source env fallback (`HERMES_HOME` → `F:\Hermes\data\personal-memory`); constructor signature widened to `data_dir: str \| Path \| None`; logger name `plugins.personal-memory` retained verbatim |
| `src/agent_personal_memory/validation.py` | `plugins/personal_memory/validation.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/erasure.py` | `plugins/personal_memory/erasure.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/fts_index.py` | `plugins/personal_memory/fts_index.py` | `f0f6908` | adapted | partial port: only the erasure-side helpers `store.py` calls (`delete_claim_fts_rows`, `_fts_table_exists`, `_ensure_fts_table` with the same FTS5 table DDL); index-build and FTS ranking functions depend on the query/ranking modules and land with the authorized-read task; no behavior change on erasure paths |
| `docs/adr/0008-physical-erasure-and-tombstones.md` | `docs/adr/0008-physical-erasure-and-tombstones.md` | `f0f6908` | ported | none — verbatim; referenced by the ported erasure test |
| `tests/test_store.py` | `plugins/personal_memory/tests/test_store.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*`; source-file reads go through `store_module.__file__` instead of a plugins-root path |
| `tests/test_crypto.py` | `plugins/personal_memory/tests/test_crypto.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*` |
| `tests/test_erasure.py` | `plugins/personal_memory/tests/test_erasure.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*`; ADR-0008 path resolved against this repo's `docs/adr/`; `patch` target renamed to `agent_personal_memory.store` |
| `tests/test_sources.py` | `plugins/personal_memory/tests/test_sources.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*` |
| `tests/test_validation.py` | `plugins/personal_memory/tests/test_validation.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*`; synthetic `OPAQUE_SECRET` fixture retained (allowlisted) |
| `tests/test_audit.py` | `plugins/personal_memory/tests/test_audit.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*` |
| `tests/test_memory_mode.py` | `plugins/personal_memory/tests/test_memory_mode.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*` |
| `tests/test_lifecycle.py` | `plugins/personal_memory/tests/test_lifecycle.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*`; source-file reads go through `lifecycle_module.__file__`/`store_module.__file__` |

### T2 notes

- All crypto/erasure invariants are byte-faithful to `f0f6908`: AES-GCM-256,
  per-record DEKs wrapped by the file KEK (`<data_dir>/master.key`), AAD
  binding tenant/entity/version/field, WAL + `secure_delete` + foreign-key
  pragmas, `BEGIN IMMEDIATE` writes, v1→v4 in-place migrations, and physical
  erasure that destroys DEKs while leaving anonymized tombstone receipts.
- No `gateway` imports, `HERMES_*` env reads, or source-tree
  (`plugins/personal_memory/`) import references remain in the ported
  modules; the only env var read is `PERSONAL_MEMORY_DATA_DIR`.

## Retained compatibility strings

Ported code intentionally keeps the following source strings verbatim. They
are format identifiers or observability labels, not host coupling; each is
allowlisted in `tools/hygiene_allowlist.txt`.

| Marker | Location | Rationale |
| --- | --- | --- |
| `hermes.personal_memory.lookup.v1` | `crypto.py` (HKDF info / AAD) | ciphertext lookup-format compatibility with the `f0f6908` source |
| `plugins.personal-memory*` | logger names in ported modules | log-source parity with the source so operators can correlate logs |
| `telegram:` | `types.py` `external_ref` label shape | record-format compatibility for refs minted by the source |
| `hermes-pm-bench-` | bench runner temp-dir prefix | retained in ported bench code; cosmetic, no functional coupling |
| `PRODUCTION_MARKERS` | bench self-test fixture | fixture strings that intentionally embed production-path markers to assert they never leak into outputs |
| `OPAQUE_SECRET` | `tests/test_validation.py` | synthetic non-credential fixture exercising opaque-secret detection in the ported validation tests; allowlisted so the scanner does not flag its `SECRET = "…"` shape |
| `dropped source env fallback` | this file (File map, `store.py` row) | doc-only mention of the removed `HERMES_HOME`/`F:\Hermes` default inside the sanctioned-delta record; allowlisted phrase, never present in code |

If a later task must retain an additional marker, it is added to this table
and to `tools/hygiene_allowlist.txt` in the same commit.

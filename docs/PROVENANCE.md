# Provenance

Extracted from Hermes `plugins/personal_memory` @ `f0f6908`.

Source snapshot: `origin/main@f0f6908` (read-only). The extraction boundary
is defined in [adr/0002-core-boundary.md](adr/0002-core-boundary.md). Every
deviation from the source is recorded in the table below; the boundary ADR
carries the rationale.

## File map

| File | Source path | Source SHA | Status (ported/adapted/dropped) | Sanctioned deltas |
| --- | --- | --- | --- | --- |

_(Rows are filled by the extraction tasks T2 onwards.)_

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

If a later task must retain an additional marker, it is added to this table
and to `tools/hygiene_allowlist.txt` in the same commit.

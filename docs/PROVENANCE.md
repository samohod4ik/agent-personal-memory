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
| `src/agent_personal_memory/fts_index.py` | `plugins/personal_memory/fts_index.py` | `f0f6908` | ported | none — verbatim full module (T2 had landed only the erasure-side helpers; T3 added `build_claim_fts_index`/`fts_index_present`/`rank_claims_fts` verbatim, so the file is now byte-identical to the source; tenant scope stays a `tenant_id = ?` predicate inside the FTS query plus authorized-candidate post-checks, exactly as source) |
| `src/agent_personal_memory/query.py` | `plugins/personal_memory/query.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/ranking.py` | `plugins/personal_memory/ranking.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/score_locality.py` | `plugins/personal_memory/score_locality.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/confirmation.py` | `plugins/personal_memory/trusted_delete.py` | `f0f6908` | adapted | cuts: (1) all host session resolution removed — `session_binding_from_env`/`session_binding_from_event`/`_lookup_session_id`/`_session_id_from_entry`/`_normalize_platform`/`_normalize_profile`/`_platform_name` plus `_NON_MESSAGING_SURFACES`/`_TELEGRAM_PLATFORMS` — caller passes `SessionBinding` explicitly; (2) host transport removed — `pre_gateway_dispatch`, `_handle_inbound_*`, `_is_trusted_inbound_event`, `_raw_message`/`_raw_author_is_bot`/`_is_forwarded`/`_FORWARD_ATTRS`, `_deliver_text`/`_try_deliver`/`_send_metadata`/`_send_succeeded`/`_finish_delivery`, `_event_command`/`_event_args`, `handle_memory_*_command`/`_COMMAND_REFUSAL`, `_generic_deny_text`/`_cancel_text`, `asyncio`/`inspect`-async delivery — stays in Hermes (replaced by injected `send(text) -> bool` per `open_preview` call, registered via `mark_preview_sent` only on truthy return; a coroutine return is denied as an unverifiable send); (3) default-store removal — `create_delete_preview`/`consume_delete_request` require an explicit `store` argument, no `PersonalMemoryStore()` fallback; (4) `session_binding_from_env` env fallback removed from `create_delete_preview` (explicit param only, `None` keeps the non-authorizing branch); (5) locale helpers vendored verbatim pending the owner-flow port (`SUPPORTED_LANGUAGES`, `_STATE_LABELS`, `_KIND_LABELS`, `_MUTATION_CONFIRM`, `normalize_language`/`format_claim_summary`/`format_mutation_confirmation` as `_normalize_language`/`_format_claim_summary`/`_format_mutation_confirmation`); (6) `_session_required_text` reworded platform-neutrally ("authenticated messaging session" / "подтверждённой сессии"); (7) unused module logger dropped. Kept verbatim: `SessionBinding` (8 fields), `BoundTarget`, `DeleteRequest`, `ConfirmNonce`, `DeleteRegistry` (TTL 600s, `max_items` 64, single-use consume, observer/cross-binding denial, `_expire_locked`/`_request_usable_locked`), `create_request`/`prepare_preview`/`mark_preview_sent`/`consume_confirm`/`cancel`/`peek`/`reset`, `REGISTRY`/`reset_registry`, `reject_legacy_confirm`, `bind_targets`, `_binding_complete`/`_bindings_equal`, `_new_token`, `_canonical_preview_text`/`_preview_heading`/`_open_preview_text` (EN+RU strings byte-identical), command-name constants |
| `docs/adr/0008-physical-erasure-and-tombstones.md` | `docs/adr/0008-physical-erasure-and-tombstones.md` | `f0f6908` | ported | none — verbatim; referenced by the ported erasure test |
| `tests/test_store.py` | `plugins/personal_memory/tests/test_store.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*`; source-file reads go through `store_module.__file__` instead of a plugins-root path |
| `tests/test_crypto.py` | `plugins/personal_memory/tests/test_crypto.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*` |
| `tests/test_erasure.py` | `plugins/personal_memory/tests/test_erasure.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*`; ADR-0008 path resolved against this repo's `docs/adr/`; `patch` target renamed to `agent_personal_memory.store` |
| `tests/test_sources.py` | `plugins/personal_memory/tests/test_sources.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*` |
| `tests/test_validation.py` | `plugins/personal_memory/tests/test_validation.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*`; synthetic `OPAQUE_SECRET` fixture retained (allowlisted) |
| `tests/test_audit.py` | `plugins/personal_memory/tests/test_audit.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*` |
| `tests/test_memory_mode.py` | `plugins/personal_memory/tests/test_memory_mode.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*` |
| `tests/test_lifecycle.py` | `plugins/personal_memory/tests/test_lifecycle.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*`; source-file reads go through `lifecycle_module.__file__`/`store_module.__file__` |
| `tests/test_query.py` | `plugins/personal_memory/tests/test_query.py` | `f0f6908` | adapted | sys.path shim removed; imports rewritten to `agent_personal_memory.*`; `patch` target renamed to `agent_personal_memory.store`; dropped member `test_query_schemas_have_no_tenant_selectors_and_are_not_tools` (imports `personal_memory._TOOLS` tools-module internals and `schemas`, both outside this task's boundary — schemas lands with the API task, `_TOOLS` stays in Hermes); now-unused module-level `schemas` import, `FORBIDDEN_SELECTORS` constant and `_walk` helper removed with it |
| `tests/test_ranking.py` | `plugins/personal_memory/tests/test_ranking.py` | `f0f6908` | adapted | sys.path shim removed; imports rewritten to `agent_personal_memory.*`; dropped member `test_assembler_query_text_reorders_personal_section` (assembler lands with the API/UX task); now-unused module-level `assemble_context` import removed with it |
| `tests/test_score_locality.py` | `plugins/personal_memory/tests/test_score_locality.py` | `f0f6908` | adapted | sys.path shim removed; imports rewritten to `agent_personal_memory.*`; forced delta: `CRITERIA` repointed from `evals/personal-memory-bench/QUALITY_CRITERIA.md` to this repo's `bench/QUALITY_CRITERIA.md` (bench lands in a later task) and `test_quality_criteria_document_covers_cluster_b` carries a `skipUnless(CRITERIA.is_file())` guard so it self-activates once that file exists; all other members verbatim |
| `tests/test_confirmation.py` | `plugins/personal_memory/tests/test_trusted_delete.py` | `f0f6908` | adapted | sys.path shim removed; imports rewritten to `agent_personal_memory.*`; FakeGateway/FakeAdapter/FakeEvent/dispatch seam replaced by an injected `FakeSend` (`send(text) -> bool`, recording payloads, controllable exception/falsy failure); previews driven via `create_delete_preview` directly (source `forget_personal_memory`/`do_not_remember`/`reject_candidate_memory`/`tools`/`register` wrappers land with the owner-flow task); dropped members `test_async_sendresult_success_marks_nonce` (contract requires synchronous `send -> bool`, async returns are denied), `test_preview_send_includes_thread_metadata` (Telegram adapter metadata plumbing), `test_missing_allow_gateway_control_denied`/`test_internal_bot_unauthorized_and_forwarded_events_skip`/`test_real_message_event_and_session_store_binding`/`test_plugin_registration_and_dispatch_seam` (host event-trust, session-store and register() surfaces stay in Hermes), `test_schemas_have_no_destructive_boolean` (schemas module lands with the API task); see T4 notes for the full member map |

### T3 notes

- `query.py`, `ranking.py`, `score_locality.py` and the completed
  `fts_index.py` are byte-identical to `f0f6908` (verified by `diff`).
- Preserved read-layer invariants: the tenant predicate remains a
  `tenant_id = ?` term inside the FTS `MATCH` query (never a post-filter
  alone); `rank_claims`/`rank_claims_fts`/`score_locality`/
  `apply_snippet_acl` keep their source names and signatures; ranking
  sees only authorized candidates; cross-tenant `get_authorized_*`
  denials stay byte-identical `NotFoundError`("Claim not found"/"Source
  not found") with the source's audit row shape.
- Dropped test members (recorded in the file map): `test_query.py` loses
  `test_query_schemas_have_no_tenant_selectors_and_are_not_tools`
  (`_TOOLS`/schemas internals); `test_ranking.py` loses
  `test_assembler_query_text_reorders_personal_section` (assembler not yet
  ported). Both surfaces are re-covered by later tasks' tests.
- Forced delta flagged: the `bench/QUALITY_CRITERIA.md` file that
  `test_score_locality.test_quality_criteria_document_covers_cluster_b`
  asserts on does not exist until the bench task lands, so the member is
  guarded by `skipUnless` rather than deleted; it turns into a real
  assertion automatically once the file is ported.

### T4 notes

- `confirmation.py` keeps the source's two-phase nonce discipline
  bit-for-bit where it matters: `create_delete_preview` never returns a
  nonce; `prepare_preview` mints it; it exists only inside the `send()`
  payload text; `mark_preview_sent` runs only after a truthy send.
- Registry/record internals (`DeleteRegistry`, `DeleteRequest`,
  `ConfirmNonce`, `BoundTarget`, `SessionBinding` 8-field shape,
  `_binding_complete`, `_bindings_equal`, TTL/eviction/consume logic) are
  line-for-line ports; the only behavioural additions are the
  coroutine-return denial in `open_preview` (an `async def` transport
  cannot confirm synchronous delivery — same fail-closed outcome as the
  source's failed-send tests) and the required explicit `store` on
  `create_delete_preview`/`consume_delete_request`.
- Ported test members (semantics preserved):
  `test_valid_delete_after_inbound_preview_and_confirm` →
  `test_valid_delete_after_preview_send_and_confirm`;
  `test_boolean_only_deny_does_not_erase` (tools-level assertions
  deferred — the tools module stays in Hermes for now);
  `test_forged_nonce_denied`; `test_other_actor_denied`;
  `test_other_session_profile_chat_denied` (binding-field mismatches
  incl. session_key/session_id); `test_target_content_changed_denies_batch`;
  `test_fixed_bulk_set_does_not_search_again`;
  `test_expired_request_denied` (now uses the injected
  `DeleteRegistry(monotonic=...)` instead of patching
  `REGISTRY._monotonic`); `test_replay_denied`;
  `test_concurrent_confirmations_one_winner`;
  `test_failed_send_does_not_enable_confirm`;
  `test_failed_sendresult_does_not_enable_confirm`;
  `test_failed_async_sendresult_does_not_enable_confirm` →
  `test_async_send_coroutine_denied`;
  `test_failed_async_sendresult_on_running_loop_does_not_enable_confirm` →
  `test_async_send_coroutine_on_running_loop_denied`;
  `test_cancel_and_session_reset_deny` → `test_cancel_denies_confirm`
  (+ `test_cancel_by_nonce_denies_confirm`).
- Added port-specific members:
  `test_no_binding_returns_non_authorizing_preview`,
  `test_empty_match_preview_registers_nothing`,
  `test_incomplete_binding_denied`, `test_registry_full_denied`,
  `test_consume_requires_explicit_store`,
  `test_create_requires_explicit_store`,
  `test_consume_against_different_store_denied`.

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

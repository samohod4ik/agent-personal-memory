# Provenance

Extracted from Hermes `plugins/personal_memory` @ `f0f6908`.

Source snapshot: `origin/main@f0f6908` (read-only). The extraction boundary
is defined in [adr/0002-core-boundary.md](adr/0002-core-boundary.md). Every
deviation from the source is recorded in the table below; the boundary ADR
carries the rationale.

## File map

| File | Source path | Source SHA | Status (ported/adapted/dropped) | Sanctioned deltas |
| --- | --- | --- | --- | --- |
| `src/agent_personal_memory/types.py` | `plugins/personal_memory/types.py` | `f0f6908` | ported | doc-only delta (PH1-F-06, T11): module docstring reworded host-neutrally — "the personal-memory plugin" → "the agent-personal-memory package"; code verbatim (retains `telegram:` external_ref label shape) |
| `src/agent_personal_memory/lifecycle.py` | `plugins/personal_memory/lifecycle.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/crypto.py` | `plugins/personal_memory/crypto.py` | `f0f6908` | ported | none — verbatim (retains `hermes.personal_memory.lookup.v1` HKDF/AAD info string) |
| `src/agent_personal_memory/store.py` | `plugins/personal_memory/store.py` | `f0f6908` | adapted | `default_data_dir` resolution changed to explicit arg → `PERSONAL_MEMORY_DATA_DIR` → `~/.agent-personal-memory`; dropped source env fallback (`HERMES_HOME` → `F:\Hermes\data\personal-memory`); constructor signature widened to `data_dir: str \| Path \| None`; logger name `plugins.personal-memory` retained verbatim; PH1-F-06 doc-only delta (T11): module docstring "the disabled personal-memory plugin" → "the agent-personal-memory store" and `SchemaVersionError` docstring "this plugin build" → "this package build"; T12 security delta (sanctioned): `explain_claim` now mirrors `get_authorized_claim`'s `restricted`/`expires_at` invisibility with a byte-identical `NotFoundError` denial — an upstream `f0f6908` inconsistency reported to the owner (Hermes NOT modified); `__init__` creates a *new* `data_dir` with mode 0700 (POSIX; a pre-existing user dir is never chmod'd) |
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
| `src/agent_personal_memory/locale.py` | `plugins/personal_memory/locale.py` | `f0f6908` | ported | verbatim except: module docstring reworded host-neutrally; forced delta — EN `remember_candidate` text "Confirm it to inject on later turns." → "Confirm it to use on later turns." (host-terminology removal; RU string already read "использовать"); the T4 vendored copies in `confirmation.py` are deleted and repointed here |
| `src/agent_personal_memory/insight_extract.py` | `plugins/personal_memory/insight_extract.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/export.py` | `plugins/personal_memory/export.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/ux.py` | `plugins/personal_memory/ux.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/mutations.py` | `plugins/personal_memory/mutations.py` | `f0f6908` | adapted | delete previews re-routed `from .trusted_delete` → `from .confirmation` (T4 gate: `create_delete_preview` + `reject_legacy_confirm`, explicit `SessionBinding`); module docstring reworded host-neutrally; `telegram:` external_ref label retained verbatim |
| `src/agent_personal_memory/review.py` | `plugins/personal_memory/review.py` | `f0f6908` | adapted | same `.trusted_delete` → `.confirmation` re-route; docstring reworded |
| `src/agent_personal_memory/learn.py` | `plugins/personal_memory/learn.py` | `f0f6908` | adapted | host hook tail stripped: `render_learn_hook_response` (reads host session env), `main()` stdin/stdout entry, module logger and `sys`/`json`/`logging`/`Mapping` imports removed; library logic (`learn_from_turn`, `remember_personal_insight`, `get_memory_mode`, `set_memory_mode`, `insight_expires_at`, persist/existing-norm helpers) verbatim |
| `src/agent_personal_memory/assembler.py` | `plugins/personal_memory/assembler.py` | `f0f6908` | adapted | travel section stripped: dropped the travel-adapter import chain (`reauthorize_travel_source`), `_TRAVEL_SOURCE_KIND`, `_trip_freshness_label`/`_trip_item`/`_matches_trip_ref`/`_collect_trip_items`, `travel_store`/`trip_ref` params and the `trip` result section; `_FILL_ORDER` is `("personal", "session")`; `assemble_context` keeps its name and remaining signature shape; personal/session sections, budgeting and validation verbatim |
| `src/agent_personal_memory/schemas.py` | `plugins/personal_memory/schemas.py` | `f0f6908` | adapted | all source schema dicts kept verbatim except: module docstring and FORGET/REJECT/DO_NOT_REMEMBER descriptions reworded host-neutrally; `ASSEMBLE_CONTEXT_SCHEMA` drops the `trip_ref` property and the travel-ACL description (trip section removed); `REMEMBER_PERSONAL_INSIGHT_SCHEMA` drops the `travel_profile_update` cross-reference sentence; added `PERSONAL_MEMORY_CONFIRM_SCHEMA` (single `nonce` arg — the named consume surface) and canonical `TOOL_SCHEMAS` name→schema dict (I-SCHEMAS); no schema accepts actor/tenant/user/platform selectors |
| `src/agent_personal_memory/engines/__init__.py` | `plugins/personal_memory/engines/__init__.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/engines/types.py` | `plugins/personal_memory/engines/types.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/engines/native.py` | `plugins/personal_memory/engines/native.py` | `f0f6908` | ported | none — verbatim |
| `src/agent_personal_memory/engines/mem0_shadow.py` | `plugins/personal_memory/engines/mem0_shadow.py` | `f0f6908` | ported | none — verbatim; protocol-only shadow layer (`Mem0ShadowClient` Protocol, injected client), no mem0 network/cloud calls anywhere |
| `src/agent_personal_memory/confirmation.py` | `plugins/personal_memory/trusted_delete.py` | `f0f6908` | adapted | T5 update: vendored locale copies (`SUPPORTED_LANGUAGES`, `_STATE_LABELS`, `_KIND_LABELS`, `_MUTATION_CONFIRM`, `_normalize_language`, `_format_claim_summary`, `_format_mutation_confirmation`, `_fail_language`, `_label`) deleted; module now imports `normalize_language`/`format_claim_summary`/`format_mutation_confirmation` from `.locale` — no behaviour change |
| `src/agent_personal_memory/__init__.py` | `plugins/personal_memory/__init__.py` | `f0f6908` | adapted | rewritten for the standalone surface: exports `PersonalMemoryStore`, `Actor`, read-layer and owner-flow functions, confirm-gate names, engine types and the schema constants/`TOOL_SCHEMAS`; drops `_TOOLS`/`register`/`load_tests`/`inspect_config_rollout`, `handle_*` wrappers, the inject-hook names and the yaml-parsing helpers (host coupling); adds `CONFIRM_REGISTRY` (module REGISTRY alias for `open_preview`/`consume_confirm` access) and `create_candidate` (unbound `PersonalMemoryStore.create_candidate` re-export); keeps `__version__` |
| `tests/test_mutations.py` | `plugins/personal_memory/tests/test_mutations.py` | `f0f6908` | adapted | sys.path shim + env juggling removed; imports rewritten to `agent_personal_memory.*` (schema constants from `.schemas`); dropped `test_register_exposes_mutation_tools`, `test_handle_do_not_remember_omitted_language_is_preview`, `test_tool_handlers_use_session_actor` (register()/tools handlers + session-env coupling stay in Hermes), `test_production_config_enables_personal_memory_toolset` (production config); `_session_env`/`_CaptureCtx` helpers removed with them |
| `tests/test_review.py` | `plugins/personal_memory/tests/test_review.py` | `f0f6908` | adapted | same adaptation; dropped `test_register_exposes_candidate_review_tools`, `test_tool_handlers_use_session_actor`, `test_production_config_enables_personal_memory_toolset` |
| `tests/test_learn.py` | `plugins/personal_memory/tests/test_learn.py` | `f0f6908` | adapted | dropped `test_learn_hook_script_persists_for_session_actor` (host hook script), `test_production_config_keeps_separate_learn_and_inject_hooks` (production config + `inspect_config_rollout`); `test_remember_tool_handler_uses_session_actor` adapted to `test_remember_flow_persists_for_explicit_actor`; `test_default_mode_auto_persists_confirmed_and_inject_sees` → `test_default_mode_auto_persists_confirmed_and_assembly_sees` and `test_dual_actor_isolation` adapted — the host inject-context call is replaced by `assemble_context` confirmed-only personal-section reads via `_personal_contents` helper (same authorized-assembly coverage minus the host text format) |
| `tests/test_export.py` | `plugins/personal_memory/tests/test_export.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*` |
| `tests/test_export_ux.py` | `plugins/personal_memory/tests/test_export_ux.py` | `f0f6908` | adapted | same adaptation; dropped `test_register_exposes_export_tool`, `test_production_config_enables_personal_memory_toolset`; `test_tool_handler_uses_session_actor` adapted to `test_export_flow_uses_explicit_actor` |
| `tests/test_insight_extract.py` | `plugins/personal_memory/tests/test_insight_extract.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.*` |
| `tests/test_assembler.py` | `plugins/personal_memory/tests/test_assembler.py` | `f0f6908` | adapted | rewritten for personal+session sections; dropped `test_removed_member_trip_source_is_omitted`, `test_trip_ref_filters_trip_section`, `test_fetch_limit_applies_after_travel_reauth` (travel-adapter seams) and the `_create_trip`/`_publish_trip` helpers + travel-store fixtures; `test_owner_gets_personal_trip_and_session_sections` → `test_owner_gets_personal_and_session_sections`, `test_cross_tenant_actor_gets_empty_personal_and_trip` → `test_cross_tenant_actor_gets_empty_personal`, budget members adapted to personal+session fill order; patch target renamed to `agent_personal_memory.assembler` |
| `tests/test_mem0_shadow.py` | `plugins/personal_memory/tests/test_mem0_shadow.py` | `f0f6908` | adapted | sys.path shim removed; imports rewritten; dropped `test_register_does_not_add_mem0_tools` (`_TOOLS`/`register` stay in Hermes); FakeMem0Client retained verbatim |
| `tests/test_schemas.py` | `plugins/personal_memory/tests/test_schemas.py` | `f0f6908` | ported | sys.path shim removed; imports rewritten to `agent_personal_memory.schemas` |
| `tests/test_ux.py` | `plugins/personal_memory/tests/test_ux.py` | `f0f6908` | adapted | same adaptation; dropped `test_register_exposes_bilingual_read_explain_tools`, `test_production_config_enables_personal_memory_toolset`; session-env-patched tool-handler members (`test_list_personal_memories_localizes_summaries`, `test_explain_why_remembered_localizes_narrative`, `test_cross_tenant_list_and_explain_fail_closed`, `test_invalid_language_raises_validation_error`) adapted to direct owner-flow calls with the explicit actor |
| `tests/test_api_surface.py` | — (new) | — | added | exercises every `__init__` export: resolves each `__all__` name, inspects callables, asserts `personal_memory_confirm` exists in `TOOL_SCHEMAS` taking only `nonce`, runs one end-to-end owner flow incl. the confirm-gate consume path |
| `tests/test_package_metadata.py` | — (new) | — | added | package metadata smoke test created in T1 (dist name, `__version__`, LICENSE holder); T6 removed its `src/` `sys.path` fallback (PH1-F-05) — the install-dependent members are gated on `_dist_installed()` so a bare checkout reports honest skips and a broken install cannot look green |
| `tests/test_tool_surface.py` | `plugins/personal_memory/tests/test_tools.py` | `f0f6908` | adapted | folds the source `tools.py` coverage onto the standalone API surface (the host `handle_*` wrappers stay in Hermes); see T6 notes for the member map |
| `tests/test_ci_requirements.py` | `plugins/personal_memory/tests/test_ci_requirements.py` | `f0f6908` | adapted | rewritten for the new layout: the source asserted the host CI requirements lock pinned `cryptography==50.0.0` via the plugin requirements file; there is no lock here, so the file became a meta-test — provenance-table completeness, forbidden-import/marker sweep of `src/`, env-var audit, and a host-path scan of tracked files reusing `tools/hygiene_scan.py` machinery; T11 hardening: provenance rows are matched by first-cell/exact backticked file stem (not substring, PH2-F-02), every `git ls-files` entry must have a first-cell row (PH2-F-01), `_REQUIRED_NEW_FILE_ROWS` covers ci.yml/SKILL.md/threat-model/quickstart test, and a meta assert pins the `cryptography>=42` floor in `pyproject.toml` (PH2-F-08); T12 hardening: `_ENV_READ` covers `environ.setdefault/pop`, bare `environ`/`getenv` call forms, aliased module prefixes and any-case env names (PH2-F-05), and the tracked-file host-path check reuses the scanner's `HOST_PATH_PATTERNS` + per-match-span `unsuppressed_hits` so test and scanner cannot diverge; T12 security-review fix: `ScannerPatternTests` pins per-span allowlist anchoring (backticked bare-marker literals, the exact `OPAQUE_SECRET` fixture) and the `chat_id`/`user_id` numeric-id pattern |
| `tools/hygiene_scan.py` | — (new) | — | adapted | repo hygiene scanner (tracked-file scan for host-coupling markers, host paths, secret shapes); created in T1 so the CI scan stays green from PH-2; T12 hardened the pattern set (generic drive-absolute + POSIX-home paths, colon/unquoted/multiplied secret assignments, `telegram:<digits>`, well-known token literals) and switched allowlist suppression from per-line to per-match-span — see T12 notes |
| `tools/hygiene_allowlist.txt` | — (new) | — | adapted | allowlist for the intentional retained markers listed in the table below; T12 rewrote entries so each regex matches the marker text itself (span-overlap suppression) |
| `docs/adr/0002-core-boundary.md` | — (new) | — | added | boundary ADR recording the approved v1 extraction boundary (T1 draft; finalized in T11 with the confirm-gate split decision and the MCP command-mapping note) |
| `docs/PROVENANCE.md` | — (new) | — | added | this file; the file-for-file provenance map to `f0f6908` (T1 draft, completed in T6, finalized in T11) |
| `pyproject.toml` | `plugins/personal_memory/requirements.txt` | `f0f6908` | adapted | packaging metadata replaces the source requirements file; dependency declared as `cryptography>=42` (the source pinned `cryptography==50.0.0`); T9 sets the optional extra pin `mcp>=1.12` — floor raised to `mcp>=1.28.1` in PH-3 r1 (past the CVE-affected 1.x releases) — and adds the `agent-personal-memory-mcp` console script |
| `skills/agent-personal-memory/SKILL.md` | — (new) | — | added | new standalone Agent Skill doc (agentskills.io frontmatter): remember/forget policy, candidate→confirmed lifecycle, privacy do/don'ts, the two-phase confirm rule incl. the MCP operator outbox flow (`<data_dir>/confirmations.jsonl` + stderr → `personal_memory_confirm`), and minimal library/MCP embed examples; all API/tool names mirror `__init__.py`/`TOOL_SCHEMAS` verbatim |
| `LICENSE` | — (new) | — | added | MIT license, holder `samohod4ik` |
| `README.md` | — (new) | — | added | canonical README (T1 stub, T11 final): what-it-is, install, executable quickstart (asserted verbatim by `tests/test_readme_quickstart.py`), API map, MCP section incl. the operator outbox flow, honest differentiation vs Mem0/Letta/basic-memory, security notes |
| `README.ru.md` | — (new) | — | added | faithful RU mirror of README.md (T1 stub, T11 final); the fenced python quickstart block is executed by `tests/test_readme_quickstart.py` |
| `docs/threat-model.md` | — (new) | — | added | threat model (T11): assets, actors, trust boundaries, at-rest-only crypto scope, KEK custody, erasure guarantees vs. backup/OS-copy limits, confirm-gate rationale, supply-chain note, honest limits |
| `tests/test_readme_quickstart.py` | — (new) | — | added | extracts the fenced python quickstart blocks from README.md and README.ru.md and executes them verbatim against temp stores (T11) |
| `.github/workflows/ci.yml` | — (new) | — | added | CI matrix ubuntu+windows × Python 3.10/3.12: `pip install ".[mcp]"`, unittest discover over `tests/` and `bench/`, bench `component --repeats 1`, `tools/hygiene_scan.py` (T8; T9 switched the install step to the `[mcp]` extra — PH3-F-06) |
| `.gitignore` | — (new) | — | added | repo baseline file extended in T1 with extraction ignores (`.delivery/`, `.env*`, `*.db`, `.venv`, `bench/results/`) |
| `src/agent_personal_memory/mcp.py` | — (new) | — | added | new standalone surface, no source counterpart; wraps the `confirmation.py` gate — thin stdio MCP facade binding the startup-provisioned Actor/SessionBinding into every `tools/call` (tool args carry no identity fields; extra keys rejected); destructive tools deliver the nonce-bearing preview to an operator-only outbox `<data_dir>/confirmations.jsonl` + stderr notice via `make_outbox_send`, then `personal_memory_confirm` consumes it against the fixed binding/store; `mcp` SDK imported lazily and adapted across the 1.x decorator and 2.x constructor APIs |
| `tests/test_mcp_stdio.py` | — (new) | — | added | always-on pure-Python members (flag/binding validation, outbox transport, dispatch wiring, preview→outbox→confirm nonce flow, wrong-nonce/binding-mismatch/extra-key denials, failed-send fail-closed); stdio JSON-RPC subprocess members gated on the installed `[mcp]` extra |
| `bench/corpus.json` | `evals/personal-memory-bench/corpus.json` | `f0f6908` | ported | none — verbatim; exactly 10 cases (5 categories × ru/en: acl, deleted, stale, contradictory, bilingual) |
| `bench/cases/abstention_unknown.json` | `evals/personal-memory-bench/cases/abstention_unknown.json` | `f0f6908` | ported | none — verbatim; non-corpus falsifier stub (no `corpus_version`/`cases`, rejected by `load_corpus`) |
| `bench/cases/honest_zero_absent.json` | `evals/personal-memory-bench/cases/honest_zero_absent.json` | `f0f6908` | ported | none — verbatim; non-corpus falsifier stub |
| `bench/cases/pointer_stale.json` | `evals/personal-memory-bench/cases/pointer_stale.json` | `f0f6908` | ported | none — verbatim; non-corpus falsifier stub |
| `bench/cases/rejected_alternatives.json` | `evals/personal-memory-bench/cases/rejected_alternatives.json` | `f0f6908` | ported | none — verbatim; non-corpus falsifier stub |
| `bench/cases/score_locality_falsifier.json` | `evals/personal-memory-bench/cases/score_locality_falsifier.json` | `f0f6908` | ported | none — verbatim; non-corpus falsifier stub |
| `bench/run_bench.py` | `evals/personal-memory-bench/run_bench.py` | `f0f6908` | adapted | plugins-root `sys.path` shim removed (`REPO_ROOT` repointed `HERE.parents[1]` → `HERE.parent`; HERE shim kept so `score` stays importable as a sibling); imports rewired `personal_memory.*` → `agent_personal_memory.*`; module docstring reworded; CLI shape, temp-store discipline, `_require_temp_store_dir` guard and `hermes-pm-bench-` tmp prefix (allowlisted) verbatim |
| `bench/score.py` | `evals/personal-memory-bench/score.py` | `f0f6908` | ported | none — verbatim; the source scorer carries no host coupling |
| `bench/test_bench_runner.py` | `evals/personal-memory-bench/test_bench.py` | `f0f6908` | adapted | renamed (bench/ is its own unittest root); plugins-root `sys.path` shim removed (HERE shim kept for sibling imports); `PRODUCTION_MARKERS` retained verbatim (allowlisted); all members verbatim |
| `bench/README.md` | `evals/personal-memory-bench/README.md` | `f0f6908` | adapted | run/unittest commands repointed `evals/personal-memory-bench/` → `bench/`; intro de-milestoned (M2-01 → package API) and "This PR" → "This suite"; production-path sentence reworded to never name a host path; results path → `bench/results/component/` + gitignore note; `docs/adr/0013` reference dropped (no equivalent ADR in this repo — provenance pointer substituted) |
| `bench/CHECKLIST.md` | `evals/personal-memory-bench/CHECKLIST.md` | `f0f6908` | adapted | title de-namespaced (Alisa plan → `bench/` suite); Related block repointed `evals/…` → `bench/…` and `plugins/personal_memory/score_locality.py` → `src/agent_personal_memory/score_locality.py`; `docs/adr/0017` reference replaced with the `engines/mem0_shadow.py` module pointer; all criteria text verbatim |
| `bench/QUALITY_CRITERIA.md` | `evals/personal-memory-bench/QUALITY_CRITERIA.md` | `f0f6908` | adapted | intro de-hostnamed; Related block: bench README ref repointed to `bench/README.md` with the upstream `evals/personal-memory-bench/README.md` path kept as a provenance mention, `docs/adr/0012`/`0016` replaced by `src/` module refs (those ADRs are not in this repo), score_locality ref repointed to `src/`; "Verified on current main" reworded to the ported package; erase-path note reworded to the standalone confirm-gate (`consume_delete_request` / `personal_memory_confirm`); all criterion text verbatim |

### Source files intentionally not ported

Every `f0f6908` source file with no counterpart in this package is listed
below. Host-coupled modules and their tests stay in Hermes; the boundary
rationale is in [adr/0002-core-boundary.md](adr/0002-core-boundary.md).

| Source path | Status | Reason |
| --- | --- | --- |
| `plugins/personal_memory/tools.py` | not-ported | host `handle_*` wrappers that derive the Actor from the host session context; coverage folded into `tests/test_tool_surface.py` against the standalone API — the wrappers themselves stay in Hermes |
| `plugins/personal_memory/inject.py` | not-ported | session-context injection hook; stays in Hermes |
| `plugins/personal_memory/miniapp_contract.py` | not-ported | mini app contract layer; stays in Hermes |
| `plugins/personal_memory/miniapp_handlers.py` | not-ported | mini app HTTP handlers; stays in Hermes |
| `plugins/personal_memory/night_cycle1_tool_descriptions.py` | not-ported | night-cycle tool descriptions; stays in Hermes |
| `plugins/personal_memory/peer_import.py` | not-ported | peer-memory import; stays in Hermes |
| `plugins/personal_memory/adapters/` (`travel.py`, `travel_compute_bridge.py`) | not-ported | travel-journal adapters; stays in Hermes |
| `plugins/personal_memory/plugin.yaml` | not-ported | host plugin manifest; stays in Hermes |
| `plugins/personal_memory/spikes/` | not-ported | sqlcipher compatibility spike; stays in Hermes |
| `plugins/personal_memory/tests/test_plugin_load.py` | not-ported | plugin `register()`/manifest loading; stays in Hermes |
| `plugins/personal_memory/tests/test_disabled_rollout.py` | not-ported | rollout-flag gating; stays in Hermes |
| `plugins/personal_memory/tests/test_cycle1_tool_descriptions.py` | not-ported | night-cycle tool descriptions; stays in Hermes |
| `plugins/personal_memory/tests/test_miniapp_api.py` | not-ported | mini app API; stays in Hermes |
| `plugins/personal_memory/tests/test_miniapp_contract.py` | not-ported | mini app contract; stays in Hermes |
| `plugins/personal_memory/tests/test_miniapp_handlers.py` | not-ported | mini app handlers; stays in Hermes |
| `plugins/personal_memory/tests/test_travel_adapter.py` | not-ported | travel adapter; stays in Hermes |
| `plugins/personal_memory/tests/test_travel_compute_bridge.py` | not-ported | travel compute bridge; stays in Hermes |
| `plugins/personal_memory/tests/test_r1_inject_acl.py` | not-ported | inject-ACL R1 test; stays in Hermes |
| `plugins/personal_memory/tests/test_inject.py` | not-ported | inject hook coverage; stays in Hermes |
| `plugins/personal_memory/tests/test_live_handler_payloads.py` | not-ported | live handler payload shapes; stays in Hermes |
| `plugins/personal_memory/tests/spikes/test_sqlcipher_spike.py` | not-ported | sqlcipher spike fixture; stays in Hermes |

### T7 notes

- `bench/` is its own test root (`python -m unittest discover -s bench`);
  no `__init__.py` needed — discovery puts `bench/` on `sys.path` and the
  HERE shim in `run_bench.py`/`test_bench_runner.py` covers direct-script
  invocation, exactly as the source eval did.
- `corpus.json` and `cases/*.json` verified byte-identical to `f0f6908`
  (`diff` clean); the corpus holds exactly 10 cases and `load_corpus`
  rejects every `cases/` stub, so the falsifier pack stays outside the
  10-case denominator.
- `run_bench.py` deltas vs source: three removed lines (the `PLUGINS_ROOT`
  constant plus its `if`-guard/`sys.path.insert` pair — PH2-F-07 corrected
  the count), one repointed constant (`REPO_ROOT`), four rewired import
  lines, one reworded docstring line. All seeding, query, scoring, CLI and
  output logic is verbatim; `component --repeats 1` yields 10/10.
- With `bench/QUALITY_CRITERIA.md` present, the `skipUnless` guard in
  `tests/test_score_locality.py` self-activated: the member now runs and
  passes, asserting on the upstream README provenance mention retained
  in the Related block.

### T6 notes

- `tests/test_tool_surface.py` member map vs source
  `plugins/personal_memory/tests/test_tools.py` (2 source members):
  `test_tools_use_session_actor_and_ignore_tenant_selectors` →
  `test_owner_tools_are_bound_to_actor_not_callable_selectors` (fixture and
  payload assertions preserved verbatim) plus
  `test_owner_flows_take_explicit_actor_without_selector_params`,
  `test_selector_kwargs_are_unpassable` and
  `test_tool_schemas_have_no_identity_selectors` — the source proved forged
  tenant/user/platform kwargs were ignored; the standalone signatures and
  schemas make them unpassable, a strictly stronger guarantee.
  `test_toolset_is_personal_memory_and_enabled_in_telegram` →
  `test_source_tool_names_have_schema_and_callable` +
  `test_every_tool_schema_maps_to_public_surface` for the surface-name
  coverage; its `register()`/`config.yaml` assertions stay in Hermes with
  the plugin registry.
- New tool-surface members with no source counterpart:
  `test_destructive_schemas_have_no_confirm_boolean` (absorbs the
  `test_schemas_have_no_destructive_boolean` intent dropped from
  `test_confirmation.py` in T4),
  `test_confirm_schema_takes_only_nonce_and_matches_consume_path`,
  `test_destructive_tools_produce_previews_only`,
  `test_two_phase_confirm_executes_bound_erasure`,
  `test_failed_send_enables_no_nonce` — they pin the I-SCHEMAS +
  two-phase-confirm contract the source covered through
  `test_trusted_delete.py` members.
- `tests/test_ci_requirements.py` is an adaptation-in-name only: the
  source file checked `requirements-ci.in`/`.txt` pins; the dependency
  floor `cryptography>=42` lives in `pyproject.toml` and the file now
  guards the extraction layout instead (provenance completeness, `src/`
  hygiene, tracked-file host-path scan).
- PH1-F-05 fix: `tests/test_package_metadata.py` no longer falls back to
  a `src/` `sys.path` shim; the three install-dependent members are gated
  on `_dist_installed()`.
- PH1-F-04 fix: file-map rows added for all new tracked files (status
  `added`) and the not-ported source inventory is now explicit in the
  table above.

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

### T5 notes

- `insight_extract.py`, `export.py`, `ux.py`, `engines/*` are byte-identical
  to `f0f6908` (verified by `diff`). `locale.py`, `learn.py`,
  `mutations.py`, `review.py`, `assembler.py`, `schemas.py` differ only by
  the sanctioned deltas in the file map.
- Forced delta flagged: `locale.py` EN `remember_candidate` confirmation
  reworded "Confirm it to inject on later turns." → "Confirm it to use on
  later turns." — the task boundary requires zero host-hook terminology in
  new modules; the RU sibling string already used the neutral wording.
- `confirmation.py` no longer carries vendored locale copies; it imports
  the three helpers from `.locale`. No behaviour change — the vendored
  code was a verbatim copy of the same source tables.
- `mem0_shadow.py` stays a pure protocol/shadow layer: `Mem0ShadowClient`
  is a `Protocol` satisfied by host-injected clients; the module performs
  no mem0 network or cloud calls (verified: zero `import mem0`, no HTTP).
- `assembler.py` keeps personal+session semantics exactly: same
  `_personal_item`/`_session_item` shapes, same token/record budgeting;
  fill order is `("personal", "session")`. The trip section and its
  reauthorization seam are out of scope for this package.
- `schemas.py` adds `PERSONAL_MEMORY_CONFIRM_SCHEMA` (the named consume
  surface for the confirm-gate — single `nonce` argument) and the
  canonical `TOOL_SCHEMAS` mapping for facades/MCP `tools/list`. The
  schema description prose was reworded host-neutrally where it named the
  source host platform; the `/memory_delete <request-id>` literal is kept
  because `create_delete_preview` emits it verbatim in `delete_command`.
- `__init__.py` drops the host plugin surface: `register`, `_TOOLS`,
  `handle_*` wrappers, the session-env inject helpers, `load_tests` and
  `inspect_config_rollout` stay in the source repo. Added standalone-only
  names: `CONFIRM_REGISTRY` (module `REGISTRY` alias), `create_candidate`
  (unbound `PersonalMemoryStore.create_candidate` re-export),
  `PERSONAL_MEMORY_CONFIRM_SCHEMA`, `TOOL_SCHEMAS`.
- Dropped test members by name (all recorded per-file above): register()
  exposure checks ×4, production `config.yaml` checks ×4, session-env tool
  handler checks ×4, host-hook script/config checks ×2 (test_learn),
  `_TOOLS`/register mem0 check ×1 (test_mem0_shadow), travel-section
  members ×3 (test_assembler). Adapted (renamed) members: 2 in
  test_learn, 1 in test_export_ux, 4 in test_ux, 3 in test_assembler.

### T9 notes

- `mcp.py` is new code with no `f0f6908` counterpart: it only wires the
  canonical `TOOL_SCHEMAS` surface to the owner-flow API. Zero memory logic
  is duplicated — dispatch is `getattr(agent_personal_memory, <tool>)` with
  `actor`/`store`/`session_binding` bound from startup config.
- The five binding flags (`--actor-platform`, `--actor-user-id`,
  `--session-key`, `--session-id`, `--chat-id`) are required: `build_binding`
  validates non-empty values plus the gate's own `_binding_complete` rule, so
  a missing or placeholder binding fails at startup (exit 2), never silently.
- Confirm-gate transport over MCP folds the source's "user opens the preview
  via `/memory_delete <request-id>`" step into the preview tool call: after
  `create_delete_preview` the facade immediately calls
  `CONFIRM_REGISTRY.open_preview(request_id, binding, send=...)` with an
  outbox transport appending `{"ts", "request_id", "body"}` lines to
  `<data_dir>/confirmations.jsonl` and printing a nonce-free notice to
  stderr. Tool results are scrubbed of nonce-shaped keys defensively; the
  nonce can only reach `personal_memory_confirm` via the operator reading
  the outbox, preserving the two-phase guarantee end to end.
- `mcp` stays an optional extra (`mcp>=1.28.1` — floor raised in the PH-3
  r1 fix past the CVE-affected 1.x releases; PH3-R2-01 corrected the stale
  `>=1.12` mention here). `build_sdk_server` adapts to
  both SDK generations: 1.x decorator registration
  (`@server.list_tools()`/`@server.call_tool()`, exceptions → isError) and
  2.x constructor handlers (`on_list_tools`/`on_call_tool` returning
  `ListToolsResult`/`CallToolResult`). Without the extra,
  `python -m agent_personal_memory.mcp` prints a pip-install hint to stderr
  and exits 2; `--help` works without it.
- CI install step now uses `pip install ".[mcp]"` so the stdio subprocess
  members run for real (the workflow comment anticipated the T9 extra).
- `tests/test_mcp_stdio.py` covers: flag/binding validation, JSONL outbox
  shape `{ts, request_id?, body}`, nonce never in tool results or stderr,
  remember→list→forget→outbox→confirm erasure, wrong-nonce and
  binding-mismatch denials (mismatched consume does not burn the nonce),
  extra/identity-field rejection, unknown/missing-arg/non-dict rejection,
  failed-send fail-closed behaviour, and `python -m` startup refusal.

### PH-3 review r1 fix notes

- PH3-F-01: `_normalize_payload` is now recursive — decrypted claim
  `content`/source `snapshot` bytes nested inside result dicts/lists decode
  to utf-8 text with a `{"encoding": "base64", "data": ...}` fallback for
  non-utf-8 blobs (same shape as `store._export_bytes`); top-level bytes
  remain the `export_owner_data` utf-8 JSON document. New
  `tests/test_mcp_stdio.py` members exercise `rank_claims`,
  `list_personal_memories(include_content=True)` and a raw non-utf-8
  `query_sources(include_payload=True)` snapshot in-process, plus
  `rank_claims`/`include_content` over the real stdio subprocess.
- PH3-F-02: `_validate_arguments` normalizes `None`→`{}` and still runs the
  required/extra-field checks, so in-process callers get `ValidationError`
  instead of a bare `KeyError`/`TypeError` from the dispatch target.
- PH3-F-03: `tests/test_mcp_stdio.py` `_stderr_text` now polls drained
  stderr for the expected substring until a deadline instead of returning
  on the first line, so the startup banner cannot mask the outbox notice.
- PH3-F-04: verbatim-text seam recorded, not altered — preview
  `confirmation`/`delete_command` fields and the outbox `body` still
  mention the source host's `/memory_delete <request_id>` and
  `/memory_cancel <request_id>` commands, which do not exist on the MCP
  surface; the mapping is documented in
  `skills/agent-personal-memory/SKILL.md` ("Command mapping under MCP").
- PH3-F-05: the `mcp` extra floor is `mcp>=1.28.1`. The green CI run on
  e828fe7 resolved `mcp==2.3.0`, so the evidence-backed adaptation is the
  2.x constructor-handler branch (`on_list_tools=`/`on_call_tool=`); the
  1.x decorator branch remains statically-written fallback code.
- PH3-F-06: plan-fidelity record — `.github/workflows/ci.yml` (the
  `pip install ".[mcp]"` install step) was modified in T9 outside its
  declared modify paths (`pyproject.toml`, `docs/PROVENANCE.md`); noted
  here, no code change required.

### T11 notes

- New files: `README.md`/`README.ru.md` final content,
  `docs/threat-model.md`, `tests/test_readme_quickstart.py` — all carry
  `added` rows in the file map above.
- PH1-F-06 resolved: the host-era "plugin" docstrings in `store.py`
  (module + `SchemaVersionError`) and `types.py` (module) were reworded to
  standalone-package wording. Doc-only delta, recorded per-file above; the
  retained `plugins.personal-memory` logger name is untouched (intentional
  marker, see the table below).
- PH2-F-01 resolved: file-map rows added for `.github/workflows/ci.yml`,
  `docs/threat-model.md` and `tests/test_readme_quickstart.py`
  (`skills/agent-personal-memory/SKILL.md` already had one), and the
  meta-test now requires a first-cell row for **every** `git ls-files`
  entry, so future omissions fail CI instead of drifting.
- PH2-F-02 resolved: `test_ci_requirements.py` matches provenance rows by
  first-cell path and by exact backticked `*.py` stem — `test_export` can
  no longer be satisfied by `test_export_ux` text, and a path mentioned in
  T-notes prose no longer counts as a row.
- PH2-F-07 resolved: the T7 note now says three removed lines
  (`PLUGINS_ROOT` + `if` + `sys.path.insert`), matching the actual diff.
- PH2-F-08 resolved: `PackagingFloorTests` asserts the `cryptography>=42`
  floor in `pyproject.toml`.
- PH3-R2-01 resolved: the stale `mcp>=1.12` mention in the T9 notes now
  reads `mcp>=1.28.1`, matching `pyproject.toml`.

### T12 notes

Scanner hardening carried forward from the PH-1/PH-2 reviews; all changes
are in `tools/hygiene_scan.py`, `tools/hygiene_allowlist.txt` and
`tests/test_ci_requirements.py`:

- PH1-F-01 / PH2-F-03 resolved: the three drive-specific host-path
  patterns (the former `F:`-/`G:`-/`C:`-prefixed literals) are replaced by
  a generic drive-absolute pattern `(?<![\w])[A-Za-z]:[\\/]` — any drive letter,
  either separator, any case; the `(?<![\w])` lookbehind keeps URI
  schemes (`https://`, `file://`) unflagged. A POSIX user-home pattern
  (`/(home|Users)/`) rides along. Both live in the exported
  `HOST_PATH_PATTERNS` so the meta-test enforces the identical family.
- PH1-F-02 resolved: secret detection now covers colon/`=>` assignment
  (`name: "…"`/`name = "…"`), unquoted `NAME=value` (requires a
  digit-bearing value so `name = generate()` stays clean),
  short-string-multiplied shapes (`name = "<ch>" * <count>`), and
  well-known token literals (`sk-…`,
  `AKIA…`/`ASIA…`, `Bearer …`, `ghp_…`/`gho_…`/…, `github_pat_…`,
  `xox…-…`, `AIza…`, `-----BEGIN … PRIVATE KEY-----`, JWT `eyJ…` triples).
  A `telegram:<digits>` pattern flags real Telegram ids while the
  allowlisted `telegram:{label}:{digest}` label shape stays clean.
- PH1-F-03 / PH2-F-04 resolved: allowlist suppression is per match span —
  a hit survives unless an allowlist regex's match on the same line
  overlaps the hit's span (`unsuppressed_hits`). Every allowlist entry was
  rewritten to match the marker text itself; doc-only markers are scoped
  to backticked notation, and the synthetic validator fixtures are
  allowlisted by exact literal (the bare quoted RSA PEM header only —
  a PEM with key material stays flagged).
- PH2-F-05 resolved: `_ENV_READ` in `tests/test_ci_requirements.py` now
  covers `environ.setdefault`/`pop`, bare `environ[...]`/`environ.get()`/
  `getenv()` call forms (`from os import …`), aliased `<module>.` prefixes
  and any-case env names.
- The tracked-file host-path meta-test now calls the scanner's own
  `HOST_PATH_PATTERNS` + `unsuppressed_hits`, so scanner and test cannot
  diverge (the old local copy under-matched backslash drive paths).
- No real leaks surfaced while widening the patterns: the only new hits
  were the intentional `MARKER_SECRETS`/`embedded_tokens`/`PRODUCTION_MARKERS`
  fixtures and the documented `F:\Hermes`/`HERMES_HOME` doc mentions,
  all re-allowlisted at span granularity.

### T12 security-review fix notes

Follow-up fixes for the PH-4 T12 security review (1 Important, 5 Minor;
no source-code port regressions found):

- F1 (outbox custody): `mcp.py` `make_outbox_send` now appends via
  `os.open(O_CREAT|O_WRONLY|O_APPEND, 0o600)` + `os.fdopen`, with a
  best-effort `os.chmod(path, 0o600)` before and after the write so a
  pre-existing loose `confirmations.jsonl` is tightened (the chmod is
  tolerated to no-op where the filesystem ignores POSIX modes). The
  outbox parent dir is created via `makedirs(mode=0o700)`, and
  `store.py` `__init__` creates a *new* `data_dir` with mode 0700 so the
  db/wal/outbox inherit owner-only custody — a pre-existing user dir is
  never chmod'd. This matches the 0600-equivalent custody that
  `docs/threat-model.md` already claimed.
- F2 (MCP error leakage): `_error_text` whitelists known exception types
  (`ValidationError`, `NotFoundError`, `ConflictError`, `ForbiddenError`,
  `TrustedDeleteDenied`, `ValueError`, `KeyError` — subclasses included
  via an MRO walk) to `TypeName: message`; any other exception collapses
  to `InternalError: operation failed` so host paths and internals never
  reach a tool result. Both SDK branches (1.x decorator → raised
  `RuntimeError`, 2.x constructor → `CallToolResult(isError)`) route
  through it.
- F3 (sanctioned upstream delta): `store.py` `explain_claim` now mirrors
  `get_authorized_claim`'s `restricted` + `expires_at` invisibility with
  a byte-identical `NotFoundError("Claim not found")` denial — at
  `f0f6908` the explain path exposed restricted/expired claim metadata
  that the authorized-read path hides. Recorded on the `store.py`
  file-map row; **reported upstream to the owner — Hermes NOT
  modified.**
- F4 (implicit-store foot-gun): documented in `README.md`,
  `README.ru.md` and `skills/agent-personal-memory/SKILL.md` — public
  owner flows accept `store=None`, which opens/creates the default data
  dir; embedded use should always pass an explicit store. No API change.
- F5 (allowlist tightening): the single over-broad backtick-span entry
  was split into per-literal anchored entries (` `HERMES_HOME` `,
  ` `F:\Hermes` ` and each not-ported module/path name), and
  `OPAQUE_SECRET` is anchored to the exact fixture assignment — a
  backtick span mixing a marker with a real secret still flags.
- F6 (scanner coverage): added a `(chat_id|user_id)` numeric-id
  assignment pattern anchored to the key names so `version`/`port`/
  `max_limit` stay unflagged, plus a "Known limits" block in the
  `hygiene_scan.py` docstring (str-concatenated ids, opaque service
  handles, NUL-seeded text — independent review is the backstop).
- Regression coverage: `tests/test_mcp_stdio.py` (outbox created 0600 /
  pre-existing loose file tightened, POSIX-mode asserts with a Windows
  write-path fallback; `_error_text` whitelist + collapse; SDK
  wire-level internal-error text), `tests/test_store.py` (created
  `data_dir` is 0700 on POSIX), `tests/test_export.py`
  (restricted/expired `explain_claim` parity with
  `get_authorized_claim`), `tests/test_ci_requirements.py`
  (`ScannerPatternTests` pinning the allowlist anchoring and the new
  numeric-id pattern).

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

Since T12, allowlist suppression is **per match span**: an allowlist regex
must match text that overlaps the scan hit itself (a phrase elsewhere on
the same line suppresses nothing), so each entry below names the marker
text — doc-only markers are scoped to their backticked notation.

| Marker | Location | Rationale |
| --- | --- | --- |
| `hermes.personal_memory.lookup.v1` | `crypto.py` (HKDF info / AAD) | ciphertext lookup-format compatibility with the `f0f6908` source |
| `plugins.personal-memory*` | logger names in ported modules | log-source parity with the source so operators can correlate logs |
| `telegram:` (non-numeric) | `types.py`/`mutations.py` `external_ref` label shape `telegram:{label}:{digest}` | record-format compatibility for refs minted by the source; `telegram:<digits>` stays flagged |
| `hermes-pm-bench-` | bench runner temp-dir prefix | retained in ported bench code; cosmetic, no functional coupling |
| `hermes-pm-*` | `tests/` `TemporaryDirectory` prefixes | verbatim source test-fixture strings; cosmetic marker, not a scan hit |
| `PRODUCTION_MARKERS = (…)` | bench self-test fixture | tuple definition embeds production-path marker strings to assert they never leak into outputs |
| `OPAQUE_SECRET` | `tests/test_validation.py` | synthetic non-credential fixture exercising opaque-secret detection in the ported validation tests; allowlisted so the scanner does not flag its `SECRET = "…"` shape |
| `MARKER_SECRETS` / `embedded_tokens` literals (`AKIAIOSFODNN7EXAMPLE`, `Bearer ya29.…`, the jwt.io example token, the bare quoted RSA PEM header) | `tests/test_validation.py` | well-known documentation example values reused verbatim as synthetic validator fixtures, allowlisted by exact literal so real credentials of the same shapes stay flagged |
| backticked `HERMES_HOME`, `F:\Hermes` and not-ported module names (`pre_gateway_dispatch`, `miniapp_*`, `night_cycle1…`, `peer_import`, `travel_compute`, `plugin.yaml`, `spikes`) | this file (File map) and `adr/0002-core-boundary.md` | doc-only mentions, inside backticks, of the removed `HERMES_HOME`/host-path default and of modules that stay in Hermes; allowlisted per backticked span — bare unbackticked occurrences stay flagged |

If a later task must retain an additional marker, it is added to this table
and to `tools/hygiene_allowlist.txt` in the same commit.

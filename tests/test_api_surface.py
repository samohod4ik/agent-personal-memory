"""Public API surface smoke test for agent_personal_memory.

Imports every name exported by the package ``__init__`` and calls or inspects
each once, then drives one end-to-end owner flow (remember -> list -> explain
-> assemble -> preview -> open_preview -> consume) against a temp store.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

import agent_personal_memory as apm
from agent_personal_memory import schemas

# Reconcile/history public surface (issue #2/#3): the five owner-flow
# tool functions plus the proposal/judge names the package re-exports.
RECONCILE_SURFACE_NAMES = (
    "list_memory_reconciliations",
    "accept_memory_reconciliation",
    "reject_memory_reconciliation",
    "personal_memory_history",
    "retract_personal_memory",
    "propose_reconciliation",
    "propose_reconciliations",
    "ConflictJudge",
    "SlotRuleJudge",
    "Relation",
)


class ApiSurfaceTests(unittest.TestCase):
    def test_every_exported_name_resolves(self) -> None:
        self.assertTrue(apm.__all__)
        for name in apm.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(apm, name), name)
                self.assertIsNotNone(getattr(apm, name))

    def test_callables_have_signatures(self) -> None:
        for name in apm.__all__:
            value = getattr(apm, name)
            if callable(value):
                with self.subTest(name=name):
                    try:
                        inspect.signature(value)
                    except (TypeError, ValueError):
                        # builtin exception types have no signature
                        self.assertTrue(issubclass(value, BaseException))

    def test_version_attribute(self) -> None:
        self.assertTrue(apm.__version__)

    def test_reconcile_and_history_surface_names(self) -> None:
        for name in RECONCILE_SURFACE_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, apm.__all__)
                self.assertTrue(hasattr(apm, name))
                self.assertIsNotNone(getattr(apm, name))

    def test_personal_memory_confirm_schema_takes_only_nonce(self) -> None:
        self.assertIn("personal_memory_confirm", schemas.TOOL_SCHEMAS)
        schema = schemas.TOOL_SCHEMAS["personal_memory_confirm"]
        self.assertIs(schema, schemas.PERSONAL_MEMORY_CONFIRM_SCHEMA)
        self.assertEqual(schema["name"], "personal_memory_confirm")
        props = schema["parameters"]["properties"]
        self.assertEqual(set(props), {"nonce"})
        self.assertEqual(schema["parameters"]["required"], ["nonce"])

    def test_tool_schemas_have_no_identity_selectors(self) -> None:
        forbidden_keys = ("tenant_id", "tenant", "user_id", "platform", "actor", "principal")
        for name, schema in schemas.TOOL_SCHEMAS.items():
            params = schema["parameters"]
            keys = set(params.get("properties") or {})
            keys.update(params.get("required") or [])
            for selector in forbidden_keys:
                self.assertNotIn(selector, keys, f"{name}: {selector}")
            packed = json.dumps(schema, ensure_ascii=False).lower()
            self.assertNotIn("tenant_id", packed, name)


class ApiSurfaceFlowTests(unittest.TestCase):
    """Call each exported owner-flow function once against a temp store."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-api-surface-")
        self.data_dir = Path(self.tmp.name)
        self.actor = apm.Actor(platform="t", user_id="api-1", display_name="")
        self.store = apm.PersonalMemoryStore(data_dir=self.data_dir)
        self.sent: list[str] = []

    def tearDown(self) -> None:
        apm.reset_registry()
        self.tmp.cleanup()

    def _send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def _nonce(self) -> str:
        for line in self.sent[-1].splitlines():
            if "/memory_confirm" in line:
                return line.split()[-1]
        raise AssertionError("nonce missing from delivered preview")

    def _binding(self) -> apm.SessionBinding:
        return apm.SessionBinding(
            platform=self.actor.platform,
            user_id=self.actor.user_id,
            session_key="agent:test:dm",
            session_id="sess-1",
            chat_id="chat-1",
        )

    def test_owner_flow_end_to_end(self) -> None:
        saved = apm.remember_personal_insight(
            self.actor, "User prefers window seats.", store=self.store
        )
        claim_id = str(saved["claim_id"])
        # The old call shape (no judge= kwarg) still works and always
        # reports the reconcile proposals it created.
        self.assertEqual(saved["reconciliations"], [])

        self.assertEqual(
            apm.get_memory_mode(self.actor, store=self.store)["mode"], "auto_insight"
        )
        self.assertEqual(
            apm.set_memory_mode(self.actor, "auto_insight", store=self.store)["mode"],
            "auto_insight",
        )
        learned = apm.learn_from_turn(self.actor, "I like quiet hotels.", store=self.store)
        self.assertTrue(learned["persisted"])

        listed = apm.list_personal_memories(self.actor, store=self.store)
        self.assertGreaterEqual(listed["count"], 1)
        explained = apm.explain_why_remembered(self.actor, claim_id, store=self.store)
        self.assertIn("narrative", explained)
        raw_explain = apm.explain_claim(self.actor, claim_id, store=self.store)
        self.assertEqual(raw_explain["claim_id"], claim_id)
        exported = apm.export_personal_memory(self.actor, store=self.store)
        self.assertIn("export", exported)
        raw_export = apm.export_owner_data(self.actor, store=self.store)
        self.assertIsInstance(raw_export, (bytes, bytearray))

        ranked = apm.rank_claims(self.actor, "window seats", store=self.store)
        self.assertTrue(ranked)
        ranked_fts = apm.rank_claims_fts(self.actor, "window seats", store=self.store)
        self.assertTrue(ranked_fts)
        built = apm.build_claim_fts_index(self.actor, store=self.store)
        self.assertGreaterEqual(built["indexed_claims"], 1)
        self.assertTrue(apm.fts_index_present(store=self.store))
        tenant = self.store.ensure_principal(self.actor)
        locality = apm.score_locality(
            [
                {
                    "claim_id": "fixture-hit",
                    "tenant_id": tenant,
                    "text": "window seats",
                    "snippet": "window",
                }
            ],
            self.actor,
            {"query_text": "window seats"},
            store=self.store,
        )
        self.assertIsInstance(locality, list)
        self.assertEqual(locality[0]["claim_id"], "fixture-hit")

        rows = apm.query_claims(self.actor, store=self.store)
        self.assertTrue(rows)
        sources = apm.query_sources(self.actor, include_payload=False, store=self.store)
        self.assertTrue(sources)
        self.assertEqual(
            apm.get_authorized_claim(self.actor, claim_id, store=self.store)["claim_id"],
            claim_id,
        )
        self.assertEqual(
            apm.get_authorized_source(
                self.actor, str(sources[0]["source_id"]), store=self.store
            )["source_id"],
            str(sources[0]["source_id"]),
        )

        candidates = apm.list_candidate_memories(self.actor, store=self.store)
        self.assertIn("items", candidates)
        assembled = apm.assemble_context(
            self.actor,
            memory_store=self.store,
            session_items=[{"role": "user", "text": "hi"}],
            query_text="window",
        )
        self.assertIn("personal", assembled)
        self.assertIn("session", assembled)

        corrected = apm.correct_personal_memory(
            self.actor, claim_id, "User prefers aisle seats.", store=self.store
        )
        # remember auto-confirm already produced v2; the correction is v3.
        self.assertEqual(corrected["version"], 3)

        created = apm.create_candidate(
            self.store,
            self.actor,
            claim_kind="preference",
            content=b"candidate for review",
            confidence=0.5,
            explanation="api surface",
            sensitivity="normal",
            source_ids=[str(sources[0]["source_id"])],
            expires_at="2099-01-01T00:00:00Z",
        )
        confirmed = apm.confirm_candidate_memory(
            self.actor, str(created["claim_id"]), store=self.store
        )
        self.assertEqual(confirmed["state"], "confirmed")

        second = apm.remember_personal_insight(
            self.actor, "User drinks green tea.", store=self.store
        )
        rejected = apm.create_candidate(
            self.store,
            self.actor,
            claim_kind="preference",
            content=b"candidate to reject",
            confidence=0.4,
            explanation="api surface",
            sensitivity="normal",
            source_ids=[str(sources[0]["source_id"])],
            expires_at="2099-01-01T00:00:00Z",
        )
        preview = apm.reject_candidate_memory(
            self.actor,
            str(rejected["claim_id"]),
            session_binding=self._binding(),
            store=self.store,
        )
        self.assertTrue(preview["preview"])
        apm.CONFIRM_REGISTRY.open_preview(
            str(preview["request_id"]), self._binding(), send=self._send
        )
        record = apm.CONFIRM_REGISTRY.consume_confirm(self._nonce(), self._binding())
        erased = apm.consume_delete_request(record, store=self.store)
        self.assertFalse(erased["preview"])

        forget_preview = apm.forget_personal_memory(self.actor, claim_id, store=self.store)
        self.assertTrue(forget_preview["preview"])
        dnr_preview = apm.do_not_remember(self.actor, text="green tea", store=self.store)
        self.assertTrue(dnr_preview["preview"])
        apm.reject_legacy_confirm(False)
        with self.assertRaises(apm.TrustedDeleteDenied):
            apm.reject_legacy_confirm(True)

    def test_engines_and_gate_types(self) -> None:
        engine = apm.NativeMemoryEngine(store=self.store)
        result, records = engine.retrieve(
            apm.RetrievalRequest(self.actor, "nothing yet", limit=5)
        )
        self.assertEqual(result.engine, "native")
        self.assertEqual(records, [])
        adapter = apm.Mem0Adapter(store=self.store)
        degraded, _ = adapter.retrieve(
            apm.RetrievalRequest(self.actor, "nothing yet", limit=5)
        )
        self.assertTrue(degraded.degraded)
        _extract_result, candidates = engine.extract_candidates(
            apm.ExtractionRequest(
                self.actor, text="I prefer aisle seats.", source_ids=("s1",)
            )
        )
        self.assertTrue(candidates)
        self.assertIsInstance(candidates[0], apm.Candidate)
        self.assertIsInstance(candidates[0].content, bytes)
        self.assertFalse(apm.purge_shadow_projection(self.actor, client=None))
        created = apm.persist_shadow_candidates(self.actor, [], store=self.store)
        self.assertEqual(created, [])

        binding = self._binding()
        self.assertIsInstance(binding.actor(), apm.Actor)
        registry = apm.DeleteRegistry()
        self.assertIsInstance(registry, apm.DeleteRegistry)
        target = apm.BoundTarget(
            claim_id="c", version=1, claim_version_id="v", content_hash="h"
        )
        self.assertIn("content_hash", target.as_dict())
        self.assertIsInstance(apm.CONFIRM_REGISTRY, apm.DeleteRegistry)

        self.assertIn("aisle", apm.extract_insights("I prefer aisle seats.")[0].content)
        self.assertTrue(apm.insight_content_allowed("I prefer aisle seats."))
        self.assertFalse(apm.turn_has_blocked_secret("hello world"))
        self.assertEqual(apm.normalize_insight_text("  Hello, World!  "), "hello, world")
        self.assertIsInstance(apm.ExtractedInsight, type)
        self.assertTrue(apm.bm25_scores("window", ["window seats", "tea"])[0] >= 0.0)
        self.assertEqual(apm.apply_snippet_acl("allowed", "allowed"), "allowed")
        self.assertIn("confirmed", {state.value for state in apm.MemoryState})

        for error_type in (
            apm.ValidationError,
            apm.NotFoundError,
            apm.ConflictError,
            apm.SchemaVersionError,
            apm.TrustedDeleteDenied,
        ):
            self.assertTrue(issubclass(error_type, Exception))
        for dtype in (
            apm.ErasureReceipt,
            apm.DeleteRequest,
            apm.ConfirmNonce,
            apm.ScoredRecordId,
            apm.EngineResult,
        ):
            self.assertIsInstance(dtype, type)

        for schema_name in (
            "ASSEMBLE_CONTEXT_SCHEMA",
            "CONFIRM_CANDIDATE_MEMORY_SCHEMA",
            "CORRECT_PERSONAL_MEMORY_SCHEMA",
            "DO_NOT_REMEMBER_SCHEMA",
            "EXPLAIN_CLAIM_SCHEMA",
            "EXPLAIN_WHY_REMEMBERED_SCHEMA",
            "EXPORT_OWNER_DATA_SCHEMA",
            "EXPORT_PERSONAL_MEMORY_SCHEMA",
            "FORGET_PERSONAL_MEMORY_SCHEMA",
            "GET_AUTHORIZED_CLAIM_SCHEMA",
            "GET_AUTHORIZED_SOURCE_SCHEMA",
            "GET_MEMORY_MODE_SCHEMA",
            "LIST_CANDIDATE_MEMORIES_SCHEMA",
            "LIST_PERSONAL_MEMORIES_SCHEMA",
            "PERSONAL_MEMORY_CONFIRM_SCHEMA",
            "QUERY_CLAIMS_SCHEMA",
            "QUERY_SOURCES_SCHEMA",
            "RANK_CLAIMS_SCHEMA",
            "REJECT_CANDIDATE_MEMORY_SCHEMA",
            "REMEMBER_PERSONAL_INSIGHT_SCHEMA",
            "SET_MEMORY_MODE_SCHEMA",
            "TOOL_SCHEMAS",
        ):
            self.assertTrue(hasattr(apm, schema_name), schema_name)


if __name__ == "__main__":
    unittest.main()

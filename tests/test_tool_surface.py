"""Standalone tool-surface guarantees folded from source tests/test_tools.py.

The source module ``plugins/personal_memory/tools.py`` (host ``handle_*``
wrappers that derived the Actor out of the host session context and
*ignored* caller-supplied tenant selectors) stays in Hermes. This file
asserts the equivalent guarantees on the ``agent_personal_memory`` public
surface:

- every name in ``TOOL_SCHEMAS`` resolves to a public callable, except the
  named consume surface ``personal_memory_confirm``, which maps to the
  confirm-gate consume path (``CONFIRM_REGISTRY.consume_confirm`` +
  ``consume_delete_request``);
- tool arguments cannot carry identity: no schema property and no
  owner-flow signature accepts actor/tenant/user/platform selectors --
  where the source silently ignored forged selectors, the standalone
  signatures make them unpassable;
- destructive tools (``forget_personal_memory``, ``do_not_remember``,
  ``reject_candidate_memory``) return non-authorizing previews only;
  erasure runs exclusively through the two-phase confirm-gate whose
  consume schema takes only a ``nonce``.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

import agent_personal_memory as apm
from agent_personal_memory.validation import content_hash_for

ALICE = apm.Actor(platform="t", user_id="1", display_name="")
BOB = apm.Actor(platform="t", user_id="2", display_name="7")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"

# The 14 (name, schema, handler) entries the source ``_TOOLS`` registry
# exposed; each must have a same-named schema and public callable here.
SOURCE_TOOL_NAMES = (
    "export_owner_data",
    "export_personal_memory",
    "explain_claim",
    "list_personal_memories",
    "explain_why_remembered",
    "correct_personal_memory",
    "forget_personal_memory",
    "do_not_remember",
    "list_candidate_memories",
    "confirm_candidate_memory",
    "reject_candidate_memory",
    "get_memory_mode",
    "set_memory_mode",
    "remember_personal_insight",
)

# Names the standalone canonical surface adds beyond the source registry.
ADDED_SURFACE_NAMES = (
    "query_claims",
    "query_sources",
    "get_authorized_claim",
    "get_authorized_source",
    "assemble_context",
    "rank_claims",
    "list_memory_reconciliations",
    "accept_memory_reconciliation",
    "reject_memory_reconciliation",
    "personal_memory_history",
    "retract_personal_memory",
)

CONFIRM_TOOL_NAME = "personal_memory_confirm"

# Reconcile/history owner flows (issue #2/#3): plain owner-flow tools,
# never preview-gated. name -> (properties, required).
NEW_OWNER_FLOW_SCHEMAS = {
    "list_memory_reconciliations": ({"status", "language"}, []),
    "accept_memory_reconciliation": ({"link_id", "language"}, ["link_id"]),
    "reject_memory_reconciliation": ({"link_id", "language"}, ["link_id"]),
    "personal_memory_history": (
        {"claim_id", "query", "collapse_promoted"},
        [],
    ),
    "retract_personal_memory": (
        {"claim_id", "explanation", "language"},
        ["claim_id"],
    ),
}

DESTRUCTIVE_TOOL_NAMES = (
    "forget_personal_memory",
    "do_not_remember",
    "reject_candidate_memory",
)

# Identity may only come from the explicit ``Actor`` argument (or the
# host-bound ``SessionBinding``/``store`` keywords, which are not tool
# arguments). These names must appear neither in schema properties nor in
# owner-flow signatures.
FORBIDDEN_SELECTOR_KEYS = frozenset(
    {
        "actor_id",
        "principal",
        "tenant",
        "tenant_id",
        "user",
        "user_id",
        "platform",
        "platform_id",
        "session_key",
        "session_id",
        "chat_id",
        "thread_id",
    }
)


def _walk_schema_keys(node: dict) -> "list[str]":
    """Yield every property and required-field name at all nesting levels."""
    keys: list[str] = []
    properties = node.get("properties") or {}
    for key, subschema in properties.items():
        keys.append(key)
        if isinstance(subschema, dict):
            keys.extend(_walk_schema_keys(subschema))
            items = subschema.get("items")
            if isinstance(items, dict):
                keys.extend(_walk_schema_keys(items))
    for key in node.get("required") or []:
        keys.append(key)
    return keys


class ToolSchemaSurfaceTests(unittest.TestCase):
    def test_source_tool_names_have_schema_and_callable(self) -> None:
        for name in SOURCE_TOOL_NAMES:
            with self.subTest(tool=name):
                self.assertIn(name, apm.TOOL_SCHEMAS)
                schema = apm.TOOL_SCHEMAS[name]
                self.assertEqual(schema["name"], name)
                self.assertTrue(callable(getattr(apm, name, None)))
                self.assertIn(name, apm.__all__)

    def test_every_tool_schema_maps_to_public_surface(self) -> None:
        for name, schema in apm.TOOL_SCHEMAS.items():
            with self.subTest(tool=name):
                self.assertEqual(schema["name"], name)
                if name == CONFIRM_TOOL_NAME:
                    # The consume surface has no plain callable: it maps to
                    # the confirm-gate path the host drives with an
                    # operator-supplied binding.
                    self.assertTrue(callable(apm.CONFIRM_REGISTRY.consume_confirm))
                    self.assertTrue(callable(apm.consume_delete_request))
                else:
                    self.assertTrue(callable(getattr(apm, name, None)))
        self.assertEqual(
            set(apm.TOOL_SCHEMAS),
            set(SOURCE_TOOL_NAMES) | set(ADDED_SURFACE_NAMES) | {CONFIRM_TOOL_NAME},
        )

    def test_tool_schemas_have_no_identity_selectors(self) -> None:
        for name, schema in apm.TOOL_SCHEMAS.items():
            keys = set(_walk_schema_keys(schema["parameters"]))
            bad = sorted(keys & FORBIDDEN_SELECTOR_KEYS)
            self.assertFalse(bad, f"{name}: identity selectors in args {bad}")

    def test_owner_flows_take_explicit_actor_without_selector_params(self) -> None:
        for name in SOURCE_TOOL_NAMES + ADDED_SURFACE_NAMES:
            function = getattr(apm, name)
            params = list(inspect.signature(function).parameters.values())
            with self.subTest(tool=name):
                self.assertTrue(params, name)
                self.assertEqual(params[0].name, "actor", name)
                bad = sorted({p.name for p in params} & FORBIDDEN_SELECTOR_KEYS)
                self.assertFalse(bad, f"{name}: selector params {bad}")

    def test_selector_kwargs_are_unpassable(self) -> None:
        # The source handlers silently ignored caller-supplied
        # tenant/user/platform kwargs; the standalone signatures reject
        # them outright.
        with self.assertRaises(TypeError):
            apm.explain_claim(ALICE, "x", tenant_id="other")
        with self.assertRaises(TypeError):
            apm.list_personal_memories(ALICE, user_id="2", platform="x")

    def test_destructive_schemas_have_no_confirm_boolean(self) -> None:
        for name in DESTRUCTIVE_TOOL_NAMES:
            properties = apm.TOOL_SCHEMAS[name]["parameters"]["properties"]
            with self.subTest(tool=name):
                self.assertNotIn("confirm", properties)
                self.assertNotIn("confirmed", properties)
                for key, subschema in properties.items():
                    self.assertNotEqual(
                        subschema.get("type"), "boolean", f"{name}.{key}"
                    )

    def test_reconcile_surface_schemas_registered(self) -> None:
        for name, (props, required) in NEW_OWNER_FLOW_SCHEMAS.items():
            with self.subTest(tool=name):
                schema = apm.TOOL_SCHEMAS[name]
                self.assertEqual(schema["name"], name)
                params = schema["parameters"]
                self.assertEqual(set(params["properties"]), props)
                self.assertEqual(params["required"], required)
                self.assertIn("current session owner", schema["description"])
                self.assertTrue(callable(getattr(apm, name, None)))

    def test_new_owner_flows_are_not_preview_gated(self) -> None:
        from agent_personal_memory import mcp as apm_mcp

        for name in NEW_OWNER_FLOW_SCHEMAS:
            with self.subTest(tool=name):
                self.assertNotIn(name, apm_mcp._PREVIEW_TOOLS)

    def test_reconcile_schema_details(self) -> None:
        props = apm.TOOL_SCHEMAS["list_memory_reconciliations"]["parameters"][
            "properties"
        ]
        self.assertEqual(
            props["status"]["enum"], ["pending", "accepted", "rejected"]
        )
        self.assertEqual(props["language"]["enum"], ["en", "ru"])
        history_props = apm.TOOL_SCHEMAS["personal_memory_history"][
            "parameters"
        ]["properties"]
        self.assertEqual(history_props["collapse_promoted"]["type"], "boolean")
        remember = apm.TOOL_SCHEMAS["remember_personal_insight"]
        self.assertNotIn("judge", remember["parameters"]["properties"])
        self.assertIn("reconciliations", remember["description"])

    def test_states_enums_include_retracted(self) -> None:
        for schema in (apm.QUERY_CLAIMS_SCHEMA, apm.RANK_CLAIMS_SCHEMA):
            with self.subTest(schema=schema["name"]):
                enum = schema["parameters"]["properties"]["states"]["items"][
                    "enum"
                ]
                self.assertIn("retracted", enum)
                self.assertIn("superseded", enum)

    def test_write_helpers_require_explicit_store(self) -> None:
        # Existing convention (correct_personal_memory): a missing store=
        # kwarg is a TypeError, never a silent default data dir.
        with self.assertRaises(TypeError):
            apm.correct_personal_memory(ALICE, "c", "x")
        for call in (
            lambda: apm.list_memory_reconciliations(ALICE),
            lambda: apm.accept_memory_reconciliation(ALICE, "link-1"),
            lambda: apm.reject_memory_reconciliation(ALICE, "link-1"),
            lambda: apm.personal_memory_history(ALICE, "claim-1"),
            lambda: apm.retract_personal_memory(ALICE, "claim-1"),
        ):
            with self.subTest(call=call):
                with self.assertRaises(TypeError):
                    call()

    def test_confirm_schema_takes_only_nonce_and_matches_consume_path(self) -> None:
        schema = apm.TOOL_SCHEMAS[CONFIRM_TOOL_NAME]
        params = schema["parameters"]
        self.assertEqual(set(params["properties"]), {"nonce"})
        self.assertEqual(params["required"], ["nonce"])
        consume_params = list(
            inspect.signature(apm.CONFIRM_REGISTRY.consume_confirm).parameters
        )
        self.assertEqual(consume_params, ["nonce", "binding"])
        execute_params = inspect.signature(apm.consume_delete_request).parameters
        self.assertEqual(list(execute_params), ["record", "store"])
        self.assertEqual(
            execute_params["store"].kind, inspect.Parameter.KEYWORD_ONLY
        )


class ToolSurfaceFlowTests(unittest.TestCase):
    """Same fixture intent as the source test: two actors, one claim each."""

    def setUp(self) -> None:
        apm.reset_registry()
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-tool-surface-")
        self.data_dir = Path(self.tmp.name)
        self.store = apm.PersonalMemoryStore(data_dir=self.data_dir)
        snapshot = b"pmA"
        source = self.store.register_source(
            ALICE,
            source_kind="document",
            external_ref="ref-1",
            content_hash=content_hash_for(snapshot),
            snapshot=snapshot,
            observed_at=NOW,
            retention_class="standard",
            metadata={"title": "A", "numeric": 7, "boolean": True, "empty": ""},
        )
        created = self.store.create_candidate(
            ALICE,
            claim_kind="preference",
            content=b"a1",
            confidence=0.7,
            explanation="",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
            expires_at=EXPIRES,
        )
        self.claim_id = str(created["claim_id"])
        bob_snapshot = b"pmB"
        bob_source = self.store.register_source(
            BOB,
            source_kind="document",
            external_ref="ref-2",
            content_hash=content_hash_for(bob_snapshot),
            snapshot=bob_snapshot,
            observed_at=NOW,
            retention_class="standard",
            metadata={"title": "B", "numeric": 0, "boolean": False, "empty": ""},
        )
        bob = self.store.create_candidate(
            BOB,
            claim_kind="preference",
            content=b"b2",
            confidence=0.4,
            explanation="",
            sensitivity="normal",
            source_ids=[str(bob_source["source_id"])],
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
            expires_at=EXPIRES,
        )
        self.bob_claim_id = str(bob["claim_id"])
        self.sent: list[str] = []

    def tearDown(self) -> None:
        apm.reset_registry()
        self.tmp.cleanup()

    def _binding(self) -> apm.SessionBinding:
        return apm.SessionBinding(
            platform=ALICE.platform,
            user_id=ALICE.user_id,
            session_key="agent:test:dm",
            session_id="sess-1",
            chat_id="chat-1",
        )

    def _send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def _nonce(self) -> str:
        for line in self.sent[-1].splitlines():
            if "/memory_confirm" in line:
                return line.split()[-1]
        raise AssertionError("nonce missing from delivered preview")

    def test_owner_tools_are_bound_to_actor_not_callable_selectors(self) -> None:
        exported = json.loads(apm.export_owner_data(ALICE, store=self.store))
        packed = json.dumps(exported, ensure_ascii=False)
        self.assertIn('"content": "a1"', packed)
        self.assertNotIn('"content": "b2"', packed)
        self.assertNotIn("pmB", packed)
        self.assertNotIn('"label": "B"', packed)
        explanation = apm.explain_claim(ALICE, self.claim_id, store=self.store)
        self.assertEqual(explanation["claim_id"], self.claim_id)
        self.assertEqual(explanation["state"], "candidate")
        with self.assertRaises(apm.NotFoundError):
            apm.explain_claim(ALICE, self.bob_claim_id, store=self.store)

    def test_destructive_tools_produce_previews_only(self) -> None:
        for call in (
            lambda: apm.forget_personal_memory(
                ALICE, self.claim_id, store=self.store
            ),
            lambda: apm.do_not_remember(ALICE, text="a1", store=self.store),
            lambda: apm.reject_candidate_memory(
                ALICE, self.claim_id, store=self.store
            ),
        ):
            payload = call()
            self.assertTrue(payload["preview"])
            self.assertIn("confirmation", payload)
            self.assertNotIn("nonce", payload)
        rows = apm.query_claims(ALICE, store=self.store)
        self.assertIn(self.claim_id, {str(row["claim_id"]) for row in rows})

    def test_retracted_state_query_roundtrip(self) -> None:
        apm.retract_personal_memory(ALICE, self.claim_id, store=self.store)
        retracted = apm.query_claims(
            ALICE, states=["retracted"], store=self.store
        )
        self.assertEqual(
            [str(row["claim_id"]) for row in retracted], [self.claim_id]
        )
        active = apm.query_claims(ALICE, store=self.store)
        self.assertNotIn(
            self.claim_id, {str(row["claim_id"]) for row in active}
        )

    def test_two_phase_confirm_executes_bound_erasure(self) -> None:
        preview = apm.forget_personal_memory(
            ALICE,
            self.claim_id,
            store=self.store,
            session_binding=self._binding(),
        )
        self.assertTrue(preview["preview"])
        self.assertIn("request_id", preview)
        # The preview is non-authorizing: it never carries the nonce.
        preview_json = json.dumps(preview, ensure_ascii=False, default=str)
        apm.CONFIRM_REGISTRY.open_preview(
            str(preview["request_id"]), self._binding(), send=self._send
        )
        nonce = self._nonce()
        self.assertNotEqual(nonce, str(preview["request_id"]))
        self.assertNotIn(nonce, preview_json)
        record = apm.CONFIRM_REGISTRY.consume_confirm(nonce, self._binding())
        erased = apm.consume_delete_request(record, store=self.store)
        self.assertFalse(erased["preview"])
        with self.assertRaises(apm.NotFoundError):
            apm.get_authorized_claim(ALICE, self.claim_id, store=self.store)

    def test_failed_send_enables_no_nonce(self) -> None:
        preview = apm.forget_personal_memory(
            ALICE,
            self.claim_id,
            store=self.store,
            session_binding=self._binding(),
        )
        with self.assertRaises(apm.TrustedDeleteDenied):
            apm.CONFIRM_REGISTRY.open_preview(
                str(preview["request_id"]),
                self._binding(),
                send=lambda text: False,
            )
        with self.assertRaises(apm.TrustedDeleteDenied):
            apm.CONFIRM_REGISTRY.consume_confirm("forged-nonce", self._binding())
        # The claim is still there: a failed send never registers a nonce.
        rows = apm.query_claims(ALICE, store=self.store)
        self.assertIn(self.claim_id, {str(row["claim_id"]) for row in rows})


if __name__ == "__main__":
    unittest.main()

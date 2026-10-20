"""Tool schemas must not accept tenant or identity selectors."""

from __future__ import annotations

import json
import unittest

from agent_personal_memory import schemas

FORBIDDEN = (
    "tenant",
    "tenant_id",
    "user_id",
    "platform",
    "actor",
    "principal",
)


def _walk(value: object) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            names.append(str(key))
            names.extend(_walk(item))
    elif isinstance(value, list):
        for item in value:
            names.extend(_walk(item))
    return names


class PersonalMemorySchemaTests(unittest.TestCase):
    def test_ownership_schemas_have_no_tenant_selectors(self) -> None:
        export_schema = schemas.EXPORT_OWNER_DATA_SCHEMA
        explain_schema = schemas.EXPLAIN_CLAIM_SCHEMA
        self.assertEqual(export_schema["name"], "export_owner_data")
        self.assertEqual(explain_schema["name"], "explain_claim")
        self.assertEqual(export_schema["parameters"]["properties"], {})
        self.assertEqual(export_schema["parameters"]["required"], [])
        explain_props = explain_schema["parameters"]["properties"]
        self.assertEqual(set(explain_props), {"claim_id"})
        self.assertEqual(explain_schema["parameters"]["required"], ["claim_id"])
        packed = json.dumps(
            [export_schema, explain_schema],
            ensure_ascii=False,
        ).lower()
        for name in (export_schema, explain_schema):
            keys = {key.lower() for key in _walk(name)}
            for forbidden in FORBIDDEN:
                self.assertNotIn(forbidden, keys)
                self.assertNotIn(forbidden, packed)


class ReconcileSurfaceSchemaTests(unittest.TestCase):
    """The five reconcile/history owner-flow tools (issue #2/#3)."""

    def test_five_new_tools_registered(self) -> None:
        expected = {
            "list_memory_reconciliations": ({"status", "language"}, []),
            "accept_memory_reconciliation": (
                {"link_id", "language"},
                ["link_id"],
            ),
            "reject_memory_reconciliation": (
                {"link_id", "language"},
                ["link_id"],
            ),
            "personal_memory_history": (
                {"claim_id", "query", "collapse_promoted"},
                [],
            ),
            "retract_personal_memory": (
                {"claim_id", "explanation", "language"},
                ["claim_id"],
            ),
        }
        for name, (props, required) in expected.items():
            with self.subTest(tool=name):
                schema = schemas.TOOL_SCHEMAS[name]
                self.assertEqual(schema["name"], name)
                params = schema["parameters"]
                self.assertEqual(set(params["properties"]), props)
                self.assertEqual(params["required"], required)
                keys = {key.lower() for key in _walk(schema)}
                for forbidden in FORBIDDEN:
                    self.assertNotIn(forbidden, keys, name)
                self.assertIn(
                    "current session owner", schema["description"]
                )

    def test_reconcile_schema_details(self) -> None:
        listing = schemas.TOOL_SCHEMAS["list_memory_reconciliations"]
        props = listing["parameters"]["properties"]
        self.assertEqual(
            props["status"]["enum"], ["pending", "accepted", "rejected"]
        )
        self.assertEqual(props["language"]["enum"], ["en", "ru"])
        history = schemas.TOOL_SCHEMAS["personal_memory_history"]
        self.assertEqual(
            history["parameters"]["properties"]["collapse_promoted"]["type"],
            "boolean",
        )
        retract = schemas.TOOL_SCHEMAS["retract_personal_memory"]
        self.assertEqual(
            retract["parameters"]["properties"]["language"]["enum"],
            ["en", "ru"],
        )
        remember = schemas.TOOL_SCHEMAS["remember_personal_insight"]
        self.assertNotIn("judge", remember["parameters"]["properties"])
        self.assertIn("reconciliations", remember["description"])

    def test_states_enums_include_retracted(self) -> None:
        for schema in (schemas.QUERY_CLAIMS_SCHEMA, schemas.RANK_CLAIMS_SCHEMA):
            with self.subTest(schema=schema["name"]):
                enum = schema["parameters"]["properties"]["states"]["items"][
                    "enum"
                ]
                self.assertIn("retracted", enum)
                self.assertIn("superseded", enum)


if __name__ == "__main__":
    unittest.main()

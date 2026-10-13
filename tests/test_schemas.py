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


if __name__ == "__main__":
    unittest.main()

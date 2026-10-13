"""Bilingual export UX over authorized personal-memory APIs."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.schemas import EXPORT_PERSONAL_MEMORY_SCHEMA
from agent_personal_memory.store import PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.ux import export_personal_memory
from agent_personal_memory.validation import content_hash_for

ALICE = Actor(platform="t", user_id="61", display_name="")
BOB = Actor(platform="t", user_id="62", display_name="")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"
FORBIDDEN_SELECTORS = ("tenant_id", "tenant", "user_id", "platform")


class PersonalMemoryExportUxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-export-ux-")
        self.data_dir = Path(self.tmp.name)
        self.store = PersonalMemoryStore(data_dir=self.data_dir)
        snapshot = b"pmA"
        source = self.store.register_source(
            ALICE,
            source_kind="document",
            external_ref="ref-1",
            content_hash=content_hash_for(snapshot),
            snapshot=snapshot,
            observed_at=NOW,
            retention_class="standard",
            metadata={"title": "A"},
        )
        self.store.create_candidate(
            ALICE,
            claim_kind="preference",
            content=b"prefers window seats",
            confidence=0.7,
            explanation="",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
            expires_at=EXPIRES,
        )
        bob_source = self.store.register_source(
            BOB,
            source_kind="document",
            external_ref="ref-2",
            content_hash=content_hash_for(b"pmB"),
            snapshot=b"pmB",
            observed_at=NOW,
            retention_class="standard",
            metadata={"title": "B"},
        )
        self.store.create_candidate(
            BOB,
            claim_kind="preference",
            content=b"bob secret",
            confidence=0.4,
            explanation="",
            sensitivity="normal",
            source_ids=[str(bob_source["source_id"])],
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
            expires_at=EXPIRES,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_export_schema_has_no_tenant_selectors(self) -> None:
        packed = json.dumps(EXPORT_PERSONAL_MEMORY_SCHEMA, ensure_ascii=False)
        for selector in FORBIDDEN_SELECTORS:
            self.assertNotIn(selector, packed)

    def test_export_localizes_heading_and_summary(self) -> None:
        en = export_personal_memory(ALICE, language="en", store=self.store)
        ru = export_personal_memory(ALICE, language="ru", store=self.store)
        self.assertIn("export", en)
        self.assertEqual(en["counts"]["sources"], 1)
        self.assertEqual(en["counts"]["claims"], 1)
        self.assertIn("Sources:", en["summary"])
        self.assertIn("Источников:", ru["summary"])
        self.assertNotEqual(en["heading"], ru["heading"])
        self.assertNotEqual(en["summary"], ru["summary"])

    def test_export_contains_only_session_owner_data(self) -> None:
        payload = export_personal_memory(ALICE, language="en", store=self.store)
        packed = json.dumps(payload["export"], default=str)
        self.assertIn("prefers window seats", packed)
        self.assertNotIn("bob secret", packed)
        for selector in FORBIDDEN_SELECTORS:
            self.assertNotIn(selector, packed)

    def test_export_flow_uses_explicit_actor(self) -> None:
        result = export_personal_memory(ALICE, language="ru", store=self.store)
        self.assertEqual(result["language"], "ru")
        self.assertIn("Экспорт", result["heading"])


if __name__ == "__main__":
    unittest.main()

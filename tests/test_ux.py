"""Bilingual read and explain-why owner-flow UX."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.schemas import (
    EXPLAIN_CLAIM_SCHEMA,
    EXPLAIN_WHY_REMEMBERED_SCHEMA,
    EXPORT_OWNER_DATA_SCHEMA,
    LIST_PERSONAL_MEMORIES_SCHEMA,
)
from agent_personal_memory.store import NotFoundError, PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.ux import explain_why_remembered, list_personal_memories
from agent_personal_memory.validation import ValidationError, content_hash_for

ALICE = Actor(platform="t", user_id="31", display_name="")
BOB = Actor(platform="t", user_id="32", display_name="")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"
FORBIDDEN_SELECTORS = ("tenant_id", "tenant", "user_id", "platform")


class PersonalMemoryUxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-ux-")
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
        created = self.store.create_candidate(
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
        self.claim_id = str(created["claim_id"])
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
        bob = self.store.create_candidate(
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
        self.bob_claim_id = str(bob["claim_id"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_list_and_explain_schemas_have_no_tenant_selectors(self) -> None:
        for schema in (
            LIST_PERSONAL_MEMORIES_SCHEMA,
            EXPLAIN_WHY_REMEMBERED_SCHEMA,
            EXPLAIN_CLAIM_SCHEMA,
            EXPORT_OWNER_DATA_SCHEMA,
        ):
            packed = json.dumps(schema, ensure_ascii=False)
            for selector in FORBIDDEN_SELECTORS:
                self.assertNotIn(selector, packed)

    def test_list_personal_memories_localizes_summaries(self) -> None:
        en = list_personal_memories(ALICE, language="en", store=self.store)
        ru = list_personal_memories(ALICE, language="ru", store=self.store)
        self.assertEqual(en["language"], "en")
        self.assertEqual(ru["language"], "ru")
        self.assertIn("confidence", en["items"][0]["summary"])
        self.assertIn("уверенность", ru["items"][0]["summary"])
        self.assertNotEqual(en["items"][0]["summary"], ru["items"][0]["summary"])

    def test_explain_why_remembered_localizes_narrative(self) -> None:
        en = explain_why_remembered(
            ALICE, self.claim_id, language="en", store=self.store
        )
        ru = explain_why_remembered(
            ALICE, self.claim_id, language="ru", store=self.store
        )
        self.assertIn("because", en["narrative"])
        self.assertIn("потому что", ru["narrative"])
        self.assertNotEqual(en["narrative"], ru["narrative"])

    def test_cross_tenant_list_and_explain_fail_closed(self) -> None:
        listed = list_personal_memories(ALICE, language="en", store=self.store)
        packed = json.dumps(listed, ensure_ascii=False)
        self.assertNotIn(self.bob_claim_id, packed)
        with self.assertRaises(NotFoundError):
            explain_why_remembered(
                ALICE, self.bob_claim_id, language="en", store=self.store
            )

    def test_invalid_language_raises_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            list_personal_memories(ALICE, language="de", store=self.store)


if __name__ == "__main__":
    unittest.main()

"""Bilingual owner mutation flows over authorized personal-memory APIs."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.mutations import (
    correct_personal_memory,
    do_not_remember,
    forget_personal_memory,
)
from agent_personal_memory.query import query_claims
from agent_personal_memory.schemas import (
    CORRECT_PERSONAL_MEMORY_SCHEMA,
    DO_NOT_REMEMBER_SCHEMA,
    FORGET_PERSONAL_MEMORY_SCHEMA,
)
from agent_personal_memory.store import NotFoundError, PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import ValidationError, content_hash_for

ALICE = Actor(platform="t", user_id="41", display_name="")
BOB = Actor(platform="t", user_id="42", display_name="")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"
FORBIDDEN_SELECTORS = ("tenant_id", "tenant", "user_id", "platform")


class PersonalMemoryMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-mut-")
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
        match_source = self.store.register_source(
            ALICE,
            source_kind="document",
            external_ref="ref-match",
            content_hash=content_hash_for(b"pm-match"),
            snapshot=b"pm-match",
            observed_at=NOW,
            retention_class="standard",
            metadata={"title": "match"},
        )
        match = self.store.create_candidate(
            ALICE,
            claim_kind="preference",
            content=b"allergic to peanuts",
            confidence=0.6,
            explanation="",
            sensitivity="normal",
            source_ids=[str(match_source["source_id"])],
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
            expires_at=EXPIRES,
        )
        self.match_claim_id = str(match["claim_id"])
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

    def test_mutation_schemas_have_no_tenant_selectors(self) -> None:
        for schema in (
            CORRECT_PERSONAL_MEMORY_SCHEMA,
            FORGET_PERSONAL_MEMORY_SCHEMA,
            DO_NOT_REMEMBER_SCHEMA,
        ):
            packed = json.dumps(schema, ensure_ascii=False)
            for selector in FORBIDDEN_SELECTORS:
                self.assertNotIn(selector, packed)

    def test_correct_creates_new_version(self) -> None:
        result = correct_personal_memory(
            ALICE,
            self.claim_id,
            "prefers aisle seats",
            explanation="updated preference",
            language="en",
            store=self.store,
        )
        self.assertEqual(result["version"], 2)
        self.assertIn("version 2", result["confirmation"])
        rows = query_claims(ALICE, include_content=True, store=self.store)
        target = next(row for row in rows if row["claim_id"] == self.claim_id)
        self.assertEqual(bytes(target["content"]).decode("utf-8"), "prefers aisle seats")

    def test_forget_preview_leaves_store_unchanged_and_boolean_cannot_erase(self) -> None:
        preview = forget_personal_memory(
            ALICE,
            self.claim_id,
            language="en",
            store=self.store,
        )
        self.assertTrue(preview["preview"])
        self.assertIn("Preview", preview["confirmation"])
        rows = query_claims(ALICE, store=self.store)
        self.assertIn(self.claim_id, {str(row["claim_id"]) for row in rows})
        with self.assertRaises(ValidationError):
            forget_personal_memory(
                ALICE,
                self.claim_id,
                confirm=True,
                language="ru",
                store=self.store,
            )
        rows = query_claims(ALICE, store=self.store)
        self.assertIn(self.claim_id, {str(row["claim_id"]) for row in rows})

    def test_forget_boolean_cannot_remove_claim_from_query(self) -> None:
        with self.assertRaises(ValidationError):
            forget_personal_memory(
                ALICE,
                self.claim_id,
                confirm=True,
                language="en",
                store=self.store,
            )
        rows = query_claims(ALICE, store=self.store)
        ids = {str(row["claim_id"]) for row in rows}
        self.assertIn(self.claim_id, ids)

    def test_do_not_remember_preview_then_boolean_cannot_erase(self) -> None:
        preview = do_not_remember(
            ALICE,
            text="peanut",
            language="en",
            store=self.store,
        )
        self.assertTrue(preview["preview"])
        self.assertEqual(preview["matched_claim_ids"], [self.match_claim_id])
        rows = query_claims(ALICE, store=self.store)
        self.assertIn(self.match_claim_id, {str(row["claim_id"]) for row in rows})
        with self.assertRaises(ValidationError):
            do_not_remember(
                ALICE,
                text="peanut",
                confirm=True,
                language="en",
                store=self.store,
            )
        rows = query_claims(ALICE, store=self.store)
        ids = {str(row["claim_id"]) for row in rows}
        self.assertIn(self.match_claim_id, ids)

    def test_do_not_remember_by_text_boolean_cannot_erase(self) -> None:
        with self.assertRaises(ValidationError):
            do_not_remember(
                ALICE,
                text="peanut",
                confirm=True,
                language="en",
                store=self.store,
            )
        rows = query_claims(ALICE, store=self.store)
        ids = {str(row["claim_id"]) for row in rows}
        self.assertIn(self.match_claim_id, ids)

    def test_do_not_remember_localizes_confirmations(self) -> None:
        en = do_not_remember(
            ALICE,
            text="missing phrase",
            language="en",
            store=self.store,
        )
        ru = do_not_remember(
            ALICE,
            text="missing phrase",
            language="ru",
            store=self.store,
        )
        self.assertIn("No matching memories", en["confirmation"])
        self.assertIn("не найдены", ru["confirmation"])
        self.assertNotEqual(en["confirmation"], ru["confirmation"])

    def test_cross_tenant_mutations_fail_closed(self) -> None:
        with self.assertRaises(NotFoundError):
            forget_personal_memory(
                ALICE,
                self.bob_claim_id,
                language="en",
                store=self.store,
            )
        with self.assertRaises(NotFoundError):
            correct_personal_memory(
                ALICE,
                self.bob_claim_id,
                "stolen",
                language="en",
                store=self.store,
            )

    def test_do_not_remember_preview_empty_language_does_not_erase(self) -> None:
        preview = do_not_remember(
            ALICE,
            text="peanut",
            language="",
            store=self.store,
        )
        self.assertTrue(preview["preview"])
        self.assertEqual(preview["language"], "en")
        rows = query_claims(ALICE, store=self.store)
        self.assertIn(self.match_claim_id, {str(row["claim_id"]) for row in rows})
        with self.assertRaises(ValidationError):
            do_not_remember(
                ALICE,
                text="peanut",
                confirm=True,
                language="ru",
                store=self.store,
            )
        rows = query_claims(ALICE, store=self.store)
        self.assertIn(self.match_claim_id, {str(row["claim_id"]) for row in rows})

    def test_do_not_remember_invalid_language_still_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            do_not_remember(
                ALICE,
                text="peanut",
                language="fr",
                store=self.store,
            )

    def test_do_not_remember_schema_defaults_are_preview_safe(self) -> None:
        props = DO_NOT_REMEMBER_SCHEMA["parameters"]["properties"]
        self.assertNotIn("confirm", props)
        self.assertNotIn("confirmed", props)
        self.assertEqual(props["language"]["default"], "en")
        self.assertEqual(props["language"]["enum"], ["en", "ru"])

    def test_do_not_remember_requires_target(self) -> None:
        with self.assertRaises(ValidationError):
            do_not_remember(ALICE, language="en", store=self.store)


if __name__ == "__main__":
    unittest.main()

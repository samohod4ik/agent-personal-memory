"""Bilingual candidate-review owner flows."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.lifecycle import MemoryState
from agent_personal_memory.query import query_claims
from agent_personal_memory.review import (
    confirm_candidate_memory,
    list_candidate_memories,
    reject_candidate_memory,
)
from agent_personal_memory.schemas import (
    CONFIRM_CANDIDATE_MEMORY_SCHEMA,
    LIST_CANDIDATE_MEMORIES_SCHEMA,
    REJECT_CANDIDATE_MEMORY_SCHEMA,
)
from agent_personal_memory.store import NotFoundError, PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import ValidationError, content_hash_for

ALICE = Actor(platform="t", user_id="51", display_name="")
BOB = Actor(platform="t", user_id="52", display_name="")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"
FORBIDDEN_SELECTORS = ("tenant_id", "tenant", "user_id", "platform")


class PersonalMemoryReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-review-")
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
        candidate = self.store.create_candidate(
            ALICE,
            claim_kind="candidate_insight",
            content=b"maybe likes jazz",
            confidence=0.55,
            explanation="inferred",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
            expires_at=EXPIRES,
        )
        self.candidate_id = str(candidate["claim_id"])
        confirmed = self.store.create_candidate(
            ALICE,
            claim_kind="preference",
            content=b"confirmed pref",
            confidence=0.8,
            explanation="",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
            expires_at=EXPIRES,
        )
        confirmation = self.store.register_source(
            ALICE,
            source_kind="user_confirmation",
            external_ref="ref-c",
            content_hash=content_hash_for(b"yes"),
            snapshot=b"yes",
            observed_at=NOW,
            retention_class="standard",
            metadata={"title": "confirm"},
        )
        self.store.confirm_claim(
            ALICE,
            str(confirmed["claim_id"]),
            confirmation_source_id=str(confirmation["source_id"]),
        )
        self.confirmed_id = str(confirmed["claim_id"])
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
        bob_candidate = self.store.create_candidate(
            BOB,
            claim_kind="candidate_insight",
            content=b"bob candidate",
            confidence=0.4,
            explanation="",
            sensitivity="normal",
            source_ids=[str(bob_source["source_id"])],
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
            expires_at=EXPIRES,
        )
        self.bob_candidate_id = str(bob_candidate["claim_id"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_review_schemas_have_no_tenant_selectors(self) -> None:
        for schema in (
            LIST_CANDIDATE_MEMORIES_SCHEMA,
            CONFIRM_CANDIDATE_MEMORY_SCHEMA,
            REJECT_CANDIDATE_MEMORY_SCHEMA,
        ):
            packed = json.dumps(schema, ensure_ascii=False)
            for selector in FORBIDDEN_SELECTORS:
                self.assertNotIn(selector, packed)

    def test_list_returns_candidates_only_with_localized_summaries(self) -> None:
        en = list_candidate_memories(ALICE, language="en", store=self.store)
        ru = list_candidate_memories(ALICE, language="ru", store=self.store)
        en_ids = {str(item["claim_id"]) for item in en["items"]}
        self.assertEqual(en_ids, {self.candidate_id})
        self.assertNotIn(self.confirmed_id, en_ids)
        self.assertIn("candidate", en["items"][0]["summary"].lower())
        self.assertIn("кандидат", ru["items"][0]["summary"].lower())
        self.assertNotEqual(en["heading"], ru["heading"])

    def test_confirm_transitions_candidate_to_confirmed(self) -> None:
        result = confirm_candidate_memory(
            ALICE,
            self.candidate_id,
            language="en",
            store=self.store,
        )
        self.assertEqual(result["state"], MemoryState.CONFIRMED.value)
        self.assertIn("confirmed", result["confirmation"].lower())
        candidates = query_claims(
            ALICE,
            states=[MemoryState.CANDIDATE.value],
            store=self.store,
        )
        self.assertNotIn(self.candidate_id, {str(row["claim_id"]) for row in candidates})

    def test_reject_preview_then_confirm_erases_candidate(self) -> None:
        preview = reject_candidate_memory(
            ALICE,
            self.candidate_id,
            language="en",
            store=self.store,
        )
        self.assertTrue(preview["preview"])
        self.assertIn("Preview", preview["confirmation"])
        candidates = query_claims(
            ALICE,
            states=[MemoryState.CANDIDATE.value],
            store=self.store,
        )
        self.assertIn(self.candidate_id, {str(row["claim_id"]) for row in candidates})
        with self.assertRaises(ValidationError):
            reject_candidate_memory(
                ALICE,
                self.candidate_id,
                confirm=True,
                language="ru",
                store=self.store,
            )
        candidates = query_claims(
            ALICE,
            states=[MemoryState.CANDIDATE.value],
            store=self.store,
        )
        self.assertIn(self.candidate_id, {str(row["claim_id"]) for row in candidates})

    def test_reject_non_candidate_raises_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            reject_candidate_memory(
                ALICE,
                self.confirmed_id,
                language="en",
                store=self.store,
            )

    def test_cross_tenant_review_denied(self) -> None:
        with self.assertRaises(NotFoundError):
            confirm_candidate_memory(
                ALICE,
                self.bob_candidate_id,
                language="en",
                store=self.store,
            )
        with self.assertRaises(NotFoundError):
            reject_candidate_memory(
                ALICE,
                self.bob_candidate_id,
                language="en",
                store=self.store,
            )


if __name__ == "__main__":
    unittest.main()

"""Bounded context assembler: personal/session sections, budgets, labels, ACL."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_personal_memory.assembler import assemble_context
from agent_personal_memory.store import PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import ValidationError, content_hash_for

ALICE = Actor(platform="telegram", user_id="2001", display_name="Alice")
BOB = Actor(platform="telegram", user_id="2002", display_name="Bob")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"
FORBIDDEN_SELECTORS = ("tenant_id", "tenant", "user_id", "platform")


def _source_kwargs(*, snapshot: bytes, external_ref: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "source_kind": "document",
        "external_ref": external_ref,
        "content_hash": content_hash_for(snapshot),
        "snapshot": snapshot,
        "observed_at": NOW,
        "retention_class": "standard",
        "metadata": {"title": "doc"},
    }
    values.update(overrides)
    return values


def _claim_kwargs(source_ids: list[str], *, content: bytes, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "claim_kind": "preference",
        "content": content,
        "confidence": 0.7,
        "explanation": "note",
        "sensitivity": "normal",
        "source_ids": list(source_ids),
        "valid_from": "2026-08-01T00:00:00Z",
        "valid_to": "2026-12-31T23:59:59Z",
        "expires_at": EXPIRES,
    }
    values.update(overrides)
    return values


class ContextAssemblerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memory_tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-assembler-")
        self.memory_store = PersonalMemoryStore(data_dir=Path(self.memory_tmp.name))

    def tearDown(self) -> None:
        self.memory_tmp.cleanup()

    def _register(self, actor: Actor, **overrides: object) -> dict[str, object]:
        return self.memory_store.register_source(actor, **_source_kwargs(**overrides))

    def _create(self, actor: Actor, content: bytes, **overrides: object) -> dict[str, object]:
        if "source_ids" not in overrides:
            source = self._register(
                actor,
                snapshot=content,
                external_ref=f"ref-{actor.user_id}-{content.decode('ascii', 'ignore')[:8]}",
            )
            overrides["source_ids"] = [str(source["source_id"])]
        return self.memory_store.create_candidate(
            actor,
            **_claim_kwargs(overrides.pop("source_ids"), content=content, **overrides),
        )

    def _assemble(self, actor: Actor, **kwargs: object) -> dict[str, object]:
        return assemble_context(
            actor,
            memory_store=self.memory_store,
            **kwargs,
        )

    def test_owner_gets_personal_and_session_sections(self) -> None:
        source = self._register(ALICE, snapshot=b"src-a", external_ref="ref-a")
        self._create(
            ALICE,
            b"candidate-pref",
            source_ids=[str(source["source_id"])],
            claim_kind="candidate_insight",
        )
        confirmation = self._register(
            ALICE,
            snapshot=b"yes",
            external_ref="ref-confirm",
            source_kind="user_confirmation",
        )
        confirmed = self._create(
            ALICE,
            b"confirmed-pref",
            source_ids=[str(source["source_id"])],
            claim_kind="confirmed_fact",
        )
        self.memory_store.confirm_claim(
            ALICE,
            str(confirmed["claim_id"]),
            confirmation_source_id=str(confirmation["source_id"]),
        )
        session = [{"role": "user", "text": "What is my plan?", "observed_at": NOW}]
        result = self._assemble(ALICE, session_items=session)
        packed = json.dumps(result, default=str)
        for selector in FORBIDDEN_SELECTORS:
            self.assertNotIn(selector, packed)
        self.assertIn("assembled_at", result)
        self.assertEqual(result["budgets"]["record_limit"], 50)
        self.assertEqual(set(result), {"assembled_at", "budgets", "personal", "session"})
        personal = result["personal"]["items"]
        session_items = result["session"]["items"]
        self.assertEqual(len(personal), 2)
        self.assertEqual(len(session_items), 1)
        labels = {str(item["freshness_label"]) for item in personal}
        self.assertIn("candidate", labels)
        self.assertIn("confirmed", labels)
        self.assertEqual(session_items[0]["role"], "user")
        self.assertEqual(session_items[0]["freshness_label"], "session")
        for item in personal:
            self.assertIn("provenance", item)
            self.assertIn("claim_id", item["provenance"])
            self.assertIn("created_at", item["provenance"])
            self.assertIn("content", item)

    def test_cross_tenant_actor_gets_empty_personal(self) -> None:
        source = self._register(ALICE, snapshot=b"src-a", external_ref="ref-a")
        self._create(ALICE, b"alice-only", source_ids=[str(source["source_id"])])
        result = self._assemble(
            BOB,
            session_items=[{"role": "user", "text": "hello"}],
        )
        self.assertEqual(result["personal"]["items"], [])
        self.assertEqual(len(result["session"]["items"]), 1)

    @patch("agent_personal_memory.assembler.utcnow_iso", new=lambda: NOW)
    def test_record_budget_truncates_deterministically(self) -> None:
        source = self._register(ALICE, snapshot=b"src-a", external_ref="ref-a")
        self._create(
            ALICE,
            b"personal-one",
            source_ids=[str(source["source_id"])],
            valid_from="2026-08-02T00:00:00Z",
        )
        self._create(
            ALICE,
            b"personal-two",
            source_ids=[str(source["source_id"])],
            valid_from="2026-08-03T00:00:00Z",
        )
        first = self._assemble(
            ALICE,
            record_budget=1,
            session_items=[{"role": "user", "text": "ignored"}],
        )
        second = self._assemble(
            ALICE,
            record_budget=1,
            session_items=[{"role": "user", "text": "ignored"}],
        )
        self.assertTrue(first["budgets"]["truncated"])
        self.assertEqual(first["budgets"]["records_used"], 1)
        self.assertEqual(first, second)
        self.assertEqual(len(first["personal"]["items"]), 1)
        self.assertEqual(len(first["session"]["items"]), 0)

    @patch("agent_personal_memory.assembler.utcnow_iso", new=lambda: NOW)
    def test_token_budget_truncates_deterministically(self) -> None:
        source = self._register(ALICE, snapshot=b"src-a", external_ref="ref-a")
        self._create(
            ALICE,
            b"x" * 80,
            source_ids=[str(source["source_id"])],
        )
        first = self._assemble(ALICE, record_budget=50, token_budget=5)
        second = self._assemble(ALICE, record_budget=50, token_budget=5)
        self.assertTrue(first["budgets"]["truncated"])
        self.assertLessEqual(first["budgets"]["tokens_estimated"], 5)
        self.assertEqual(first, second)

    def test_erased_claim_never_appears(self) -> None:
        source = self._register(ALICE, snapshot=b"src-a", external_ref="ref-a")
        live = self._create(ALICE, b"live", source_ids=[str(source["source_id"])])
        gone = self._create(ALICE, b"gone", source_ids=[str(source["source_id"])])
        self.memory_store.erase_claim(ALICE, str(gone["claim_id"]))
        result = self._assemble(ALICE)
        claim_ids = {str(item["claim_id"]) for item in result["personal"]["items"]}
        self.assertIn(str(live["claim_id"]), claim_ids)
        self.assertNotIn(str(gone["claim_id"]), claim_ids)

    def test_invalid_budgets_raise_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            self._assemble(ALICE, record_budget=0)
        with self.assertRaises(ValidationError):
            self._assemble(ALICE, token_budget=40001)


if __name__ == "__main__":
    unittest.main()

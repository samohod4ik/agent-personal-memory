"""Auto-insight learn path, confirm_required isolation, and remember flow."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.assembler import assemble_context
from agent_personal_memory.learn import learn_from_turn, remember_personal_insight
from agent_personal_memory.lifecycle import MemoryState
from agent_personal_memory.mutations import do_not_remember
from agent_personal_memory.query import query_claims
from agent_personal_memory.store import PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import (
    AUTO_INSIGHT_SOURCE_KIND,
    MEMORY_MODE_AUTO_INSIGHT,
    MEMORY_MODE_CONFIRM_REQUIRED,
    ValidationError,
    content_hash_for,
)

ALICE = Actor(platform="telegram", user_id="3201", display_name="Alice")
BOB = Actor(platform="telegram", user_id="3202", display_name="Bob")


def _personal_contents(actor: Actor, store: PersonalMemoryStore, query_text: str) -> list[str]:
    """Assemble ranked confirmed claims for ``actor`` (replaces the source's
    host inject call: same authorized assembly minus the host text format)."""
    assembled = assemble_context(
        actor,
        memory_store=store,
        query_text=query_text,
        states=[MemoryState.CONFIRMED.value],
        record_budget=24,
        token_budget=550,
    )
    contents: list[str] = []
    for item in assembled["personal"]["items"]:
        raw = item.get("content")
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8")
        contents.append(str(raw))
    return contents


class PersonalMemoryLearnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-learn-")
        self.data_dir = Path(self.tmp.name)
        self.store = PersonalMemoryStore(data_dir=self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _claims(self, actor: Actor, **kwargs: object) -> list[dict[str, object]]:
        return query_claims(actor, include_content=True, limit=50, store=self.store, **kwargs)

    def test_default_mode_auto_persists_confirmed_and_assembly_sees(self) -> None:
        self.assertEqual(self.store.get_memory_mode(ALICE), MEMORY_MODE_AUTO_INSIGHT)
        result = learn_from_turn(
            ALICE,
            "I prefer window seats on flights.",
            store=self.store,
        )
        self.assertEqual(result["mode"], MEMORY_MODE_AUTO_INSIGHT)
        self.assertEqual(len(result["persisted"]), 1)
        self.assertEqual(result["persisted"][0]["state"], MemoryState.CONFIRMED.value)
        rows = self._claims(ALICE, states=["confirmed"])
        self.assertEqual(len(rows), 1)
        content = rows[0]["content"]
        if isinstance(content, (bytes, bytearray)):
            content = bytes(content).decode("utf-8")
        self.assertIn("window", str(content).lower())
        contents = _personal_contents(
            ALICE, self.store, "Which seat do I prefer on flights?"
        )
        self.assertTrue(any("window" in item.lower() for item in contents))

    def test_confirm_required_does_not_auto_confirm_from_chat(self) -> None:
        self.store.set_memory_mode(ALICE, MEMORY_MODE_CONFIRM_REQUIRED)
        result = learn_from_turn(
            ALICE,
            "I prefer window seats on flights.",
            store=self.store,
        )
        self.assertEqual(result["mode"], MEMORY_MODE_CONFIRM_REQUIRED)
        self.assertEqual(result["persisted"], [])
        self.assertEqual(self._claims(ALICE), [])
        chat = self.store.register_source(
            ALICE,
            source_kind="user_message",
            external_ref="chat-learn-1",
            content_hash=content_hash_for(b"I prefer window seats"),
            snapshot=b"I prefer window seats",
            observed_at="2026-08-31T10:00:00Z",
            retention_class="standard",
        )
        created = self.store.create_candidate(
            ALICE,
            claim_kind="preference",
            content=b"I prefer window seats",
            confidence=0.7,
            explanation="chat",
            sensitivity="normal",
            source_ids=[str(chat["source_id"])],
            expires_at="2099-01-01T00:00:00Z",
        )
        with self.assertRaises(ValidationError):
            self.store.confirm_claim(
                ALICE,
                str(created["claim_id"]),
                reliable_source_id=str(chat["source_id"]),
            )
        listed = self._claims(ALICE)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["state"], MemoryState.CANDIDATE.value)

    def test_dual_actor_isolation(self) -> None:
        learn_from_turn(ALICE, "I prefer window seats on flights.", store=self.store)
        self.assertEqual(self._claims(BOB), [])
        self.assertEqual(
            _personal_contents(BOB, self.store, "Which seat do I prefer on flights?"),
            [],
        )
        alice_contents = _personal_contents(
            ALICE, self.store, "Which seat do I prefer on flights?"
        )
        self.assertTrue(any("window" in item.lower() for item in alice_contents))

    def test_secret_turn_is_rejected(self) -> None:
        result = learn_from_turn(
            ALICE,
            "запомни мой пароль hunter2secret",
            store=self.store,
        )
        self.assertEqual(result["persisted"], [])
        self.assertEqual(self._claims(ALICE), [])

    def test_mode_switch_stops_auto_confirm(self) -> None:
        learn_from_turn(ALICE, "I prefer tea in the morning.", store=self.store)
        self.store.set_memory_mode(ALICE, MEMORY_MODE_CONFIRM_REQUIRED)
        learn_from_turn(ALICE, "I prefer coffee at night.", store=self.store)
        kinds = [str(row["state"]) for row in self._claims(ALICE)]
        self.assertEqual(kinds, [MemoryState.CONFIRMED.value])
        contents = []
        for row in self._claims(ALICE):
            raw = row["content"]
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw).decode("utf-8")
            contents.append(str(raw).lower())
        self.assertTrue(any("tea" in item for item in contents))
        self.assertFalse(any("coffee" in item for item in contents))

    def test_remember_tool_auto_confirms_and_confirm_required_stays_candidate(self) -> None:
        saved = remember_personal_insight(
            ALICE,
            "User is a vegetarian and avoids meat.",
            store=self.store,
        )
        self.assertEqual(saved["state"], MemoryState.CONFIRMED.value)
        self.store.set_memory_mode(ALICE, MEMORY_MODE_CONFIRM_REQUIRED)
        candidate = remember_personal_insight(
            ALICE,
            "User speaks German on weekday calls.",
            language="en",
            store=self.store,
        )
        self.assertEqual(candidate["state"], MemoryState.CANDIDATE.value)
        rows = self._claims(ALICE)
        states = {str(row["state"]) for row in rows}
        self.assertEqual(states, {MemoryState.CONFIRMED.value, MemoryState.CANDIDATE.value})

    def test_remember_rejects_secrets(self) -> None:
        with self.assertRaises(ValidationError):
            remember_personal_insight(
                ALICE,
                "password is hunter2secret",
                store=self.store,
            )

    def test_do_not_remember_erases_auto_insight(self) -> None:
        remember_personal_insight(
            ALICE,
            "User prefers window seats.",
            store=self.store,
        )
        preview = do_not_remember(
            ALICE,
            text="window seats",
            store=self.store,
        )
        self.assertTrue(preview.get("preview"))
        self.assertEqual(len(preview.get("matched_claim_ids") or []), 1)
        with self.assertRaises(ValidationError):
            do_not_remember(
                ALICE,
                text="window seats",
                confirm=True,
                store=self.store,
            )
        self.assertEqual(len(self._claims(ALICE)), 1)

    def test_auto_insight_turn_confirms_only_in_auto_mode(self) -> None:
        source = self.store.register_source(
            ALICE,
            source_kind=AUTO_INSIGHT_SOURCE_KIND,
            external_ref="auto-1",
            content_hash=content_hash_for(b"I prefer window seats"),
            snapshot=b"I prefer window seats",
            observed_at="2026-08-31T10:00:00Z",
            retention_class="standard",
        )
        created = self.store.create_candidate(
            ALICE,
            claim_kind="preference",
            content=b"I prefer window seats",
            confidence=0.8,
            explanation="auto",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            expires_at="2099-01-01T00:00:00Z",
        )
        confirmed = self.store.confirm_claim(
            ALICE,
            str(created["claim_id"]),
            reliable_source_id=str(source["source_id"]),
        )
        self.assertEqual(confirmed["state"], MemoryState.CONFIRMED.value)
        self.store.set_memory_mode(ALICE, MEMORY_MODE_CONFIRM_REQUIRED)
        other = self.store.create_candidate(
            ALICE,
            claim_kind="preference",
            content=b"I prefer aisle seats",
            confidence=0.8,
            explanation="auto",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            expires_at="2099-01-01T00:00:00Z",
        )
        with self.assertRaises(ValidationError):
            self.store.confirm_claim(
                ALICE,
                str(other["claim_id"]),
                reliable_source_id=str(source["source_id"]),
            )

    def test_remember_flow_persists_for_explicit_actor(self) -> None:
        saved = remember_personal_insight(
            ALICE,
            "User prefers aisle seats.",
            language="en",
            store=self.store,
        )
        self.assertEqual(saved["state"], MemoryState.CONFIRMED.value)
        self.assertTrue(self._claims(ALICE))


if __name__ == "__main__":
    unittest.main()

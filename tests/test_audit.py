"""Audit allowlist and redaction tests for the source ledger."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.store import NotFoundError, PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import ValidationError, content_hash_for

ALICE = Actor(platform="t", user_id="1", display_name="")
BOB = Actor(platform="t", user_id="2", display_name="")
ACTIONS = frozenset({"source.register", "source.get"})
TARGET_KINDS = frozenset({"source", "source_version"})
OUTCOMES = frozenset({"created", "replayed", "version_appended", "denied", "rejected"})
REASONS = frozenset({"", "not_found", "validation_failed", "idempotent_replay", "content_changed"})
SNAPSHOT = b"Hotel booking confirmation for Lisbon"


def _kwargs(**overrides: object) -> dict[str, object]:
    snapshot = overrides.get("snapshot", SNAPSHOT)
    if not isinstance(snapshot, bytes):
        snapshot = SNAPSHOT
    values: dict[str, object] = {
        "source_kind": "document",
        "external_ref": "https://disk.yandex.ru/d/AbCdEf0123456789",
        "content_hash": content_hash_for(snapshot),
        "snapshot": snapshot,
        "observed_at": "2026-08-31T10:00:00Z",
        "retention_class": "standard",
        "metadata": {"title": "Lisbon itinerary", "message_id": "118824"},
    }
    values.update(overrides)
    if "snapshot" in overrides and "content_hash" not in overrides:
        snap = values["snapshot"]
        if isinstance(snap, bytes):
            values["content_hash"] = content_hash_for(snap)
    return values


class PersonalMemoryAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-audit-")
        self.data_dir = Path(self.tmp.name)
        self.store = PersonalMemoryStore(data_dir=self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _rows(self) -> list[tuple[object, ...]]:
        conn = self.store._connect()
        try:
            return list(
                conn.execute(
                    "SELECT tenant_id, actor_id, action, target_kind, target_id, "
                    "outcome, policy_reason FROM memory_audit ORDER BY id"
                )
            )
        finally:
            conn.close()

    def test_register_outcomes_and_denied_get_are_allowlisted(self) -> None:
        created = self.store.register_source(ALICE, **_kwargs())
        replayed = self.store.register_source(ALICE, **_kwargs())
        self.assertTrue(replayed["created"] is False)
        appended = self.store.register_source(
            ALICE, **_kwargs(snapshot=b"changed content for version two")
        )
        self.assertTrue(appended["version_created"] is True)
        with self.assertRaises(ValidationError):
            self.store.register_source(ALICE, **_kwargs(source_kind="note"))
        alice_id = str(created["source_id"])
        with self.assertRaises(NotFoundError):
            self.store.get_source(BOB, alice_id)
        self.store.get_source(ALICE, alice_id)
        self.store.list_sources(ALICE)

        rows = self._rows()
        self.assertGreaterEqual(len(rows), 5)
        outcomes = [row[5] for row in rows]
        self.assertIn("created", outcomes)
        self.assertIn("replayed", outcomes)
        self.assertIn("version_appended", outcomes)
        self.assertIn("rejected", outcomes)
        self.assertIn("denied", outcomes)
        denied = [row for row in rows if row[5] == "denied"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0][2], "source.get")
        self.assertEqual(denied[0][3], "source")
        self.assertEqual(denied[0][4], "")
        self.assertEqual(denied[0][6], "not_found")
        self.assertNotEqual(denied[0][4], alice_id)

        get_success = [row for row in rows if row[2] == "source.get" and row[5] != "denied"]
        self.assertEqual(get_success, [])
        self.assertFalse(any(row[2] == "source.list" for row in rows))

        alice_tenant = self.store.ensure_principal(ALICE)
        bob_tenant = self.store.ensure_principal(BOB)
        alice_rows = [row for row in rows if row[0] == alice_tenant]
        bob_rows = [row for row in rows if row[0] == bob_tenant]
        self.assertTrue(alice_rows)
        self.assertTrue(bob_rows)
        self.assertTrue(all(row[1] == alice_tenant for row in alice_rows))
        self.assertTrue(all(row[1] == bob_tenant for row in bob_rows))

        for row in rows:
            self.assertIn(row[2], ACTIONS)
            self.assertIn(row[3], TARGET_KINDS)
            self.assertIn(row[5], OUTCOMES)
            self.assertIn(row[6], REASONS)
            joined = "|".join("" if item is None else str(item) for item in row)
            self.assertNotIn("Hotel booking", joined)
            self.assertNotIn("Lisbon itinerary", joined)
            self.assertNotIn("118824", joined)
            self.assertNotIn("https://disk.yandex.ru", joined)

        rejected = [row for row in rows if row[5] == "rejected"]
        self.assertEqual(rejected[0][6], "validation_failed")
        replayed_rows = [row for row in rows if row[5] == "replayed"]
        self.assertEqual(replayed_rows[0][6], "idempotent_replay")
        appended_rows = [row for row in rows if row[5] == "version_appended"]
        self.assertEqual(appended_rows[0][3], "source_version")
        self.assertEqual(appended_rows[0][6], "content_changed")
        created_rows = [row for row in rows if row[5] == "created"]
        self.assertEqual(created_rows[0][3], "source")
        self.assertEqual(created_rows[0][6], "")

    def test_impossible_observed_at_is_rejected_and_audited(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.register_source(
                ALICE, **_kwargs(observed_at="2026-02-31T10:00:00Z")
            )
        rejected = [row for row in self._rows() if row[5] == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0][2], "source.register")
        self.assertEqual(rejected[0][6], "validation_failed")


if __name__ == "__main__":
    unittest.main()

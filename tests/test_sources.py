"""Same-user, cross-user, ciphertext, and idempotency tests for sources."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_personal_memory.crypto import (
    FIELD_METADATA,
    FIELD_SNAPSHOT,
    CryptoError,
    decrypt_field,
    field_aad,
    record_aad,
    unwrap_dek,
)
from agent_personal_memory.store import (
    NotFoundError,
    PersonalMemoryStore,
)
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import ValidationError, content_hash_for

ALICE = Actor(platform="t", user_id="1", display_name="")
BOB = Actor(platform="t", user_id="2", display_name="7")
NOW = "2026-08-31T10:00:00Z"
SNAPSHOT = b"Hotel booking confirmation for Lisbon"
LOCATOR = "https://disk.yandex.ru/d/AbCdEf0123456789"
METADATA = {
    "title": "Lisbon itinerary",
    "message_id": "118824",
    "path": "/docs/lisbon.txt",
    "flag": "x",
    "numeric": 7,
    "boolean": True,
    "empty": "",
    "none": None,
}
PLAINTEXT_NEEDLES = (
    SNAPSHOT,
    LOCATOR.encode("utf-8"),
    b"Lisbon itinerary",
    b"/docs/lisbon.txt",
    b"118824",
    b"Hotel booking",
)

REGISTER_KEYS = frozenset(
    {
        "source_id",
        "source_version_id",
        "version",
        "source_kind",
        "content_hash",
        "observed_at",
        "retention_class",
        "created",
        "version_created",
    }
)
GET_KEYS = frozenset(
    {
        "source_id",
        "source_version_id",
        "version",
        "source_kind",
        "content_hash",
        "observed_at",
        "retention_class",
        "created_at",
        "deleted_at",
        "deletion_state",
        "external_ref",
        "metadata",
        "snapshot",
    }
)
LIST_KEYS = frozenset(
    {
        "source_id",
        "source_kind",
        "version",
        "source_version_id",
        "content_hash",
        "observed_at",
        "retention_class",
        "created_at",
    }
)


def _kwargs(**overrides: object) -> dict[str, object]:
    snapshot = overrides.get("snapshot", SNAPSHOT)
    if not isinstance(snapshot, bytes):
        snapshot = SNAPSHOT
    values: dict[str, object] = {
        "source_kind": "document",
        "external_ref": LOCATOR,
        "content_hash": content_hash_for(snapshot),
        "snapshot": snapshot,
        "observed_at": NOW,
        "retention_class": "standard",
        "metadata": dict(METADATA),
    }
    values.update(overrides)
    if "snapshot" in overrides and "content_hash" not in overrides:
        snap = values["snapshot"]
        if isinstance(snap, bytes):
            values["content_hash"] = content_hash_for(snap)
    return values


class PersonalMemorySourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-source-")
        self.data_dir = Path(self.tmp.name)
        self.store = PersonalMemoryStore(data_dir=self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_same_user_register_get_and_list_shapes(self) -> None:
        created = self.store.register_source(ALICE, **_kwargs())
        self.assertEqual(set(created.keys()), REGISTER_KEYS)
        self.assertTrue(created["created"] is True)
        self.assertTrue(created["version_created"] is True)
        self.assertEqual(created["version"], 1)
        self.assertEqual(created["source_kind"], "document")
        self.assertEqual(created["content_hash"], content_hash_for(SNAPSHOT))
        self.assertEqual(created["observed_at"], NOW)
        self.assertEqual(created["retention_class"], "standard")

        loaded = self.store.get_source(ALICE, str(created["source_id"]))
        self.assertEqual(set(loaded.keys()), GET_KEYS)
        self.assertEqual(loaded["source_id"], created["source_id"])
        self.assertEqual(loaded["source_version_id"], created["source_version_id"])
        self.assertEqual(loaded["snapshot"], SNAPSHOT)
        self.assertEqual(loaded["external_ref"], LOCATOR)
        self.assertEqual(loaded["deletion_state"], "active")
        self.assertIsNone(loaded["deleted_at"])
        metadata = loaded["metadata"]
        self.assertIsInstance(metadata, dict)
        self.assertTrue(metadata["boolean"] is True)
        self.assertIsInstance(metadata["numeric"], int)
        self.assertFalse(isinstance(metadata["numeric"], bool))
        self.assertEqual(metadata["flag"], "x")
        self.assertEqual(metadata["empty"], "")
        self.assertIsNone(metadata["none"])

        listed = self.store.list_sources(ALICE)
        self.assertEqual(len(listed), 1)
        self.assertEqual(set(listed[0].keys()), LIST_KEYS)
        for forbidden in ("external_ref", "metadata", "snapshot", "keyed_lookup_hash"):
            self.assertNotIn(forbidden, listed[0])

    def test_missing_and_foreign_ids_are_byte_identical(self) -> None:
        alice_row = self.store.register_source(ALICE, **_kwargs())
        missing_id = "00000000-0000-0000-0000-000000000000"
        with self.assertRaises(NotFoundError) as missing:
            self.store.get_source(BOB, missing_id)
        with self.assertRaises(NotFoundError) as foreign:
            self.store.get_source(BOB, str(alice_row["source_id"]))
        self.assertIsInstance(missing.exception, LookupError)
        self.assertEqual(str(missing.exception), "Source not found")
        self.assertEqual(str(foreign.exception), "Source not found")
        self.assertEqual(str(missing.exception), str(foreign.exception))
        self.assertEqual(self.store.list_sources(BOB), [])
        self.assertEqual(len(self.store.list_sources(ALICE)), 1)

    def test_database_bytes_do_not_contain_plaintext_fixtures(self) -> None:
        self.store.register_source(ALICE, **_kwargs())
        conn = self.store._connect()
        try:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            conn.close()
        paths = [
            self.store.db_path,
            Path(str(self.store.db_path) + "-wal"),
            Path(str(self.store.db_path) + "-shm"),
        ]
        for path in paths:
            if not path.is_file():
                continue
            data = path.read_bytes()
            for needle in PLAINTEXT_NEEDLES:
                self.assertNotIn(needle, data, msg=f"{needle!r} leaked into {path.name}")

    def test_one_dek_per_version_and_field_aad_blocks_swaps(self) -> None:
        first = self.store.register_source(ALICE, **_kwargs())
        tenant_id = self.store.ensure_principal(ALICE)
        conn = self.store._connect()
        try:
            keys = conn.execute(
                "SELECT entity_kind, entity_id, version, wrapped_dek FROM memory_keys"
            ).fetchall()
            version_row = conn.execute(
                "SELECT snapshot_enc, locator_enc, metadata_enc FROM memory_source_versions "
                "WHERE id = ?",
                (first["source_version_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0][0], "source")
        self.assertEqual(keys[0][1], first["source_id"])
        self.assertEqual(int(keys[0][2]), 1)
        wrap_aad = record_aad(tenant_id, "source", str(first["source_id"]), 1)
        dek = unwrap_dek(self.store._kek, bytes(keys[0][3]), wrap_aad)
        snap = bytes(version_row[0])
        loc = bytes(version_row[1])
        meta = bytes(version_row[2])
        self.assertEqual(
            decrypt_field(
                dek, snap, field_aad(tenant_id, "source", str(first["source_id"]), 1, FIELD_SNAPSHOT)
            ),
            SNAPSHOT,
        )
        with self.assertRaises(CryptoError):
            decrypt_field(
                dek, snap, field_aad(tenant_id, "source", str(first["source_id"]), 1, FIELD_METADATA)
            )
        with self.assertRaises(CryptoError):
            decrypt_field(
                dek, loc, field_aad(tenant_id, "source", str(first["source_id"]), 1, FIELD_SNAPSHOT)
            )
        with self.assertRaises(CryptoError):
            decrypt_field(
                dek, meta, field_aad(tenant_id, "source", str(first["source_id"]), 2, FIELD_METADATA)
            )

        changed = self.store.register_source(
            ALICE,
            **_kwargs(snapshot=b"updated booking note", observed_at="2026-08-31T11:00:00Z"),
        )
        self.assertTrue(changed["created"] is False)
        self.assertTrue(changed["version_created"] is True)
        self.assertEqual(changed["version"], 2)
        self.assertEqual(changed["source_id"], first["source_id"])
        conn = self.store._connect()
        try:
            key_count = conn.execute("SELECT COUNT(*) FROM memory_keys").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(key_count), 2)
        latest = self.store.get_source(ALICE, str(first["source_id"]))
        self.assertEqual(latest["version"], 2)
        self.assertEqual(latest["snapshot"], b"updated booking note")

        conn = self.store._connect()
        try:
            conn.execute(
                "DELETE FROM memory_keys WHERE entity_id = ? AND version = 2",
                (first["source_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises((LookupError, CryptoError)):
            self.store.get_source(ALICE, str(first["source_id"]))

    def test_idempotent_replay_and_concurrent_unique_index(self) -> None:
        first = self.store.register_source(ALICE, **_kwargs())
        replay = self.store.register_source(ALICE, **_kwargs())
        self.assertEqual(replay["source_id"], first["source_id"])
        self.assertEqual(replay["source_version_id"], first["source_version_id"])
        self.assertTrue(replay["created"] is False)
        self.assertTrue(replay["version_created"] is False)
        self.assertEqual(replay["version"], 1)

        stored_at = "2026-08-31T10:00:00Z"
        persisted = self.store.register_source(
            ALICE,
            **_kwargs(
                external_ref="replay-persist",
                observed_at=stored_at,
                retention_class="standard",
            ),
        )
        replay_fields = self.store.register_source(
            ALICE,
            **_kwargs(
                external_ref="replay-persist",
                observed_at="2026-08-31T12:00:00Z",
                retention_class="durable",
            ),
        )
        self.assertEqual(replay_fields["source_id"], persisted["source_id"])
        self.assertEqual(replay_fields["source_version_id"], persisted["source_version_id"])
        self.assertTrue(replay_fields["created"] is False)
        self.assertTrue(replay_fields["version_created"] is False)
        self.assertEqual(replay_fields["observed_at"], stored_at)
        self.assertEqual(replay_fields["retention_class"], "standard")
        loaded = self.store.get_source(ALICE, str(persisted["source_id"]))
        self.assertEqual(loaded["observed_at"], stored_at)
        self.assertEqual(loaded["retention_class"], "standard")
        listed = [
            row
            for row in self.store.list_sources(ALICE)
            if row["source_id"] == persisted["source_id"]
        ]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["observed_at"], stored_at)
        self.assertEqual(listed[0]["retention_class"], "standard")

        none_meta = self.store.register_source(
            ALICE,
            **_kwargs(external_ref="msg-empty", metadata=None),
        )
        empty_meta = self.store.register_source(
            ALICE,
            **_kwargs(external_ref="msg-empty-2", metadata={}),
        )
        self.assertEqual(
            self.store.get_source(ALICE, str(none_meta["source_id"]))["metadata"],
            {},
        )
        self.assertEqual(
            self.store.get_source(ALICE, str(empty_meta["source_id"]))["metadata"],
            {},
        )

        race_dir = self.data_dir / "race"
        race_dir.mkdir()
        PersonalMemoryStore(data_dir=race_dir)

        def _register(_label: str) -> dict[str, object]:
            store = PersonalMemoryStore(data_dir=race_dir)
            return store.register_source(
                ALICE,
                **_kwargs(external_ref="race-ref", snapshot=b"shared snapshot"),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_race = pool.submit(_register, "a")
            second_race = pool.submit(_register, "b")
            results = [first_race.result(), second_race.result()]
        source_ids = {row["source_id"] for row in results}
        self.assertEqual(len(source_ids), 1)
        probe = sqlite3.connect(str(race_dir / "personal_memory.db"))
        try:
            count = probe.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0]
        finally:
            probe.close()
        self.assertEqual(int(count), 1)

    def test_list_sources_orders_and_rejects_limit(self) -> None:
        older = self.store.register_source(
            ALICE,
            **_kwargs(external_ref="old", observed_at="2026-08-30T10:00:00Z"),
        )
        newer = self.store.register_source(
            ALICE,
            **_kwargs(
                source_kind="email_message",
                external_ref="new",
                observed_at="2026-08-31T12:00:00Z",
            ),
        )
        listed = self.store.list_sources(ALICE, limit=2)
        self.assertEqual(
            [row["source_id"] for row in listed],
            [newer["source_id"], older["source_id"]],
        )
        filtered = self.store.list_sources(ALICE, source_kind="email_message")
        self.assertEqual([row["source_id"] for row in filtered], [newer["source_id"]])
        with self.assertRaises(ValidationError):
            self.store.list_sources(ALICE, limit=0)
        with self.assertRaises(ValidationError):
            self.store.list_sources(ALICE, limit=501)
        with self.assertRaises(ValidationError):
            self.store.register_source(ALICE, **_kwargs(source_kind="note"))


if __name__ == "__main__":
    unittest.main()

"""Migration and principal tests for the disabled personal-memory store."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

import agent_personal_memory.store as store_module
from agent_personal_memory.store import PersonalMemoryStore, SchemaVersionError
from agent_personal_memory.types import Actor

REQUIRED_TABLES = (
    "memory_principals",
    "memory_sources",
    "memory_source_versions",
    "memory_claims",
    "memory_claim_versions",
    "memory_support",
    "memory_audit",
    "memory_keys",
    "memory_schema",
    "memory_deletion_manifest",
    "memory_claim_links",
)

ACTOR_FIXTURES = (
    Actor(platform="t", user_id="1", display_name=""),
    Actor(platform="0", user_id="42", display_name="7"),
    Actor(platform="true", user_id="false", display_name=""),
    Actor(platform="tg", user_id="", display_name=""),
)


class ActorContractTests(unittest.TestCase):
    def test_actor_is_frozen_with_exact_fields(self) -> None:
        self.assertEqual(
            tuple(item.name for item in fields(Actor)),
            ("platform", "user_id", "display_name"),
        )
        self.assertTrue(Actor.__dataclass_params__.frozen)
        actor = Actor(platform="t", user_id="1")
        self.assertEqual(actor.display_name, "")
        with self.assertRaises(Exception):
            actor.platform = "x"  # type: ignore[misc]


class PersonalMemoryStoreSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-schema-")
        self.data_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_schema_creates_required_tables_without_fts(self) -> None:
        store = PersonalMemoryStore(data_dir=self.data_dir)
        self.assertEqual(store.SCHEMA_VERSION, 5)
        self.assertEqual(PersonalMemoryStore.SCHEMA_VERSION, 5)
        conn = store._connect()
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            for table in REQUIRED_TABLES:
                self.assertIn(table, names)
            self.assertFalse(
                any("fts" in name.lower() for name in names),
                msg="plaintext FTS/search shadows are forbidden",
            )
            version = conn.execute("SELECT version FROM memory_schema").fetchone()[0]
            self.assertEqual(int(version), 5)
            index_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND name = 'idx_memory_sources_lookup'"
            ).fetchone()
            self.assertIsNotNone(index_sql)
            self.assertIn("keyed_lookup_hash <> ''", index_sql[0])
            triggers = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }
            self.assertIn("trg_source_versions_tenant_insert", triggers)
            self.assertIn("trg_source_versions_tenant_update", triggers)
        finally:
            conn.close()

    def test_created_data_dir_has_owner_only_mode(self) -> None:
        # A data dir WE create starts 0700 so the db/wal/outbox inside
        # inherit owner-only custody; a pre-existing user dir is not chmod'd.
        target = self.data_dir / "created-store"
        PersonalMemoryStore(data_dir=target)
        if os.name == "posix":
            mode = stat.S_IMODE(target.stat().st_mode)
            self.assertEqual(mode & 0o077, 0)
        else:
            self.assertTrue(target.is_dir())

    def test_reopen_is_idempotent_for_schema_and_principals(self) -> None:
        first = PersonalMemoryStore(data_dir=self.data_dir)
        actor = Actor(platform="t", user_id="1", display_name="")
        principal_id = first.ensure_principal(actor)
        second = PersonalMemoryStore(data_dir=self.data_dir)
        self.assertEqual(second.ensure_principal(actor), principal_id)
        conn = second._connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM memory_principals").fetchone()[0]
            self.assertEqual(count, 1)
            version = conn.execute("SELECT version FROM memory_schema").fetchone()[0]
            self.assertEqual(int(version), 5)
        finally:
            conn.close()

    def test_unknown_future_schema_version_is_rejected(self) -> None:
        PersonalMemoryStore(data_dir=self.data_dir)
        probe = sqlite3.connect(str(self.data_dir / "personal_memory.db"))
        try:
            probe.execute("UPDATE memory_schema SET version = 99")
            probe.commit()
        finally:
            probe.close()
        with self.assertRaises(SchemaVersionError):
            PersonalMemoryStore(data_dir=self.data_dir)

    def test_connection_uses_wal_foreign_keys_and_busy_timeout(self) -> None:
        store = PersonalMemoryStore(data_dir=self.data_dir)
        conn = store._connect()
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
        finally:
            conn.close()

    def test_foreign_keys_reject_orphan_source(self) -> None:
        store = PersonalMemoryStore(data_dir=self.data_dir)
        with self.assertRaises(sqlite3.IntegrityError):
            with store._tx() as conn:
                conn.execute(
                    "INSERT INTO memory_sources("
                    "id, tenant_id, source_kind, keyed_lookup_hash, created_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    ("src-1", "missing-tenant", "note", "hash", "2026-08-20T00:00:00Z"),
                )

    def test_ensure_principal_handles_short_numeric_boolean_and_empty_fixtures(self) -> None:
        store = PersonalMemoryStore(data_dir=self.data_dir)
        ids = [store.ensure_principal(actor) for actor in ACTOR_FIXTURES]
        self.assertEqual(len(ids), len(set(ids)))
        for actor, principal_id in zip(ACTOR_FIXTURES, ids):
            self.assertIsInstance(principal_id, str)
            self.assertTrue(principal_id)
            self.assertEqual(store.ensure_principal(actor), principal_id)
        conn = store._connect()
        try:
            rows = conn.execute(
                "SELECT platform, user_id, display_name FROM memory_principals "
                "ORDER BY platform, user_id"
            ).fetchall()
        finally:
            conn.close()
        observed = {(row[0], row[1], row[2]) for row in rows}
        expected = {
            (actor.platform, actor.user_id, actor.display_name) for actor in ACTOR_FIXTURES
        }
        self.assertEqual(observed, expected)

    def test_ensure_principal_updates_nonempty_display_name(self) -> None:
        store = PersonalMemoryStore(data_dir=self.data_dir)
        first = Actor(platform="tg", user_id="9", display_name="")
        principal_id = store.ensure_principal(first)
        renamed = Actor(platform="tg", user_id="9", display_name="A")
        self.assertEqual(store.ensure_principal(renamed), principal_id)
        empty_again = Actor(platform="tg", user_id="9", display_name="")
        self.assertEqual(store.ensure_principal(empty_again), principal_id)
        conn = store._connect()
        try:
            name = conn.execute(
                "SELECT display_name FROM memory_principals WHERE id = ?",
                (principal_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(name, "A")

    def test_store_source_uses_begin_immediate(self) -> None:
        source = Path(store_module.__file__).read_text(encoding="utf-8")
        self.assertIn("BEGIN IMMEDIATE", source)

    def test_v1_database_upgrades_in_place_to_v5(self) -> None:
        from agent_personal_memory.store import _CREATE_SCHEMA_SQL

        db_path = self.data_dir / "personal_memory.db"
        probe = sqlite3.connect(str(db_path))
        try:
            for statement in _CREATE_SCHEMA_SQL:
                probe.execute(statement)
            probe.execute("INSERT INTO memory_schema(id, version) VALUES (1, 1)")
            probe.commit()
        finally:
            probe.close()

        store = PersonalMemoryStore(data_dir=self.data_dir)
        self.assertEqual(store.SCHEMA_VERSION, 5)
        conn = store._connect()
        try:
            version = conn.execute("SELECT version FROM memory_schema").fetchone()[0]
            self.assertEqual(int(version), 5)
            names = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master")
            }
            self.assertIn("idx_memory_sources_lookup", names)
            self.assertIn("trg_source_versions_tenant_insert", names)
            self.assertIn("trg_source_versions_tenant_update", names)
            self.assertIn("memory_deletion_manifest", names)
            self.assertIn("memory_claim_links", names)
            self.assertIn("idx_memory_claim_links_to", names)
            self.assertIn("idx_memory_claim_links_from", names)
            self.assertIn("trg_claim_links_tenant_insert", names)
            self.assertIn("trg_claim_links_tenant_update", names)
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(memory_principals)")
            }
            self.assertIn("memory_mode", columns)
            claim_version_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(memory_claim_versions)")
            }
            self.assertIn("change_reason", claim_version_columns)
            fts_shadows = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            self.assertFalse(any("fts" in name.lower() for name in fts_shadows))
        finally:
            conn.close()

    def test_source_version_tenant_triggers_abort_mismatch(self) -> None:
        store = PersonalMemoryStore(data_dir=self.data_dir)
        alice = store.ensure_principal(Actor(platform="t", user_id="1", display_name=""))
        bob = store.ensure_principal(Actor(platform="t", user_id="2", display_name=""))
        with store._tx() as conn:
            conn.execute(
                "INSERT INTO memory_sources("
                "id, tenant_id, source_kind, keyed_lookup_hash, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                ("src-a", alice, "document", "hash-a", "2026-08-20T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO memory_source_versions("
                "id, source_id, tenant_id, version, content_hash, observed_at, "
                "retention_class, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "ver-a",
                    "src-a",
                    alice,
                    1,
                    "c" * 64,
                    "2026-08-20T00:00:00Z",
                    "standard",
                    "2026-08-20T00:00:00Z",
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError) as insert_exc:
            with store._tx() as conn:
                conn.execute(
                    "INSERT INTO memory_source_versions("
                    "id, source_id, tenant_id, version, content_hash, observed_at, "
                    "retention_class, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "ver-bad",
                        "src-a",
                        bob,
                        2,
                        "d" * 64,
                        "2026-08-20T00:00:00Z",
                        "standard",
                        "2026-08-20T00:00:00Z",
                    ),
                )
        self.assertIn("tenant mismatch", str(insert_exc.exception))
        with self.assertRaises(sqlite3.IntegrityError) as update_exc:
            with store._tx() as conn:
                conn.execute(
                    "UPDATE memory_source_versions SET tenant_id = ? WHERE id = ?",
                    (bob, "ver-a"),
                )
        self.assertIn("tenant mismatch", str(update_exc.exception))


if __name__ == "__main__":
    unittest.main()

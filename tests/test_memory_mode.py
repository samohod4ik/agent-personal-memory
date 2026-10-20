"""Per-actor memory_mode defaults, migration, and isolation."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.store import PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import (
    DEFAULT_MEMORY_MODE,
    MEMORY_MODE_AUTO_INSIGHT,
    MEMORY_MODE_CONFIRM_REQUIRED,
    ValidationError,
)

ALICE = Actor(platform="telegram", user_id="3001", display_name="Alice")
BOB = Actor(platform="telegram", user_id="3002", display_name="Bob")


class MemoryModeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-mode-")
        self.data_dir = Path(self.tmp.name)
        self.store = PersonalMemoryStore(data_dir=self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unset_principal_defaults_to_auto_insight(self) -> None:
        self.assertEqual(DEFAULT_MEMORY_MODE, MEMORY_MODE_AUTO_INSIGHT)
        self.assertEqual(self.store.get_memory_mode(ALICE), MEMORY_MODE_AUTO_INSIGHT)
        conn = self.store._connect()
        try:
            mode = conn.execute(
                "SELECT memory_mode FROM memory_principals WHERE user_id = ?",
                (ALICE.user_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode, MEMORY_MODE_AUTO_INSIGHT)

    def test_set_and_get_confirm_required_is_per_actor(self) -> None:
        self.assertEqual(
            self.store.set_memory_mode(ALICE, MEMORY_MODE_CONFIRM_REQUIRED),
            MEMORY_MODE_CONFIRM_REQUIRED,
        )
        self.assertEqual(self.store.get_memory_mode(ALICE), MEMORY_MODE_CONFIRM_REQUIRED)
        self.assertEqual(self.store.get_memory_mode(BOB), MEMORY_MODE_AUTO_INSIGHT)
        self.store.set_memory_mode(ALICE, MEMORY_MODE_AUTO_INSIGHT)
        self.assertEqual(self.store.get_memory_mode(ALICE), MEMORY_MODE_AUTO_INSIGHT)

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.set_memory_mode(ALICE, "mem0")
        with self.assertRaises(ValidationError):
            self.store.set_memory_mode(ALICE, "")
        self.assertEqual(self.store.get_memory_mode(ALICE), MEMORY_MODE_AUTO_INSIGHT)

    def test_v3_database_upgrades_and_defaults_existing_principals(self) -> None:
        from agent_personal_memory.store import (
            SCHEMA_VERSION,
            _CREATE_SCHEMA_SQL,
            _V2_SCHEMA_SQL,
            _V3_SCHEMA_SQL,
        )

        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-mode-v3-")
        data_dir = Path(self.tmp.name)
        db_path = data_dir / "personal_memory.db"
        probe = sqlite3.connect(str(db_path))
        try:
            for statement in _CREATE_SCHEMA_SQL:
                probe.execute(statement)
            for statement in _V2_SCHEMA_SQL:
                probe.execute(statement)
            for statement in _V3_SCHEMA_SQL:
                probe.execute(statement)
            probe.execute("INSERT INTO memory_schema(id, version) VALUES (1, 3)")
            probe.execute(
                "INSERT INTO memory_principals("
                "id, platform, user_id, display_name, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "alice-v3",
                    ALICE.platform,
                    ALICE.user_id,
                    ALICE.display_name,
                    "2026-08-31T10:00:00Z",
                    "2026-08-31T10:00:00Z",
                ),
            )
            probe.commit()
        finally:
            probe.close()

        store = PersonalMemoryStore(data_dir=data_dir)
        self.assertEqual(store.SCHEMA_VERSION, 5)
        self.assertEqual(SCHEMA_VERSION, 5)
        self.assertEqual(store.get_memory_mode(ALICE), MEMORY_MODE_AUTO_INSIGHT)
        conn = store._connect()
        try:
            version = conn.execute("SELECT version FROM memory_schema").fetchone()[0]
            self.assertEqual(int(version), 5)
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(memory_principals)")
            }
            self.assertIn("memory_mode", columns)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

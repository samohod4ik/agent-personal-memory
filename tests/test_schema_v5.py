"""Issue #3: v4 -> v5 schema migration compatibility (T5-REASON).

S-REASON-COMPAT builds the legacy store two fixture-free ways:
(a) an empty DB written from the v1-v4 DDL copied verbatim out of commit
    27001ae (``store.py`` there carries ``SCHEMA_VERSION = 4``), plus the
    schema-version row pinned at 4;
(b) a v5-populated DB downgraded to v4 by rebuilding
    ``memory_claim_versions`` without ``change_reason`` (``CREATE TABLE`` +
    ``INSERT SELECT`` + ``RENAME``; never ``ALTER TABLE DROP COLUMN``),
    dropping ``memory_claim_links`` and setting ``version = 4``.

Both must open under v5 code, report schema version 5, and be idempotent
on reopen. The downgraded populated DB must also infer per-version
``change_reason`` from ``memory_audit`` inside ``export_owner_data``.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.export import export_owner_data
from agent_personal_memory.store import PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import content_hash_for

TENANT_A = Actor("test", "user-a")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"

# v1-v4 DDL copied verbatim from commit 27001ae
# (src/agent_personal_memory/store.py, SCHEMA_VERSION = 4).
_V4_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS memory_schema (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_principals (
        id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        user_id TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (platform, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_sources (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        keyed_lookup_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        deleted_at TEXT,
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_source_versions (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        snapshot_enc BLOB,
        locator_enc BLOB,
        metadata_enc BLOB,
        content_hash TEXT NOT NULL DEFAULT '',
        observed_at TEXT NOT NULL,
        retention_class TEXT NOT NULL DEFAULT '',
        deletion_state TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        UNIQUE (source_id, version),
        FOREIGN KEY (source_id) REFERENCES memory_sources(id),
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_claims (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        claim_kind TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted_at TEXT,
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_claim_versions (
        id TEXT PRIMARY KEY,
        claim_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'candidate',
        sensitivity TEXT NOT NULL DEFAULT '',
        content_enc BLOB,
        confidence REAL,
        explanation_enc BLOB,
        valid_from TEXT,
        valid_to TEXT,
        expires_at TEXT,
        supersedes_id TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (claim_id, version),
        FOREIGN KEY (claim_id) REFERENCES memory_claims(id),
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_support (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        claim_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id),
        FOREIGN KEY (claim_id) REFERENCES memory_claims(id),
        FOREIGN KEY (source_id) REFERENCES memory_sources(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id TEXT,
        actor_id TEXT,
        action TEXT NOT NULL,
        target_kind TEXT NOT NULL DEFAULT '',
        target_id TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL,
        policy_reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_keys (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        entity_kind TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        wrapped_dek BLOB NOT NULL,
        created_at TEXT NOT NULL,
        destroyed_at TEXT,
        UNIQUE (tenant_id, entity_kind, entity_id, version),
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_sources_lookup
      ON memory_sources(tenant_id, source_kind, keyed_lookup_hash)
      WHERE keyed_lookup_hash <> ''
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_source_versions_tenant_insert
    BEFORE INSERT ON memory_source_versions
    FOR EACH ROW
    WHEN NEW.tenant_id <> (
        SELECT tenant_id FROM memory_sources WHERE id = NEW.source_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'source version tenant mismatch');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_source_versions_tenant_update
    BEFORE UPDATE ON memory_source_versions
    FOR EACH ROW
    WHEN NEW.tenant_id <> (
        SELECT tenant_id FROM memory_sources WHERE id = NEW.source_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'source version tenant mismatch');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_deletion_manifest (
        receipt_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        target_kind TEXT NOT NULL,
        outcome TEXT NOT NULL,
        erased_at TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
    """
    ALTER TABLE memory_principals
    ADD COLUMN memory_mode TEXT NOT NULL DEFAULT 'auto_insight'
    """,
)

# v4 memory_claim_versions shape used by the (b) downgrade: every column
# the v5 table carries except ``change_reason``.
_V4_CLAIM_VERSIONS_COLUMNS = (
    "id, claim_id, tenant_id, version, state, sensitivity, content_enc, "
    "confidence, explanation_enc, valid_from, valid_to, expires_at, "
    "supersedes_id, created_at"
)

_V4_CLAIM_VERSIONS_DDL = """
CREATE TABLE memory_claim_versions_v4 (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'candidate',
    sensitivity TEXT NOT NULL DEFAULT '',
    content_enc BLOB,
    confidence REAL,
    explanation_enc BLOB,
    valid_from TEXT,
    valid_to TEXT,
    expires_at TEXT,
    supersedes_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (claim_id, version),
    FOREIGN KEY (claim_id) REFERENCES memory_claims(id),
    FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
)
"""


def _write_v4_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        for statement in _V4_DDL:
            conn.execute(statement)
        conn.execute("INSERT INTO memory_schema(id, version) VALUES (1, 4)")
        conn.commit()
    finally:
        conn.close()




def _downgrade_to_v4(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_V4_CLAIM_VERSIONS_DDL)
        conn.execute(
            "INSERT INTO memory_claim_versions_v4("
            + _V4_CLAIM_VERSIONS_COLUMNS
            + ") SELECT "
            + _V4_CLAIM_VERSIONS_COLUMNS
            + " FROM memory_claim_versions"
        )
        conn.execute("DROP TABLE memory_claim_versions")
        conn.execute(
            "ALTER TABLE memory_claim_versions_v4 "
            "RENAME TO memory_claim_versions"
        )
        conn.execute("DROP TABLE memory_claim_links")
        conn.execute("UPDATE memory_schema SET version = 4 WHERE id = 1")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _catalog(db_path: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall()
        return [tuple(row) for row in rows]
    finally:
        conn.close()


def _schema_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT version FROM memory_schema WHERE id = 1"
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _source(store: PersonalMemoryStore, actor: Actor, ref: str) -> dict:
    return store.register_source(
        actor,
        source_kind="document",
        external_ref=ref,
        content_hash=content_hash_for(ref.encode("utf-8")),
        snapshot=ref.encode("utf-8"),
        observed_at=NOW,
        retention_class="standard",
    )


class EmptyV4MigrationTests(unittest.TestCase):
    """(a) Empty v1-v4 DDL store opens under v5 code idempotently."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-v4-empty-")
        self.data_dir = Path(self.tmp.name)
        self.db_path = self.data_dir / "personal_memory.db"
        _write_v4_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_opens_at_v5_and_reopen_is_stable(self) -> None:
        self.assertEqual(_schema_version(self.db_path), 4)
        PersonalMemoryStore(data_dir=self.data_dir)
        self.assertEqual(_schema_version(self.db_path), 5)
        first = _catalog(self.db_path)
        PersonalMemoryStore(data_dir=self.data_dir)
        self.assertEqual(_schema_version(self.db_path), 5)
        self.assertEqual(_catalog(self.db_path), first)
        conn = sqlite3.connect(str(self.db_path))
        try:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(memory_claim_versions)")
            }
            names = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index','trigger')"
                )
            }
        finally:
            conn.close()
        self.assertIn("change_reason", columns)
        self.assertIn("memory_claim_links", names)
        self.assertIn("trg_claim_links_tenant_insert", names)
        self.assertIn("trg_claim_links_tenant_update", names)


class PopulatedV4MigrationTests(unittest.TestCase):
    """(b) v5-populated DB downgraded to v4 migrates and infers reasons."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-v4-full-")
        self.data_dir = Path(self.tmp.name)
        self.db_path = self.data_dir / "personal_memory.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _populate(self) -> dict[str, str]:
        store = PersonalMemoryStore(data_dir=self.data_dir)
        source = _source(store, TENANT_A, "v4-src")
        chain = store.create_candidate(
            TENANT_A,
            claim_kind="confirmed_fact",
            content="я предпочитаю чай".encode("utf-8"),
            confidence=0.9,
            explanation="seed",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            expires_at=EXPIRES,
        )
        chain_id = str(chain["claim_id"])
        store.confirm_claim(
            TENANT_A, chain_id, reliable_source_id=str(source["source_id"])
        )
        store.correct_claim(
            TENANT_A,
            chain_id,
            content="я предпочитаю кофе".encode("utf-8"),
            explanation="owner fix",
            source_ids=[str(source["source_id"])],
        )
        other = store.create_candidate(
            TENANT_A,
            claim_kind="confirmed_fact",
            content="у меня есть кот".encode("utf-8"),
            confidence=0.9,
            explanation="seed",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            expires_at=EXPIRES,
        )
        return {"chain_id": chain_id, "other_id": str(other["claim_id"])}

    def test_downgraded_store_migrates_and_infers_reasons(self) -> None:
        ids = self._populate()
        _downgrade_to_v4(self.db_path)
        self.assertEqual(_schema_version(self.db_path), 4)
        store = PersonalMemoryStore(data_dir=self.data_dir)
        self.assertEqual(_schema_version(self.db_path), 5)
        first = _catalog(self.db_path)
        PersonalMemoryStore(data_dir=self.data_dir)
        self.assertEqual(_catalog(self.db_path), first)
        payload = json.loads(
            export_owner_data(TENANT_A, store=store).decode("utf-8")
        )
        claims = {str(item["claim_id"]): item for item in payload["claims"]}
        chain = claims[ids["chain_id"]]
        reasons = [
            (int(item["version"]), item["change_reason"])
            for item in chain["lifecycle"]
        ]
        self.assertEqual(
            reasons, [(1, "created"), (2, "promoted"), (3, "changed")]
        )
        for item in chain["lifecycle"]:
            self.assertIs(item["change_reason_inferred"], True)
        other = claims[ids["other_id"]]
        self.assertEqual(
            [(int(i["version"]), i["change_reason"]) for i in other["lifecycle"]],
            [(1, "created")],
        )
        self.assertIs(other["lifecycle"][0]["change_reason_inferred"], True)
        self.assertIn("links", payload)
        self.assertEqual(payload["links"], [])


if __name__ == "__main__":
    unittest.main()

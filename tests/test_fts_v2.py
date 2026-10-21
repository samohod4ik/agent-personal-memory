"""Issue #4: FTS v2 stem column, write-path sync and v1 fallback (T2-FTS)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.fts_index import (
    build_claim_fts_index,
    fts_index_present,
    rank_claims_fts,
)
from agent_personal_memory.learn import remember_personal_insight
from agent_personal_memory.ranking import rank_claims
from agent_personal_memory.store import PersonalMemoryStore, utcnow_iso
from agent_personal_memory.text_norm import NORMALIZER_VERSION
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import content_hash_for

TENANT_A = Actor("test", "user-a")
F1 = "у меня ноутбук с видеокартой RTX 3060"
F2 = "я предпочитаю утренние созвоны"
F3 = "у меня есть кот"
F4 = "я не ем мясо"
FACTS = (F1, F2, F3, F4)
F5 = "я играю на гитаре"
F5_FIXED = "я играю на скрипке"
EXPIRES = "2099-01-01T00:00:00Z"

# v1 DDL copied verbatim from the baseline _ensure_fts_table (27001ae).
_FTS_V1_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_claim_fts_index USING fts5(
    claim_id UNINDEXED,
    tenant_id UNINDEXED,
    body,
    tokenize='unicode61 remove_diacritics 0'
)
"""


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return ""


class FtsV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-fts-v2-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))
        self.fact_ids: dict[str, str] = {}
        for text in FACTS:
            created = remember_personal_insight(TENANT_A, text, store=self.store)
            self.fact_ids[text] = str(created["claim_id"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _build(self) -> dict[str, object]:
        return build_claim_fts_index(TENANT_A, store=self.store)

    def _ranked(self, query: str) -> list[dict[str, object]]:
        return rank_claims_fts(TENANT_A, query, store=self.store)

    def _fts_hit(self, query: str, claim_id: str) -> dict[str, object] | None:
        for row in self._ranked(query):
            if str(row["claim_id"]) == claim_id:
                return row
        return None

    def _fts_rows(self, claim_id: str) -> list[object]:
        conn = self.store._connect()
        try:
            return conn.execute(
                "SELECT claim_id FROM memory_claim_fts_index WHERE claim_id = ?",
                (claim_id,),
            ).fetchall()
        finally:
            conn.close()

    def _register_source(self, label: str, text: str) -> str:
        payload = text.encode("utf-8")
        source = self.store.register_source(
            TENANT_A,
            source_kind="user_message",
            external_ref=label,
            content_hash=content_hash_for(payload),
            snapshot=payload,
            observed_at=utcnow_iso(),
            retention_class="durable",
            metadata={"title": label},
        )
        return str(source["source_id"])

    def test_stemmed_match_finds_case_form(self) -> None:
        self._build()
        hit = self._fts_hit("видеокарта", self.fact_ids[F1])
        self.assertIsNotNone(hit)
        self.assertEqual(_as_text(hit["content"]), F1)
        self.assertIn("fts", hit["match_kinds"])

    def test_write_path_syncs_create_correct_and_erase(self) -> None:
        self._build()
        created = remember_personal_insight(TENANT_A, F5, store=self.store)
        f5_id = str(created["claim_id"])
        hit = self._fts_hit("гитарой", f5_id)
        self.assertIsNotNone(hit)
        self.assertIn("fts", hit["match_kinds"])
        source_id = self._register_source("corr-1", F5_FIXED)
        self.store.correct_claim(
            TENANT_A,
            f5_id,
            content=F5_FIXED.encode("utf-8"),
            explanation="owner correction",
            source_ids=[source_id],
        )
        self.assertIsNone(self._fts_hit("гитаре", f5_id))
        hit = self._fts_hit("скрипкой", f5_id)
        self.assertIsNotNone(hit)
        self.assertIn("fts", hit["match_kinds"])
        self.store.erase_claim(TENANT_A, f5_id)
        self.assertIsNone(self._fts_hit("скрипкой", f5_id))
        self.assertEqual(self._fts_rows(f5_id), [])

    def test_restricted_claim_never_gets_an_fts_row(self) -> None:
        self._build()
        source_id = self._register_source("restricted-src", "raw evidence")
        created = self.store.create_candidate(
            TENANT_A,
            claim_kind="confirmed_fact",
            content="детали здоровья без номеров".encode("utf-8"),
            confidence=0.9,
            explanation="restricted note",
            sensitivity="restricted",
            source_ids=[source_id],
            expires_at=EXPIRES,
        )
        self.assertEqual(self._fts_rows(str(created["claim_id"])), [])
        f3_id = self.fact_ids[F3]
        self.assertNotEqual(self._fts_rows(f3_id), [])
        source_id = self._register_source("corr-restricted", "downgrade")
        self.store.correct_claim(
            TENANT_A,
            f3_id,
            content=F3.encode("utf-8"),
            explanation="now restricted",
            source_ids=[source_id],
            sensitivity="restricted",
        )
        self.assertEqual(self._fts_rows(f3_id), [])

    def test_v1_table_is_treated_as_absent_and_rebuilt(self) -> None:
        conn = self.store._connect()
        try:
            conn.execute(_FTS_V1_DDL)
            conn.execute(
                "INSERT INTO memory_claim_fts_index(claim_id, tenant_id, body) "
                "VALUES ('stale-id', 'stale-tenant', 'кот')"
            )
        finally:
            conn.close()
        self.assertFalse(fts_index_present(self.store))
        native = rank_claims(TENANT_A, "кот", store=self.store)
        self.assertEqual(self._ranked("кот"), native)
        created = remember_personal_insight(TENANT_A, F5, store=self.store)
        f5_id = str(created["claim_id"])
        source_id = self._register_source("corr-old", F5_FIXED)
        self.store.correct_claim(
            TENANT_A,
            f5_id,
            content=F5_FIXED.encode("utf-8"),
            explanation="owner correction",
            source_ids=[source_id],
        )
        self.assertFalse(fts_index_present(self.store))
        self._build()
        self.assertTrue(fts_index_present(self.store))
        conn = self.store._connect()
        try:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(memory_claim_fts_index)")
            }
            self.assertIn("body_stem", columns)
            version = conn.execute(
                "SELECT normalizer_version FROM memory_fts_meta WHERE id = 1"
            ).fetchone()
            self.assertIsNotNone(version)
            self.assertEqual(int(version[0]), NORMALIZER_VERSION)
            stale = conn.execute(
                "SELECT claim_id FROM memory_claim_fts_index "
                "WHERE claim_id = 'stale-id'"
            ).fetchall()
            self.assertEqual(stale, [])
        finally:
            conn.close()
        hit = self._fts_hit("скрипкой", f5_id)
        self.assertIsNotNone(hit)
        self.assertIn("fts", hit["match_kinds"])

    def test_meta_version_mismatch_is_treated_as_absent(self) -> None:
        self._build()
        self.assertTrue(fts_index_present(self.store))
        conn = self.store._connect()
        try:
            conn.execute(
                "UPDATE memory_fts_meta SET normalizer_version = ? WHERE id = 1",
                (NORMALIZER_VERSION + 1,),
            )
        finally:
            conn.close()
        self.assertFalse(fts_index_present(self.store))
        native = rank_claims(TENANT_A, "кот", store=self.store)
        self.assertEqual(self._ranked("кот"), native)


if __name__ == "__main__":
    unittest.main()

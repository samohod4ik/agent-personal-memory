"""Native ranking and optional FTS over authorized claims."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.fts_index import (
    build_claim_fts_index,
    fts_index_present,
    rank_claims_fts,
)
from agent_personal_memory.ranking import rank_claims
from agent_personal_memory.store import PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import ValidationError, content_hash_for

ALICE = Actor(platform="t", user_id="11", display_name="")
BOB = Actor(platform="t", user_id="12", display_name="")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"


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


class RankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-rank-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _register(self, actor: Actor, **overrides: object) -> dict[str, object]:
        return self.store.register_source(actor, **_source_kwargs(**overrides))

    def _create(self, actor: Actor, content: bytes, **overrides: object) -> dict[str, object]:
        if "source_ids" not in overrides:
            source = self._register(
                actor,
                snapshot=content,
                external_ref=f"ref-{actor.user_id}-{content[:8]!r}",
            )
            overrides["source_ids"] = [str(source["source_id"])]
        return self.store.create_candidate(
            actor,
            **_claim_kwargs(overrides.pop("source_ids"), content=content, **overrides),
        )

    def test_matching_claim_ranks_above_non_match(self) -> None:
        source = self._register(
            ALICE,
            snapshot=b"src",
            external_ref="ref-a",
        )
        low = self._create(
            ALICE,
            b"unrelated gardening tips",
            source_ids=[str(source["source_id"])],
        )
        high = self._create(
            ALICE,
            b"I prefer window seats on flights",
            source_ids=[str(source["source_id"])],
        )
        ranked = rank_claims(ALICE, "window seat flights", store=self.store)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(str(ranked[0]["claim_id"]), str(high["claim_id"]))
        self.assertNotEqual(str(ranked[0]["claim_id"]), str(low["claim_id"]))
        self.assertGreater(float(ranked[0]["rank_score"]), 0.0)
        self.assertIn("lexical", ranked[0]["match_kinds"])

    def test_entity_boost_is_deterministic(self) -> None:
        source = self._register(ALICE, snapshot=b"src", external_ref="ref-a")
        claim = self._create(
            ALICE,
            b"Meeting in Lisbon office",
            source_ids=[str(source["source_id"])],
        )
        first = rank_claims(ALICE, '"Lisbon" office', store=self.store)
        second = rank_claims(ALICE, '"Lisbon" office', store=self.store)
        self.assertEqual(first, second)
        self.assertEqual(str(first[0]["claim_id"]), str(claim["claim_id"]))
        self.assertIn("entity", first[0]["match_kinds"])

    def test_cross_tenant_and_erased_claims_are_absent(self) -> None:
        alice_source = self._register(
            ALICE,
            snapshot=b"a",
            external_ref="ref-a",
        )
        bob_source = self._register(
            BOB,
            snapshot=b"b",
            external_ref="ref-b",
        )
        alice_claim = self._create(
            ALICE,
            b"alice secret keyword alpha",
            source_ids=[str(alice_source["source_id"])],
        )
        self._create(
            BOB,
            b"bob secret keyword beta",
            source_ids=[str(bob_source["source_id"])],
        )
        erased = self._create(
            ALICE,
            b"alice secret keyword gamma",
            source_ids=[str(alice_source["source_id"])],
        )
        self.store.erase_claim(ALICE, str(erased["claim_id"]))
        ranked = rank_claims(ALICE, "keyword", store=self.store)
        claim_ids = {str(row["claim_id"]) for row in ranked}
        self.assertIn(str(alice_claim["claim_id"]), claim_ids)
        self.assertNotIn(str(erased["claim_id"]), claim_ids)
        packed = json.dumps(ranked, default=str)
        self.assertNotIn(str(bob_source["source_id"]), packed)

    def test_fts_build_and_rank_with_native_fallback(self) -> None:
        store = PersonalMemoryStore(data_dir=Path(self.tmp.name))
        self.assertFalse(fts_index_present(store))
        native = rank_claims_fts(ALICE, "anything", store=store)
        self.assertEqual(native, [])
        source = self._register(
            ALICE,
            snapshot=b"src",
            external_ref="ref-a",
        )
        claim = self._create(
            ALICE,
            b"dark roast coffee preference",
            source_ids=[str(source["source_id"])],
        )
        self.assertFalse(fts_index_present(store))
        fallback = rank_claims_fts(ALICE, "coffee roast", store=store)
        self.assertEqual(str(fallback[0]["claim_id"]), str(claim["claim_id"]))
        built = build_claim_fts_index(ALICE, store=store)
        self.assertEqual(built["indexed_claims"], 1)
        self.assertTrue(fts_index_present(store))
        fts_ranked = rank_claims_fts(ALICE, "coffee roast", store=store)
        self.assertEqual(str(fts_ranked[0]["claim_id"]), str(claim["claim_id"]))
        self.assertIn("fts", fts_ranked[0]["match_kinds"])

    def test_erase_claim_removes_fts_plaintext_before_rebuild(self) -> None:
        store = PersonalMemoryStore(data_dir=Path(self.tmp.name))
        source = self._register(
            ALICE,
            snapshot=b"src",
            external_ref="ref-fts",
        )
        claim = self._create(
            ALICE,
            b"unique fts purge marker coffee",
            source_ids=[str(source["source_id"])],
        )
        build_claim_fts_index(ALICE, store=store)
        conn = store._connect()
        try:
            rows = conn.execute(
                "SELECT body FROM memory_claim_fts_index WHERE claim_id = ?",
                (str(claim["claim_id"]),),
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertIn(b"coffee", rows[0][0].encode("utf-8") if isinstance(rows[0][0], str) else rows[0][0])
        finally:
            conn.close()
        store.erase_claim(ALICE, str(claim["claim_id"]))
        conn = store._connect()
        try:
            rows = conn.execute(
                "SELECT body FROM memory_claim_fts_index WHERE claim_id = ?",
                (str(claim["claim_id"]),),
            ).fetchall()
            self.assertEqual(rows, [])
        finally:
            conn.close()
        ranked = rank_claims_fts(ALICE, "coffee", store=store)
        self.assertEqual(ranked, [])

    def test_invalid_query_raises_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            rank_claims(ALICE, "   ", store=self.store)


if __name__ == "__main__":
    unittest.main()

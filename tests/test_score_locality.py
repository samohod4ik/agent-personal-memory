"""Cluster B score-locality barriers over candidate hits."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.score_locality import (
    bm25_scores,
    score_locality,
)
from agent_personal_memory.store import PersonalMemoryStore
from agent_personal_memory.types import Actor

ALICE = Actor(platform="t", user_id="11", display_name="Alice")
ALICE_TENANT = "tenant-alice"
BOB_TENANT = "tenant-bob"
CRITERIA = (
    Path(__file__).resolve().parents[1]
    / "bench"
    / "QUALITY_CRITERIA.md"
)


def _alice_ctx(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "query_text": "raretoken preference",
        "tenant_id": ALICE_TENANT,
    }
    values.update(overrides)
    return values


class ScoreLocalityTests(unittest.TestCase):
    @unittest.skipUnless(
        CRITERIA.is_file(),
        "bench/QUALITY_CRITERIA.md is ported by a later task; member re-activates then",
    )
    def test_quality_criteria_document_covers_cluster_b(self) -> None:
        text = CRITERIA.read_text(encoding="utf-8")
        for needle in (
            "Pointer is not copy",
            "Honest-zero is not green Recall@k",
            "Access-contract",
            "Rejected alternatives kept",
            "Abstention / authorized zero",
            "Candidate-ACL",
            "Score-locality",
            "Snippet-ACL",
            "Shared FTS",
            "evals/personal-memory-bench/README.md",
        ):
            self.assertIn(needle, text)

    def test_cross_tenant_candidate_is_rejected(self) -> None:
        hits = [
            {
                "claim_id": "alice-1",
                "tenant_id": ALICE_TENANT,
                "content": "alice raretoken preference",
                "snippet": "alice raretoken preference",
                "score": 0.1,
            },
            {
                "claim_id": "bob-1",
                "tenant_id": BOB_TENANT,
                "content": "bob raretoken preference",
                "snippet": "bob raretoken preference",
                "score": 9.9,
            },
        ]
        ranked = score_locality(hits, ALICE, _alice_ctx())
        ids = [str(row["claim_id"]) for row in ranked]
        self.assertEqual(ids, ["alice-1"])
        packed = repr(ranked)
        self.assertNotIn("bob-1", packed)
        self.assertNotIn("bob raretoken", packed)

    def test_score_does_not_use_shared_df_pollution(self) -> None:
        alice_doc = "alice raretoken preference"
        bob_docs = [f"bob raretoken filler {index}" for index in range(20)]
        shared_docs = [alice_doc, *bob_docs]
        shared_alice = bm25_scores("raretoken", shared_docs)[0]
        local_alice = bm25_scores("raretoken", [alice_doc])[0]
        self.assertGreater(local_alice, shared_alice)
        self.assertGreater(local_alice, 0.0)

        hits = [
            {
                "claim_id": "alice-1",
                "tenant_id": ALICE_TENANT,
                "content": alice_doc,
                "snippet": alice_doc,
                "score": shared_alice,
            },
            {
                "claim_id": "bob-polluter",
                "tenant_id": BOB_TENANT,
                "content": bob_docs[0],
                "snippet": bob_docs[0],
                "score": 99.0,
            },
        ]
        ranked = score_locality(
            hits,
            ALICE,
            _alice_ctx(
                query_text="raretoken",
                tenant_corpus=[alice_doc],
            ),
        )
        self.assertEqual(len(ranked), 1)
        self.assertEqual(str(ranked[0]["claim_id"]), "alice-1")
        self.assertAlmostEqual(float(ranked[0]["rank_score"]), local_alice, places=6)
        self.assertNotAlmostEqual(float(ranked[0]["rank_score"]), shared_alice, places=6)
        self.assertTrue(math.isfinite(float(ranked[0]["rank_score"])))

    def test_snippet_acl_strips_unauthorized_text(self) -> None:
        authorized = "Alice prefers window seats"
        leaked = "Alice prefers window seats. Bob SSN 999-00-0000 secret."
        hits = [
            {
                "claim_id": "alice-1",
                "tenant_id": ALICE_TENANT,
                "content": authorized,
                "snippet": leaked,
                "score": 1.0,
            }
        ]
        ranked = score_locality(
            hits,
            ALICE,
            _alice_ctx(query_text="window seats"),
        )
        self.assertEqual(len(ranked), 1)
        snippet = str(ranked[0]["snippet"])
        self.assertIn("window", snippet.casefold())
        self.assertNotIn("Bob", snippet)
        self.assertNotIn("SSN", snippet)
        self.assertNotIn("999-00-0000", snippet)
        self.assertNotIn("secret", snippet)

    def test_authorized_zero_is_empty_not_vacuous_hit(self) -> None:
        hits = [
            {
                "claim_id": "bob-1",
                "tenant_id": BOB_TENANT,
                "content": "bob only memory",
                "snippet": "bob only memory",
                "score": 5.0,
            }
        ]
        ranked = score_locality(hits, ALICE, _alice_ctx(query_text="memory"))
        self.assertEqual(ranked, [])

    def test_missing_tenant_and_query_fail_closed(self) -> None:
        hits = [
            {
                "claim_id": "alice-1",
                "tenant_id": ALICE_TENANT,
                "content": "alice",
                "snippet": "alice",
            }
        ]
        with self.assertRaises(ValueError):
            score_locality(hits, ALICE, {"query_text": "alice"})
        with self.assertRaises(ValueError):
            score_locality(hits, ALICE, {"tenant_id": ALICE_TENANT, "query_text": "   "})

    def test_query_context_actor_mismatch_fails_closed(self) -> None:
        bob = Actor(platform="t", user_id="12", display_name="Bob")
        with self.assertRaises(ValueError):
            score_locality(
                [],
                ALICE,
                _alice_ctx(actor=bob, query_text="raretoken"),
            )

    def test_store_principal_wins_over_caller_tenant_id(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-sl-")
        self.addCleanup(tmp.cleanup)
        store = PersonalMemoryStore(data_dir=Path(tmp.name))
        alice_tenant = store.ensure_principal(ALICE)
        bob_tenant = store.ensure_principal(
            Actor(platform="t", user_id="12", display_name="Bob")
        )
        hits = [
            {
                "claim_id": "alice-1",
                "tenant_id": alice_tenant,
                "content": "alice raretoken preference",
                "snippet": "alice raretoken preference",
                "score": 0.1,
            },
            {
                "claim_id": "bob-1",
                "tenant_id": bob_tenant,
                "content": "bob raretoken preference",
                "snippet": "bob raretoken preference",
                "score": 9.9,
            },
        ]
        ranked = score_locality(
            hits,
            ALICE,
            {
                "query_text": "raretoken",
                "tenant_id": bob_tenant,
            },
            store=store,
        )
        self.assertEqual([str(row["claim_id"]) for row in ranked], ["alice-1"])
        packed = repr(ranked)
        self.assertNotIn("bob-1", packed)
        self.assertNotIn(bob_tenant, packed)


if __name__ == "__main__":
    unittest.main()

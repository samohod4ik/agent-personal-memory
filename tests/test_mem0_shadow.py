"""Mem0 shadow MemoryEngine adapter and native fallback."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_personal_memory.engines import (
    Mem0Adapter,
    NativeMemoryEngine,
    persist_shadow_candidates,
    purge_shadow_projection,
)
from agent_personal_memory.engines.types import (
    ExtractionRequest,
    RetrievalRequest,
)
from agent_personal_memory.store import PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import ValidationError, content_hash_for

ALICE = Actor(platform="t", user_id="21", display_name="")
BOB = Actor(platform="t", user_id="22", display_name="")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"


class FakeMem0Client:
    def __init__(
        self,
        *,
        extract_items: list[dict[str, Any]] | None = None,
        search_items: list[dict[str, Any]] | None = None,
        fail_extract: bool = False,
        fail_search: bool = False,
    ) -> None:
        self.extract_items = list(extract_items or [])
        self.search_items = list(search_items or [])
        self.fail_extract = fail_extract
        self.fail_search = fail_search
        self.deleted_users: list[str] = []

    def extract(
        self,
        *,
        user_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if self.fail_extract:
            raise RuntimeError("mem0 extract failed")
        return list(self.extract_items)

    def search(
        self,
        *,
        user_id: str,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if self.fail_search:
            raise RuntimeError("mem0 search failed")
        return list(self.search_items)[:limit]

    def delete_user(self, *, user_id: str) -> None:
        self.deleted_users.append(user_id)


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


class Mem0ShadowAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-mem0-")
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
                external_ref=f"ref-{actor.user_id}",
            )
            overrides["source_ids"] = [str(source["source_id"])]
        return self.store.create_candidate(
            actor,
            **_claim_kwargs(overrides.pop("source_ids"), content=content, **overrides),
        )

    def test_native_retrieve_returns_authorized_ranked_claims(self) -> None:
        source = self._register(ALICE, snapshot=b"src", external_ref="ref-a")
        claim = self._create(
            ALICE,
            b"prefers aisle seats on long flights",
            source_ids=[str(source["source_id"])],
        )
        engine = NativeMemoryEngine(store=self.store)
        result, records = engine.retrieve(
            RetrievalRequest(ALICE, "aisle seats", limit=5)
        )
        self.assertFalse(result.degraded)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].record_id, str(claim["claim_id"]))
        self.assertEqual(records[0].engine, "native")

    def test_mem0_extract_and_persist_create_candidates(self) -> None:
        source = self._register(ALICE, snapshot=b"src", external_ref="ref-a")
        client = FakeMem0Client(
            extract_items=[{"memory": "likes morning coffee before meetings"}]
        )
        adapter = Mem0Adapter(store=self.store, mem0_client=client)
        request = ExtractionRequest(
            ALICE,
            text="I always drink coffee before standup",
            source_ids=(str(source["source_id"]),),
        )
        result, candidates = adapter.extract_candidates(request)
        self.assertFalse(result.degraded)
        self.assertEqual(len(candidates), 1)
        created = persist_shadow_candidates(ALICE, candidates, store=self.store)
        self.assertEqual(len(created), 1)
        self.assertEqual(str(created[0]["state"]), "candidate")

    def test_mem0_unavailable_degrades_and_native_retrieve_fallback(self) -> None:
        source = self._register(ALICE, snapshot=b"src", external_ref="ref-a")
        self._create(
            ALICE,
            b"allergy to shellfish",
            source_ids=[str(source["source_id"])],
        )
        adapter = Mem0Adapter(store=self.store, mem0_client=None)
        result, records = adapter.retrieve(
            RetrievalRequest(ALICE, "shellfish allergy", limit=5)
        )
        self.assertTrue(result.degraded)
        self.assertEqual(result.degraded_reason, "mem0_unavailable")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].engine, "native")

    def test_mem0_search_failure_falls_back_to_native(self) -> None:
        source = self._register(ALICE, snapshot=b"src", external_ref="ref-a")
        self._create(
            ALICE,
            b"runs five kilometers on weekends",
            source_ids=[str(source["source_id"])],
        )
        client = FakeMem0Client(fail_search=True)
        adapter = Mem0Adapter(store=self.store, mem0_client=client)
        result, records = adapter.retrieve(
            RetrievalRequest(ALICE, "weekend running", limit=5)
        )
        self.assertTrue(result.degraded)
        self.assertEqual(result.degraded_reason, "mem0_search_failed")
        self.assertEqual(len(records), 1)

    def test_mem0_retrieve_reconciles_to_authorized_claim(self) -> None:
        source = self._register(ALICE, snapshot=b"src", external_ref="ref-a")
        claim = self._create(
            ALICE,
            b"peanut allergy requires epinephrine nearby",
            source_ids=[str(source["source_id"])],
        )
        client = FakeMem0Client(
            search_items=[{"text": "peanut allergy epinephrine"}]
        )
        adapter = Mem0Adapter(store=self.store, mem0_client=client)
        result, records = adapter.retrieve(
            RetrievalRequest(ALICE, "allergy", limit=5)
        )
        self.assertFalse(result.degraded)
        self.assertEqual(records[0].record_id, str(claim["claim_id"]))
        self.assertEqual(records[0].engine, "mem0")

    def test_cross_tenant_retrieve_is_empty(self) -> None:
        source = self._register(ALICE, snapshot=b"src", external_ref="ref-a")
        self._create(
            ALICE,
            b"alice private keyword",
            source_ids=[str(source["source_id"])],
        )
        engine = NativeMemoryEngine(store=self.store)
        result, records = engine.retrieve(
            RetrievalRequest(BOB, "private keyword", limit=5)
        )
        self.assertEqual(records, [])
        packed = json.dumps([record.__dict__ for record in records], default=str)
        self.assertNotIn("alice", packed)

    def test_erased_claim_is_not_retrieved(self) -> None:
        source = self._register(ALICE, snapshot=b"src", external_ref="ref-a")
        live = self._create(
            ALICE,
            b"keep this keyword visible",
            source_ids=[str(source["source_id"])],
        )
        gone = self._create(
            ALICE,
            b"erase this keyword hidden",
            source_ids=[str(source["source_id"])],
        )
        self.store.erase_claim(ALICE, str(gone["claim_id"]))
        engine = NativeMemoryEngine(store=self.store)
        result, records = engine.retrieve(
            RetrievalRequest(ALICE, "keyword", limit=10)
        )
        ids = {record.record_id for record in records}
        self.assertIn(str(live["claim_id"]), ids)
        self.assertNotIn(str(gone["claim_id"]), ids)

    def test_purge_shadow_projection_calls_client(self) -> None:
        client = FakeMem0Client()
        self.assertTrue(purge_shadow_projection(ALICE, client=client))
        self.assertEqual(client.deleted_users, ["t:21"])
        self.assertFalse(purge_shadow_projection(ALICE, client=None))


if __name__ == "__main__":
    unittest.main()

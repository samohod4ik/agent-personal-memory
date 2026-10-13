"""Authorized query engine: ACL, filters, and auth-before-decrypt."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_personal_memory.lifecycle import MemoryState
from agent_personal_memory.query import (
    get_authorized_claim,
    get_authorized_source,
    query_claims,
    query_sources,
)
from agent_personal_memory.store import (
    AUDIT_ACTIONS,
    NotFoundError,
    PersonalMemoryStore,
)
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import ValidationError, content_hash_for

ALICE = Actor(platform="t", user_id="1", display_name="")
BOB = Actor(platform="t", user_id="2", display_name="7")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"
ALICE_SOURCE = b"pmA"
ALICE_CLAIM = b"a1"
BOB_SOURCE = b"pmB"
BOB_CLAIM = b"b2"
CIPHERTEXT_MARKERS = (
    "wrapped_dek",
    "snapshot_enc",
    "locator_enc",
    "metadata_enc",
    "content_enc",
    "explanation_enc",
    "keyed_lookup_hash",
)
CLAIM_META_KEYS = frozenset(
    {
        "claim_id",
        "claim_version_id",
        "version",
        "claim_kind",
        "state",
        "sensitivity",
        "confidence",
        "valid_from",
        "valid_to",
        "expires_at",
        "created_at",
    }
)
SOURCE_META_KEYS = frozenset(
    {
        "source_id",
        "source_version_id",
        "version",
        "source_kind",
        "content_hash",
        "observed_at",
        "retention_class",
        "created_at",
        "deletion_state",
    }
)


def _source_kwargs(*, snapshot: bytes, external_ref: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "source_kind": "document",
        "external_ref": external_ref,
        "content_hash": content_hash_for(snapshot),
        "snapshot": snapshot,
        "observed_at": NOW,
        "retention_class": "standard",
        "metadata": {"title": "A"},
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


class PersonalMemoryQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-query-")
        self.data_dir = Path(self.tmp.name)
        self.store = PersonalMemoryStore(data_dir=self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _register(self, actor: Actor, **overrides: object) -> dict[str, object]:
        return self.store.register_source(actor, **overrides)

    def _create(self, actor: Actor, **overrides: object) -> dict[str, object]:
        if "source_ids" not in overrides:
            snapshot = ALICE_SOURCE if actor is ALICE else BOB_SOURCE
            source = self._register(
                actor,
                **_source_kwargs(
                    snapshot=snapshot,
                    external_ref="ref-" + actor.user_id,
                ),
            )
            overrides["source_ids"] = [str(source["source_id"])]
        content = overrides.pop("content", ALICE_CLAIM if actor is ALICE else BOB_CLAIM)
        return self.store.create_candidate(
            actor, **_claim_kwargs(overrides.pop("source_ids"), content=content, **overrides)
        )

    def _seed_tenants(self) -> dict[str, object]:
        alice_source = self._register(
            ALICE, **_source_kwargs(snapshot=ALICE_SOURCE, external_ref="ref-1")
        )
        alice_live = self._create(
            ALICE,
            source_ids=[str(alice_source["source_id"])],
            content=ALICE_CLAIM,
        )
        confirmation = self._register(
            ALICE,
            **_source_kwargs(
                snapshot=b"yes",
                external_ref="ref-c",
                source_kind="user_confirmation",
            ),
        )
        self.store.confirm_claim(
            ALICE,
            str(alice_live["claim_id"]),
            confirmation_source_id=str(confirmation["source_id"]),
        )
        alice_gone = self._create(
            ALICE,
            source_ids=[str(alice_source["source_id"])],
            content=b"z0",
        )
        self.store.erase_claim(ALICE, str(alice_gone["claim_id"]))
        alice_erased_source = self._register(
            ALICE, **_source_kwargs(snapshot=b"gone", external_ref="ref-gone")
        )
        self.store.erase_source(ALICE, str(alice_erased_source["source_id"]))
        bob_source = self._register(
            BOB, **_source_kwargs(snapshot=BOB_SOURCE, external_ref="ref-2")
        )
        bob_claim = self._create(
            BOB,
            source_ids=[str(bob_source["source_id"])],
            content=BOB_CLAIM,
        )
        return {
            "alice_source_id": str(alice_source["source_id"]),
            "alice_claim_id": str(alice_live["claim_id"]),
            "alice_erased_claim_id": str(alice_gone["claim_id"]),
            "alice_erased_source_id": str(alice_erased_source["source_id"]),
            "bob_source_id": str(bob_source["source_id"]),
            "bob_claim_id": str(bob_claim["claim_id"]),
        }

    def _audit_rows(self, action: str) -> list[tuple[object, ...]]:
        conn = self.store._connect()
        try:
            return list(
                conn.execute(
                    "SELECT action, target_kind, target_id, outcome, policy_reason "
                    "FROM memory_audit WHERE action = ? ORDER BY id",
                    (action,),
                )
            )
        finally:
            conn.close()

    def test_alice_cannot_query_or_get_bobs_rows(self) -> None:
        ids = self._seed_tenants()
        claims = query_claims(ALICE, store=self.store)
        sources = query_sources(ALICE, store=self.store)
        claim_ids = {str(row["claim_id"]) for row in claims}
        source_ids = {str(row["source_id"]) for row in sources}
        self.assertIn(ids["alice_claim_id"], claim_ids)
        self.assertIn(ids["alice_source_id"], source_ids)
        self.assertNotIn(ids["bob_claim_id"], claim_ids)
        self.assertNotIn(ids["bob_source_id"], source_ids)
        self.assertEqual(query_claims(BOB, store=self.store)[0]["claim_id"], ids["bob_claim_id"])
        packed = json.dumps(claims + sources, default=str)
        self.assertNotIn(ids["bob_claim_id"], packed)
        self.assertNotIn(ids["bob_source_id"], packed)
        with self.assertRaises(NotFoundError) as foreign_claim:
            get_authorized_claim(ALICE, ids["bob_claim_id"], store=self.store)
        with self.assertRaises(NotFoundError) as missing_claim:
            get_authorized_claim(ALICE, "00000000-0000-0000-0000-000000000000", store=self.store)
        with self.assertRaises(NotFoundError) as foreign_source:
            get_authorized_source(ALICE, ids["bob_source_id"], store=self.store)
        with self.assertRaises(NotFoundError) as missing_source:
            get_authorized_source(ALICE, "00000000-0000-0000-0000-000000000001", store=self.store)
        self.assertEqual(str(foreign_claim.exception), "Claim not found")
        self.assertEqual(str(missing_claim.exception), "Claim not found")
        self.assertEqual(repr(foreign_claim.exception), repr(missing_claim.exception))
        self.assertEqual(str(foreign_source.exception), "Source not found")
        self.assertEqual(str(missing_source.exception), "Source not found")
        self.assertEqual(repr(foreign_source.exception), repr(missing_source.exception))

    def test_erased_claim_and_source_are_invisible(self) -> None:
        ids = self._seed_tenants()
        claim_ids = {str(row["claim_id"]) for row in query_claims(ALICE, store=self.store)}
        source_ids = {str(row["source_id"]) for row in query_sources(ALICE, store=self.store)}
        self.assertNotIn(ids["alice_erased_claim_id"], claim_ids)
        self.assertNotIn(ids["alice_erased_source_id"], source_ids)
        with self.assertRaises(NotFoundError) as erased_claim:
            get_authorized_claim(ALICE, ids["alice_erased_claim_id"], store=self.store)
        with self.assertRaises(NotFoundError) as erased_source:
            get_authorized_source(ALICE, ids["alice_erased_source_id"], store=self.store)
        self.assertEqual(str(erased_claim.exception), "Claim not found")
        self.assertEqual(str(erased_source.exception), "Source not found")
        denied_claims = self._audit_rows("claim.query")
        denied_sources = self._audit_rows("source.query")
        self.assertIn("claim.query", AUDIT_ACTIONS)
        self.assertIn("source.query", AUDIT_ACTIONS)
        for row in denied_claims:
            if row[3] == "denied":
                self.assertEqual(row[2], "")
                self.assertEqual(row[4], "not_found")
                self.assertNotIn(ids["alice_erased_claim_id"], str(row))
                self.assertNotIn(ids["bob_claim_id"], str(row))
        for row in denied_sources:
            if row[3] == "denied":
                self.assertEqual(row[2], "")
                self.assertEqual(row[4], "not_found")
                self.assertNotIn(ids["alice_erased_source_id"], str(row))
                self.assertNotIn(ids["bob_source_id"], str(row))

    def test_state_filter_candidate_versus_confirmed(self) -> None:
        source = self._register(ALICE, **_source_kwargs(snapshot=ALICE_SOURCE, external_ref="ref-1"))
        candidate = self._create(
            ALICE,
            source_ids=[str(source["source_id"])],
            content=b"cand",
        )
        confirmed = self._create(
            ALICE,
            source_ids=[str(source["source_id"])],
            content=b"conf",
        )
        confirmation = self._register(
            ALICE,
            **_source_kwargs(
                snapshot=b"yes",
                external_ref="ref-c",
                source_kind="user_confirmation",
            ),
        )
        self.store.confirm_claim(
            ALICE,
            str(confirmed["claim_id"]),
            confirmation_source_id=str(confirmation["source_id"]),
        )
        default_ids = {str(row["claim_id"]) for row in query_claims(ALICE, store=self.store)}
        self.assertEqual(default_ids, {str(candidate["claim_id"]), str(confirmed["claim_id"])})
        candidates = query_claims(ALICE, states=["candidate"], store=self.store)
        confirmed_rows = query_claims(ALICE, states=["confirmed"], store=self.store)
        self.assertEqual([row["claim_id"] for row in candidates], [candidate["claim_id"]])
        self.assertEqual([row["state"] for row in candidates], [MemoryState.CANDIDATE.value])
        self.assertEqual([row["claim_id"] for row in confirmed_rows], [confirmed["claim_id"]])
        self.assertEqual([row["state"] for row in confirmed_rows], [MemoryState.CONFIRMED.value])
        mixed = query_claims(
            ALICE, states=[MemoryState.CANDIDATE, "confirmed"], store=self.store
        )
        self.assertEqual(
            {str(row["claim_id"]) for row in mixed},
            {str(candidate["claim_id"]), str(confirmed["claim_id"])},
        )

    def test_kind_and_domain_filters(self) -> None:
        source = self._register(ALICE, **_source_kwargs(snapshot=ALICE_SOURCE, external_ref="ref-1"))
        preference = self._create(
            ALICE,
            source_ids=[str(source["source_id"])],
            content=b"pref",
            claim_kind="preference",
        )
        task = self._create(
            ALICE,
            source_ids=[str(source["source_id"])],
            content=b"task",
            claim_kind="task",
        )
        calendar = self._register(
            ALICE,
            **_source_kwargs(
                snapshot=b"cal",
                external_ref="ref-cal",
                source_kind="calendar_event",
                observed_at="2026-08-30T10:00:00Z",
            ),
        )
        prefs = query_claims(ALICE, claim_kinds=["preference"], store=self.store)
        tasks = query_claims(ALICE, claim_kinds=["task"], store=self.store)
        self.assertEqual([row["claim_id"] for row in prefs], [preference["claim_id"]])
        self.assertEqual([row["claim_id"] for row in tasks], [task["claim_id"]])
        docs = query_sources(ALICE, source_kinds=["document"], store=self.store)
        cals = query_sources(ALICE, source_kinds=["calendar_event"], store=self.store)
        self.assertEqual([row["source_id"] for row in docs], [str(source["source_id"])])
        self.assertEqual([row["source_id"] for row in cals], [str(calendar["source_id"])])

    def test_valid_time_and_observed_time_windows(self) -> None:
        source = self._register(ALICE, **_source_kwargs(snapshot=ALICE_SOURCE, external_ref="ref-1"))
        winter = self._create(
            ALICE,
            source_ids=[str(source["source_id"])],
            content=b"winter",
            valid_from="2026-01-01T00:00:00Z",
            valid_to="2026-03-31T23:59:59Z",
        )
        summer = self._create(
            ALICE,
            source_ids=[str(source["source_id"])],
            content=b"summer",
            valid_from="2026-06-01T00:00:00Z",
            valid_to="2026-08-31T23:59:59Z",
        )
        july = query_claims(
            ALICE,
            valid_from="2026-07-01T00:00:00Z",
            valid_to="2026-07-31T23:59:59Z",
            store=self.store,
        )
        february = query_claims(
            ALICE,
            valid_from="2026-02-01T00:00:00Z",
            valid_to="2026-02-28T23:59:59Z",
            store=self.store,
        )
        self.assertEqual([row["claim_id"] for row in july], [summer["claim_id"]])
        self.assertEqual([row["claim_id"] for row in february], [winter["claim_id"]])
        early = self._register(
            ALICE,
            **_source_kwargs(
                snapshot=b"early",
                external_ref="ref-early",
                observed_at="2026-08-01T10:00:00Z",
            ),
        )
        late = self._register(
            ALICE,
            **_source_kwargs(
                snapshot=b"late",
                external_ref="ref-late",
                observed_at="2026-08-31T10:00:00Z",
            ),
        )
        after_mid = query_sources(
            ALICE, observed_from="2026-08-15T00:00:00Z", store=self.store
        )
        before_mid = query_sources(
            ALICE, observed_to="2026-08-15T00:00:00Z", store=self.store
        )
        after_ids = {str(row["source_id"]) for row in after_mid}
        before_ids = {str(row["source_id"]) for row in before_mid}
        self.assertIn(str(late["source_id"]), after_ids)
        self.assertNotIn(str(early["source_id"]), after_ids)
        self.assertIn(str(early["source_id"]), before_ids)
        self.assertNotIn(str(late["source_id"]), before_ids)

    def test_empty_results_and_partial_matches(self) -> None:
        source = self._register(ALICE, **_source_kwargs(snapshot=ALICE_SOURCE, external_ref="ref-1"))
        live = self._create(
            ALICE,
            source_ids=[str(source["source_id"])],
            content=ALICE_CLAIM,
            claim_kind="preference",
        )
        self.assertEqual(query_claims(ALICE, claim_kinds=["task"], store=self.store), [])
        self.assertEqual(query_sources(ALICE, source_kinds=["travel_record"], store=self.store), [])
        self.assertEqual(query_claims(BOB, store=self.store), [])
        self.assertEqual(query_sources(BOB, store=self.store), [])
        partial = query_claims(ALICE, claim_kinds=["preference", "task"], store=self.store)
        self.assertEqual([row["claim_id"] for row in partial], [live["claim_id"]])

    def test_include_content_decrypts_only_after_auth(self) -> None:
        ids = self._seed_tenants()
        listed = query_claims(ALICE, store=self.store)
        sourced = query_sources(ALICE, store=self.store)
        meta = listed[0]
        source_meta = sourced[0]
        self.assertTrue(CLAIM_META_KEYS.issubset(meta))
        self.assertTrue(SOURCE_META_KEYS.issubset(source_meta))
        self.assertNotIn("content", meta)
        self.assertNotIn("explanation", meta)
        self.assertNotIn("snapshot", source_meta)
        self.assertNotIn("external_ref", source_meta)
        self.assertNotIn("metadata", source_meta)
        for row in listed:
            self.assertNotIn("content", row)
            self.assertNotIn("explanation", row)
            self.assertNotEqual(row.get("claim_kind"), ALICE_CLAIM.decode("ascii"))
        for row in sourced:
            self.assertNotIn("snapshot", row)
            self.assertNotIn("external_ref", row)
            self.assertNotIn("metadata", row)
        packed = json.dumps(listed + sourced, default=str)
        for marker in CIPHERTEXT_MARKERS:
            self.assertNotIn(marker, packed)
        with patch("agent_personal_memory.store.decrypt_field") as decrypt:
            with self.assertRaises(NotFoundError):
                get_authorized_claim(
                    ALICE,
                    ids["bob_claim_id"],
                    include_content=True,
                    store=self.store,
                )
            with self.assertRaises(NotFoundError):
                get_authorized_source(
                    ALICE,
                    ids["bob_source_id"],
                    include_payload=True,
                    store=self.store,
                )
            with self.assertRaises(NotFoundError):
                get_authorized_claim(
                    ALICE,
                    ids["alice_erased_claim_id"],
                    include_content=True,
                    store=self.store,
                )
            decrypt.assert_not_called()
        owned = get_authorized_claim(
            ALICE, ids["alice_claim_id"], include_content=True, store=self.store
        )
        payload = get_authorized_source(
            ALICE, ids["alice_source_id"], include_payload=True, store=self.store
        )
        self.assertEqual(owned["content"], ALICE_CLAIM)
        self.assertEqual(owned["explanation"], "note")
        self.assertEqual(payload["snapshot"], ALICE_SOURCE)
        self.assertEqual(payload["external_ref"], "ref-1")
        self.assertEqual(payload["metadata"]["title"], "A")
        for marker in CIPHERTEXT_MARKERS:
            self.assertNotIn(marker, json.dumps(owned, default=str))
            self.assertNotIn(marker, json.dumps(payload, default=str))
        queried = query_claims(ALICE, include_content=True, store=self.store)
        live = next(row for row in queried if str(row["claim_id"]) == ids["alice_claim_id"])
        self.assertEqual(live["content"], ALICE_CLAIM)
        self.assertIsInstance(live["content"], (bytes, bytearray))

    def test_default_query_excludes_superseded_deleted_and_expired(self) -> None:
        source = self._register(ALICE, **_source_kwargs(snapshot=ALICE_SOURCE, external_ref="ref-1"))
        live = self._create(
            ALICE, source_ids=[str(source["source_id"])], content=b"live"
        )
        expired = self._create(
            ALICE, source_ids=[str(source["source_id"])], content=b"stale"
        )
        conn = self.store._connect()
        try:
            conn.execute(
                "UPDATE memory_claim_versions SET expires_at = ? WHERE claim_id = ?",
                ("2020-01-01T00:00:00Z", expired["claim_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        ids = {str(row["claim_id"]) for row in query_claims(ALICE, store=self.store)}
        self.assertIn(str(live["claim_id"]), ids)
        self.assertNotIn(str(expired["claim_id"]), ids)
        self.assertTrue(all(row["state"] != MemoryState.SUPERSEDED.value for row in query_claims(ALICE, store=self.store)))
        self.assertTrue(all(row["state"] != MemoryState.DELETED.value for row in query_claims(ALICE, store=self.store)))

    def test_default_query_excludes_restricted_and_hides_cross_tenant(self) -> None:
        source = self._register(ALICE, **_source_kwargs(snapshot=ALICE_SOURCE, external_ref="ref-1"))
        live = self._create(
            ALICE, source_ids=[str(source["source_id"])], content=b"live"
        )
        restricted = self._create(
            ALICE,
            source_ids=[str(source["source_id"])],
            sensitivity="restricted",
            content=b"Home street is short",
        )
        default_ids = {str(row["claim_id"]) for row in query_claims(ALICE, store=self.store)}
        self.assertIn(str(live["claim_id"]), default_ids)
        self.assertNotIn(str(restricted["claim_id"]), default_ids)
        self.assertTrue(
            all(row["sensitivity"] != "restricted" for row in query_claims(ALICE, store=self.store))
        )
        bob_ids = {str(row["claim_id"]) for row in query_claims(BOB, store=self.store)}
        self.assertNotIn(str(restricted["claim_id"]), bob_ids)
        self.assertEqual(query_claims(BOB, store=self.store), [])
        with self.assertRaises(NotFoundError) as foreign:
            get_authorized_claim(BOB, str(restricted["claim_id"]), store=self.store)
        self.assertEqual(str(foreign.exception), "Claim not found")

    def test_get_by_id_denies_restricted_and_expired_for_owner(self) -> None:
        source = self._register(ALICE, **_source_kwargs(snapshot=ALICE_SOURCE, external_ref="ref-1"))
        restricted = self._create(
            ALICE,
            source_ids=[str(source["source_id"])],
            sensitivity="restricted",
            content=b"secret address",
        )
        expired = self._create(
            ALICE,
            source_ids=[str(source["source_id"])],
            content=b"stale fact",
        )
        conn = self.store._connect()
        try:
            conn.execute(
                "UPDATE memory_claim_versions SET expires_at = ? WHERE claim_id = ?",
                ("2020-01-01T00:00:00Z", expired["claim_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        default_ids = {str(row["claim_id"]) for row in query_claims(ALICE, store=self.store)}
        self.assertNotIn(str(restricted["claim_id"]), default_ids)
        self.assertNotIn(str(expired["claim_id"]), default_ids)
        with patch("agent_personal_memory.store.decrypt_field") as decrypt:
            with self.assertRaises(NotFoundError):
                get_authorized_claim(
                    ALICE,
                    str(restricted["claim_id"]),
                    include_content=True,
                    store=self.store,
                )
            with self.assertRaises(NotFoundError):
                get_authorized_claim(
                    ALICE,
                    str(expired["claim_id"]),
                    include_content=True,
                    store=self.store,
                )
            decrypt.assert_not_called()

    def test_invalid_filters_raise_validation_error(self) -> None:
        self._register(ALICE, **_source_kwargs(snapshot=ALICE_SOURCE, external_ref="ref-1"))
        with self.assertRaises(ValidationError):
            query_claims(ALICE, states=["Candidate"], store=self.store)
        with self.assertRaises(ValidationError):
            query_claims(ALICE, claim_kinds=["Preference"], store=self.store)
        with self.assertRaises(ValidationError):
            query_sources(ALICE, source_kinds=["Document"], store=self.store)
        with self.assertRaises(ValidationError):
            query_claims(ALICE, valid_from="2026-12-31T00:00:00Z", valid_to="2026-01-01T00:00:00Z", store=self.store)
        with self.assertRaises(ValidationError):
            query_sources(ALICE, observed_from="2026-08-31T10:00:00+00:00", store=self.store)
        with self.assertRaises(ValidationError):
            query_claims(ALICE, limit=0, store=self.store)
        rejected = self._audit_rows("claim.query") + self._audit_rows("source.query")
        self.assertTrue(any(row[3] == "rejected" and row[4] == "validation_failed" for row in rejected))


if __name__ == "__main__":
    unittest.main()

"""Cross-tenant export and safe provenance explanation tests."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.export import explain_claim, export_owner_data
from agent_personal_memory.lifecycle import MemoryState
from agent_personal_memory.query import get_authorized_claim
from agent_personal_memory.store import (
    AUDIT_ACTIONS,
    NotFoundError,
    PersonalMemoryStore,
)
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import content_hash_for

ALICE = Actor(platform="t", user_id="1", display_name="")
BOB = Actor(platform="t", user_id="2", display_name="7")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"
ALICE_SOURCE = b"pmA"
ALICE_CLAIM = b"a1"
ALICE_TITLE = "A"
BOB_SOURCE = b"pmB"
BOB_CLAIM = b"b2"
BOB_TITLE = "B"
SECRET_MARKERS = (
    "wrapped_dek",
    "snapshot_enc",
    "locator_enc",
    "metadata_enc",
    "content_enc",
    "explanation_enc",
    "keyed_lookup_hash",
    "master.key",
)
FORBIDDEN_SELECTORS = ("tenant_id", "tenant", "user_id", "platform")


def _source_kwargs(*, snapshot: bytes, title: str, external_ref: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "source_kind": "document",
        "external_ref": external_ref,
        "content_hash": content_hash_for(snapshot),
        "snapshot": snapshot,
        "observed_at": NOW,
        "retention_class": "standard",
        "metadata": {
            "title": title,
            "numeric": 7,
            "boolean": True,
            "empty": "",
        },
    }
    values.update(overrides)
    return values


def _claim_kwargs(source_ids: list[str], *, content: bytes, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "claim_kind": "preference",
        "content": content,
        "confidence": 0.7,
        "explanation": "",
        "sensitivity": "normal",
        "source_ids": list(source_ids),
        "valid_from": "2026-08-01T00:00:00Z",
        "valid_to": "2026-12-31T23:59:59Z",
        "expires_at": EXPIRES,
    }
    values.update(overrides)
    return values


class PersonalMemoryOwnershipExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-export-")
        self.data_dir = Path(self.tmp.name)
        self.store = PersonalMemoryStore(data_dir=self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _register(self, actor: Actor, **overrides: object) -> dict[str, object]:
        return self.store.register_source(actor, **overrides)

    def _create(self, actor: Actor, **overrides: object) -> dict[str, object]:
        if "source_ids" not in overrides:
            title = ALICE_TITLE if actor is ALICE else BOB_TITLE
            snapshot = ALICE_SOURCE if actor is ALICE else BOB_SOURCE
            source = self._register(
                actor,
                **_source_kwargs(
                    snapshot=snapshot,
                    title=title,
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
            ALICE,
            **_source_kwargs(snapshot=ALICE_SOURCE, title=ALICE_TITLE, external_ref="ref-1"),
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
                title="",
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
            explanation="",
        )
        receipt = self.store.erase_claim(ALICE, str(alice_gone["claim_id"]))
        bob_source = self._register(
            BOB,
            **_source_kwargs(snapshot=BOB_SOURCE, title=BOB_TITLE, external_ref="ref-2"),
        )
        bob_claim = self._create(
            BOB,
            source_ids=[str(bob_source["source_id"])],
            content=BOB_CLAIM,
        )
        return {
            "alice_source_id": str(alice_source["source_id"]),
            "alice_claim_id": str(alice_live["claim_id"]),
            "alice_erased_id": str(alice_gone["claim_id"]),
            "alice_receipt_id": receipt.receipt_id,
            "bob_source_id": str(bob_source["source_id"]),
            "bob_claim_id": str(bob_claim["claim_id"]),
        }

    def _export(self, actor: Actor) -> dict[str, object]:
        raw = export_owner_data(actor, store=self.store)
        self.assertIsInstance(raw, (bytes, bytearray))
        payload = json.loads(bytes(raw).decode("utf-8"))
        self.assertIsInstance(payload, dict)
        return payload

    def _packed(self, value: object) -> str:
        return json.dumps(value, ensure_ascii=False)

    def test_export_contains_only_current_actor_data(self) -> None:
        ids = self._seed_tenants()
        payload = self._export(ALICE)
        packed = self._packed(payload)
        self.assertIn("sources", payload)
        self.assertIn("claims", payload)
        self.assertIn("deletion_receipts", payload)
        source_ids = {str(row["source_id"]) for row in payload["sources"]}
        claim_ids = {str(row["claim_id"]) for row in payload["claims"]}
        receipt_ids = {str(row["receipt_id"]) for row in payload["deletion_receipts"]}
        self.assertIn(ids["alice_source_id"], source_ids)
        self.assertIn(ids["alice_claim_id"], claim_ids)
        self.assertIn(ids["alice_receipt_id"], receipt_ids)
        self.assertNotIn(ids["bob_source_id"], source_ids)
        self.assertNotIn(ids["bob_claim_id"], claim_ids)
        self.assertNotIn(ids["bob_source_id"], packed)
        self.assertNotIn(ids["bob_claim_id"], packed)
        self.assertNotIn('"label": "B"', packed)
        self.assertNotIn("pmB", packed)
        self.assertNotIn('"content": "b2"', packed)
        self.assertNotIn("ref-2", packed)
        for selector in FORBIDDEN_SELECTORS:
            self.assertNotIn(selector, packed)

    def test_export_includes_lifecycle_safe_provenance_and_receipts(self) -> None:
        ids = self._seed_tenants()
        payload = self._export(ALICE)
        live = next(
            row
            for row in payload["claims"]
            if str(row["claim_id"]) == ids["alice_claim_id"]
        )
        self.assertIn("lifecycle", live)
        states = {str(item["state"]) for item in live["lifecycle"]}
        self.assertIn(MemoryState.SUPERSEDED, states)
        self.assertIn(MemoryState.CONFIRMED, states)
        self.assertEqual(len(live["lifecycle"]), 2)
        newest = max(live["lifecycle"], key=lambda item: int(item["version"]))
        self.assertEqual(newest["state"], MemoryState.CONFIRMED)
        self.assertEqual(newest["confidence"], 0.7)
        self.assertTrue(str(newest["created_at"]).endswith("Z"))
        provenance = live["provenance"]
        kinds = {str(item["source_kind"]) for item in provenance}
        labels = {str(item["label"]) for item in provenance}
        self.assertIn("document", kinds)
        self.assertIn("user_confirmation", kinds)
        self.assertIn(ALICE_TITLE, labels)
        self.assertIn("", labels)
        sources = {str(row["source_id"]): row for row in payload["sources"]}
        self.assertEqual(sources[ids["alice_source_id"]]["label"], ALICE_TITLE)
        self.assertEqual(sources[ids["alice_source_id"]]["source_kind"], "document")
        receipts = payload["deletion_receipts"]
        self.assertTrue(receipts)
        self.assertEqual(receipts[0]["outcome"], "erased")
        self.assertNotIn(ids["alice_erased_id"], self._packed(receipts))

    def test_export_decrypts_owner_fields_without_secrets(self) -> None:
        self._seed_tenants()
        payload = self._export(ALICE)
        packed = self._packed(payload)
        self.assertIn("a1", packed)
        self.assertIn("pmA", packed)
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, packed)
        self.assertNotIn("raw credential", packed.lower())

    def test_export_audit_has_no_plaintext(self) -> None:
        self._seed_tenants()
        export_owner_data(ALICE, store=self.store)
        conn = self.store._connect()
        try:
            rows = list(
                conn.execute(
                    "SELECT action, target_id, outcome, policy_reason FROM memory_audit "
                    "WHERE action = 'owner.export'"
                )
            )
        finally:
            conn.close()
        self.assertTrue(rows)
        self.assertIn("owner.export", AUDIT_ACTIONS)
        for row in rows:
            joined = "|".join("" if item is None else str(item) for item in row)
            self.assertNotIn("a1", joined)
            self.assertNotIn("pmA", joined)
            self.assertNotIn(ALICE_TITLE, joined)

    def test_explain_claim_returns_safe_provenance(self) -> None:
        ids = self._seed_tenants()
        explanation = explain_claim(ALICE, ids["alice_claim_id"], store=self.store)
        self.assertEqual(explanation["claim_id"], ids["alice_claim_id"])
        self.assertEqual(explanation["state"], MemoryState.CONFIRMED)
        self.assertEqual(explanation["confidence"], 0.7)
        self.assertTrue(str(explanation["created_at"]).endswith("Z"))
        kinds = {str(item["source_kind"]) for item in explanation["sources"]}
        labels = {str(item["label"]) for item in explanation["sources"]}
        self.assertIn("document", kinds)
        self.assertIn("user_confirmation", kinds)
        self.assertIn(ALICE_TITLE, labels)
        # T6-HISTORY: the "history" key carries owner-visible per-version
        # content by design; the envelope itself still exposes no
        # top-level content field, and the packed payload must not carry
        # snapshots, ciphertext field names or cross-tenant identifiers.
        self.assertNotIn("content", explanation)
        packed = self._packed(explanation)
        self.assertNotIn("snapshot", packed)
        self.assertNotIn("pmA", packed)
        self.assertNotIn(ids["bob_source_id"], packed)
        for selector in FORBIDDEN_SELECTORS:
            self.assertNotIn(selector, packed)
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, packed)

    def test_explain_claim_missing_foreign_and_erased_are_not_found(self) -> None:
        ids = self._seed_tenants()
        with self.assertRaises(NotFoundError) as missing:
            explain_claim(ALICE, "00000000-0000-0000-0000-000000000000", store=self.store)
        with self.assertRaises(NotFoundError) as foreign:
            explain_claim(ALICE, ids["bob_claim_id"], store=self.store)
        with self.assertRaises(NotFoundError) as erased:
            explain_claim(ALICE, ids["alice_erased_id"], store=self.store)
        self.assertEqual(str(missing.exception), "Claim not found")
        self.assertEqual(str(foreign.exception), "Claim not found")
        self.assertEqual(str(erased.exception), "Claim not found")
        self.assertEqual(repr(missing.exception), repr(foreign.exception))

    def test_explain_claim_restricted_and_expired_are_not_found(self) -> None:
        """explain_claim mirrors get_authorized_claim's restricted/expired
        invisibility with byte-identical denial text (T12 security delta)."""
        alice_source = self._register(
            ALICE,
            **_source_kwargs(
                snapshot=ALICE_SOURCE, title=ALICE_TITLE, external_ref="ref-1"
            ),
        )
        restricted = self._create(
            ALICE,
            source_ids=[str(alice_source["source_id"])],
            content=b"secret address",
            sensitivity="restricted",
        )
        expired = self._create(
            ALICE,
            source_ids=[str(alice_source["source_id"])],
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
        for claim_id in (str(restricted["claim_id"]), str(expired["claim_id"])):
            with self.subTest(claim_id=claim_id):
                with self.assertRaises(NotFoundError) as denied:
                    explain_claim(ALICE, claim_id, store=self.store)
                # Byte-level parity with the authorized-read denial.
                with self.assertRaises(NotFoundError) as query_denied:
                    get_authorized_claim(ALICE, claim_id, store=self.store)
                self.assertEqual(str(denied.exception), "Claim not found")
                self.assertEqual(
                    str(denied.exception), str(query_denied.exception)
                )
                self.assertEqual(
                    repr(denied.exception), repr(query_denied.exception)
                )

    def test_export_preserves_binary_snapshot_and_content(self) -> None:
        snapshot = b"\xff\x00binary-snap"
        content = b"\xfe\x01binary-claim"
        source = self._register(
            ALICE,
            **_source_kwargs(snapshot=snapshot, title="bin", external_ref="ref-bin"),
        )
        claim = self._create(
            ALICE,
            source_ids=[str(source["source_id"])],
            content=content,
        )
        payload = self._export(ALICE)
        exported_source = next(
            row
            for row in payload["sources"]
            if str(row["source_id"]) == str(source["source_id"])
        )
        exported_claim = next(
            row
            for row in payload["claims"]
            if str(row["claim_id"]) == str(claim["claim_id"])
        )
        newest = max(exported_claim["lifecycle"], key=lambda item: int(item["version"]))
        self._assert_exported_bytes(exported_source["snapshot"], snapshot)
        self._assert_exported_bytes(newest["content"], content)

    def _assert_exported_bytes(self, value: object, expected: bytes) -> None:
        self.assertIsInstance(value, dict)
        envelope = dict(value)
        self.assertEqual(envelope.get("encoding"), "base64")
        self.assertEqual(base64.b64decode(envelope["data"]), expected)


if __name__ == "__main__":
    unittest.main()

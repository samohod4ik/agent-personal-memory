"""Transition-matrix, provenance, ACL, expiry, and encryption tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent_personal_memory.lifecycle as lifecycle_module
import agent_personal_memory.store as store_module
from agent_personal_memory.crypto import (
    FIELD_CONTENT,
    FIELD_EXPLANATION,
    FIELD_LABELS,
    FIELD_METADATA,
    FIELD_SNAPSHOT,
    CryptoError,
    decrypt_field,
    field_aad,
    record_aad,
    unwrap_dek,
)
from agent_personal_memory.lifecycle import MemoryState
from agent_personal_memory.store import (
    AUDIT_ACTIONS,
    AUDIT_OUTCOMES,
    AUDIT_REASONS,
    AUDIT_TARGET_KINDS,
    NotFoundError,
    PersonalMemoryStore,
)
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import ValidationError, content_hash_for

ALICE = Actor(platform="t", user_id="1", display_name="")
BOB = Actor(platform="t", user_id="2", display_name="7")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"
CONTENT = b"Prefers window seats"
EXPLANATION = "Extracted from booking note"
CLAIM_NEEDLES = (
    CONTENT,
    EXPLANATION.encode("utf-8"),
    b"Prefers window",
    b"Extracted from booking",
)

CREATE_KEYS = frozenset(
    {
        "claim_id",
        "claim_version_id",
        "version",
        "claim_kind",
        "state",
        "sensitivity",
        "confidence",
        "expires_at",
        "created_at",
    }
)
LIST_KEYS = frozenset(
    {
        "claim_id",
        "version",
        "claim_version_id",
        "claim_kind",
        "state",
        "sensitivity",
        "confidence",
        "expires_at",
        "created_at",
    }
)


def _future_expiry(*, days: int = 30) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _source_kwargs(**overrides: object) -> dict[str, object]:
    snapshot = overrides.get("snapshot", CONTENT)
    if not isinstance(snapshot, bytes):
        snapshot = CONTENT
    values: dict[str, object] = {
        "source_kind": "document",
        "external_ref": "https://disk.yandex.ru/d/ClaimSrc01",
        "content_hash": content_hash_for(snapshot),
        "snapshot": snapshot,
        "observed_at": NOW,
        "retention_class": "standard",
        "metadata": {"title": "x", "numeric": 7, "boolean": True, "empty": ""},
    }
    values.update(overrides)
    if "snapshot" in overrides and "content_hash" not in overrides:
        snap = values["snapshot"]
        if isinstance(snap, bytes):
            values["content_hash"] = content_hash_for(snap)
    return values


def _claim_kwargs(source_ids: list[str], **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "claim_kind": "preference",
        "content": CONTENT,
        "confidence": 0.7,
        "explanation": EXPLANATION,
        "sensitivity": "normal",
        "source_ids": list(source_ids),
        "valid_from": "2026-08-01T00:00:00Z",
        "valid_to": "2026-12-31T23:59:59Z",
        "expires_at": EXPIRES,
    }
    values.update(overrides)
    return values


class PersonalMemoryLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-life-")
        self.data_dir = Path(self.tmp.name)
        self.store = PersonalMemoryStore(data_dir=self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _register(self, actor: Actor = ALICE, **overrides: object) -> dict[str, object]:
        return self.store.register_source(actor, **_source_kwargs(**overrides))

    def _create(self, actor: Actor = ALICE, **overrides: object) -> dict[str, object]:
        if "source_ids" not in overrides:
            source = self._register(actor)
            overrides["source_ids"] = [str(source["source_id"])]
        return self.store.create_candidate(actor, **_claim_kwargs(**overrides))

    def _rows(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        conn = self.store._connect()
        try:
            conn.row_factory = sqlite3.Row
            return list(conn.execute(sql, params))
        finally:
            conn.close()

    def _audit(self) -> list[tuple[object, ...]]:
        conn = self.store._connect()
        try:
            return list(
                conn.execute(
                    "SELECT tenant_id, actor_id, action, target_kind, target_id, "
                    "outcome, policy_reason FROM memory_audit ORDER BY id"
                )
            )
        finally:
            conn.close()

    def test_memory_state_is_closed_string_enum(self) -> None:
        self.assertEqual(MemoryState.CANDIDATE, "candidate")
        self.assertEqual(MemoryState.CONFIRMED, "confirmed")
        self.assertEqual(MemoryState.SUPERSEDED, "superseded")
        self.assertEqual(MemoryState.RETRACTED, "retracted")
        self.assertEqual(MemoryState.DELETED, "deleted")
        self.assertTrue(issubclass(MemoryState, str))
        self.assertEqual(
            {item.value for item in MemoryState},
            {"candidate", "confirmed", "superseded", "retracted", "deleted"},
        )

    def test_create_candidate_shape_and_state(self) -> None:
        created = self._create()
        self.assertTrue(CREATE_KEYS.issubset(created.keys()))
        self.assertEqual(created["state"], MemoryState.CANDIDATE)
        self.assertEqual(created["version"], 1)
        self.assertEqual(created["claim_kind"], "preference")
        self.assertEqual(created["sensitivity"], "normal")
        self.assertEqual(created["confidence"], 0.7)
        self.assertEqual(created["expires_at"], EXPIRES)
        self.assertIsInstance(created["created_at"], str)
        self.assertTrue(str(created["created_at"]).endswith("Z"))
        self.assertNotIn("content", created)
        self.assertNotIn("explanation", created)
        self.assertNotIn("content_enc", created)

        listed = self.store.list_active_claims(ALICE)
        self.assertEqual(len(listed), 1)
        self.assertEqual(set(listed[0].keys()), LIST_KEYS)
        self.assertEqual(listed[0]["claim_id"], created["claim_id"])
        self.assertEqual(listed[0]["state"], MemoryState.CANDIDATE)
        for forbidden in ("content", "explanation", "content_enc", "explanation_enc"):
            self.assertNotIn(forbidden, listed[0])

        support = self._rows(
            "SELECT source_id FROM memory_support WHERE claim_id = ?",
            (created["claim_id"],),
        )
        self.assertEqual(len(support), 1)

    def test_create_rejects_past_and_non_z_expires_at(self) -> None:
        source = self._register()
        source_ids = [str(source["source_id"])]
        with self.assertRaises(ValidationError):
            self._create(source_ids=source_ids, expires_at="2020-01-01T00:00:00Z")
        with self.assertRaises(ValidationError):
            self._create(
                source_ids=source_ids, expires_at="2099-01-01T00:00:00+00:00"
            )
        with self.assertRaises(ValidationError):
            self._create(source_ids=source_ids, expires_at="2099-01-01T00:00:00")
        with self.assertRaises(ValidationError):
            self._create(source_ids=source_ids, expires_at="")

    def test_confirm_requires_user_confirmation_or_reliable_source(self) -> None:
        created = self._create()
        with self.assertRaises(ValidationError):
            self.store.confirm_claim(ALICE, str(created["claim_id"]))
        chat = self._register(
            source_kind="user_message",
            external_ref="chat-1",
            snapshot=b"I like window seats",
        )
        with self.assertRaises(ValidationError):
            self.store.confirm_claim(
                ALICE,
                str(created["claim_id"]),
                confirmation_source_id=str(chat["source_id"]),
            )
        assistant = self._register(
            source_kind="assistant_message",
            external_ref="asst-1",
            snapshot=b"Noted preference",
        )
        with self.assertRaises(ValidationError):
            self.store.confirm_claim(
                ALICE,
                str(created["claim_id"]),
                reliable_source_id=str(assistant["source_id"]),
            )
        still = self.store.list_active_claims(ALICE)
        self.assertEqual(len(still), 1)
        self.assertEqual(still[0]["state"], MemoryState.CANDIDATE)
        self.assertEqual(still[0]["version"], 1)

    def test_confirm_with_user_confirmation_supersedes_candidate(self) -> None:
        created = self._create()
        confirmation = self._register(
            source_kind="user_confirmation",
            external_ref="confirm-1",
            snapshot=b"yes that is correct",
        )
        confirmed = self.store.confirm_claim(
            ALICE,
            str(created["claim_id"]),
            confirmation_source_id=str(confirmation["source_id"]),
        )
        self.assertTrue(CREATE_KEYS.issubset(confirmed.keys()))
        self.assertEqual(confirmed["claim_id"], created["claim_id"])
        self.assertEqual(confirmed["version"], 2)
        self.assertEqual(confirmed["state"], MemoryState.CONFIRMED)
        self.assertEqual(confirmed["supersedes_id"], created["claim_version_id"])
        self.assertIsNone(confirmed["expires_at"])
        self.assertEqual(confirmed["confidence"], 0.7)
        self.assertEqual(confirmed["sensitivity"], "normal")
        versions = self._rows(
            "SELECT version, state, supersedes_id FROM memory_claim_versions "
            "WHERE claim_id = ? ORDER BY version",
            (created["claim_id"],),
        )
        self.assertEqual(int(versions[0]["version"]), 1)
        self.assertEqual(versions[0]["state"], MemoryState.SUPERSEDED)
        self.assertEqual(int(versions[1]["version"]), 2)
        self.assertEqual(versions[1]["state"], MemoryState.CONFIRMED)
        listed = self.store.list_active_claims(ALICE)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["state"], MemoryState.CONFIRMED)
        self.assertEqual(listed[0]["version"], 2)
        support_ids = {
            str(row["source_id"])
            for row in self._rows(
                "SELECT source_id FROM memory_support WHERE claim_id = ?",
                (created["claim_id"],),
            )
        }
        self.assertIn(str(confirmation["source_id"]), support_ids)
        with self.assertRaises(ValidationError):
            self.store.confirm_claim(
                ALICE,
                str(created["claim_id"]),
                confirmation_source_id=str(confirmation["source_id"]),
            )

    def test_confirm_with_reliable_structured_source(self) -> None:
        created = self._create()
        calendar = self._register(
            source_kind="calendar_event",
            external_ref="cal-1",
            snapshot=b"window seat requested",
        )
        confirmed = self.store.confirm_claim(
            ALICE,
            str(created["claim_id"]),
            reliable_source_id=str(calendar["source_id"]),
        )
        self.assertEqual(confirmed["state"], MemoryState.CONFIRMED)
        self.assertEqual(confirmed["version"], 2)

    def test_correct_creates_next_version_and_supersedes(self) -> None:
        created = self._create()
        correction_source = self._register(
            external_ref="corr-1",
            snapshot=b"aisle seats instead",
        )
        corrected = self.store.correct_claim(
            ALICE,
            str(created["claim_id"]),
            content=b"Prefers aisle seats",
            explanation="User correction",
            source_ids=[str(correction_source["source_id"])],
        )
        self.assertEqual(corrected["claim_id"], created["claim_id"])
        self.assertEqual(corrected["version"], 2)
        self.assertEqual(corrected["state"], MemoryState.CANDIDATE)
        self.assertEqual(corrected["supersedes_id"], created["claim_version_id"])
        versions = self._rows(
            "SELECT id, version, state FROM memory_claim_versions "
            "WHERE claim_id = ? ORDER BY version",
            (created["claim_id"],),
        )
        self.assertEqual(versions[0]["state"], MemoryState.SUPERSEDED)
        self.assertEqual(str(versions[0]["id"]), created["claim_version_id"])
        self.assertEqual(versions[1]["state"], MemoryState.CANDIDATE)
        listed = self.store.list_active_claims(ALICE)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["claim_version_id"], corrected["claim_version_id"])
        keys = self._rows(
            "SELECT entity_kind, entity_id, version FROM memory_keys "
            "WHERE entity_kind = 'claim' ORDER BY version"
        )
        self.assertEqual(len(keys), 2)
        self.assertEqual(int(keys[0]["version"]), 1)
        self.assertEqual(int(keys[1]["version"]), 2)
        self.assertEqual(str(keys[0]["entity_id"]), created["claim_id"])
        store_text = Path(store_module.__file__).read_text(encoding="utf-8")
        life_text = Path(lifecycle_module.__file__).read_text(encoding="utf-8")
        self.assertTrue(
            "BEGIN IMMEDIATE" in store_text or "BEGIN IMMEDIATE" in life_text
        )

    def test_list_excludes_superseded_deleted_expired_and_restricted(self) -> None:
        active = self._create()
        restricted = self._create(
            source_ids=[str(self._register(external_ref="r1")["source_id"])],
            sensitivity="restricted",
            content=b"Home street is short",
        )
        expired = self._create(
            source_ids=[str(self._register(external_ref="e1")["source_id"])],
            content=b"Stale aisle guess",
        )
        deleted = self._create(
            source_ids=[str(self._register(external_ref="d1")["source_id"])],
            content=b"Old phone guess",
        )
        conn = self.store._connect()
        try:
            conn.execute(
                "UPDATE memory_claim_versions SET expires_at = ? WHERE claim_id = ?",
                ("2020-01-01T00:00:00Z", expired["claim_id"]),
            )
            conn.execute(
                "UPDATE memory_claim_versions SET state = ? WHERE claim_id = ?",
                (MemoryState.DELETED, deleted["claim_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        listed = self.store.list_active_claims(ALICE)
        ids = {row["claim_id"] for row in listed}
        self.assertIn(active["claim_id"], ids)
        self.assertNotIn(restricted["claim_id"], ids)
        self.assertNotIn(expired["claim_id"], ids)
        self.assertNotIn(deleted["claim_id"], ids)
        self.assertTrue(all(row["state"] != MemoryState.SUPERSEDED for row in listed))
        self.assertTrue(all(row["state"] != MemoryState.DELETED for row in listed))
        self.assertTrue(all(row["sensitivity"] != "restricted" for row in listed))

    def test_list_orders_newest_first_and_validates_limit(self) -> None:
        first = self._create(content=b"first preference")
        second = self._create(
            source_ids=[str(self._register(external_ref="n2")["source_id"])],
            claim_kind="task",
            content=b"second task",
        )
        listed = self.store.list_active_claims(ALICE)
        self.assertEqual(
            [row["claim_id"] for row in listed],
            [second["claim_id"], first["claim_id"]],
        )
        filtered = self.store.list_active_claims(ALICE, claim_kind="task")
        self.assertEqual([row["claim_id"] for row in filtered], [second["claim_id"]])
        limited = self.store.list_active_claims(ALICE, limit=1)
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0]["claim_id"], second["claim_id"])
        with self.assertRaises(ValidationError):
            self.store.list_active_claims(ALICE, limit=0)
        with self.assertRaises(ValidationError):
            self.store.list_active_claims(ALICE, limit=501)
        with self.assertRaises(ValidationError):
            self.store.list_active_claims(ALICE, limit=True)
        with self.assertRaises(ValidationError):
            self.store.list_active_claims(ALICE, claim_kind="Preference")

    def test_missing_and_foreign_claim_ids_are_byte_identical(self) -> None:
        created = self._create()
        missing_id = "00000000-0000-0000-0000-000000000000"
        with self.assertRaises(NotFoundError) as missing:
            self.store.confirm_claim(
                BOB,
                missing_id,
                confirmation_source_id=str(
                    self._register(BOB, external_ref="bob-c")["source_id"]
                ),
            )
        with self.assertRaises(NotFoundError) as foreign:
            self.store.confirm_claim(
                BOB,
                str(created["claim_id"]),
                confirmation_source_id=str(
                    self._register(BOB, external_ref="bob-c2")["source_id"]
                ),
            )
        self.assertEqual(str(missing.exception), "Claim not found")
        self.assertEqual(str(foreign.exception), "Claim not found")
        self.assertEqual(str(missing.exception), str(foreign.exception))
        with self.assertRaises(NotFoundError) as missing_corr:
            self.store.correct_claim(
                BOB,
                missing_id,
                content=b"x",
                explanation="x",
                source_ids=[
                    str(self._register(BOB, external_ref="bob-corr")["source_id"])
                ],
            )
        with self.assertRaises(NotFoundError) as foreign_corr:
            self.store.correct_claim(
                BOB,
                str(created["claim_id"]),
                content=b"x",
                explanation="x",
                source_ids=[
                    str(self._register(BOB, external_ref="bob-corr2")["source_id"])
                ],
            )
        self.assertEqual(str(missing_corr.exception), "Claim not found")
        self.assertEqual(str(foreign_corr.exception), "Claim not found")
        self.assertEqual(self.store.list_active_claims(BOB), [])
        self.assertEqual(len(self.store.list_active_claims(ALICE)), 1)

    def test_cross_tenant_source_ids_are_rejected_without_leak(self) -> None:
        alice_source = self._register(ALICE, external_ref="alice-src")
        bob_source = self._register(BOB, external_ref="bob-src")
        with self.assertRaises((ValidationError, NotFoundError)) as exc:
            self.store.create_candidate(
                ALICE,
                **_claim_kwargs([str(bob_source["source_id"])]),
            )
        message = str(exc.exception)
        self.assertNotIn(str(bob_source["source_id"]), message)
        self.assertNotIn("bob-src", message)
        created = self._create(source_ids=[str(alice_source["source_id"])])
        with self.assertRaises((ValidationError, NotFoundError)) as confirm_exc:
            self.store.confirm_claim(
                ALICE,
                str(created["claim_id"]),
                confirmation_source_id=str(bob_source["source_id"]),
            )
        self.assertNotIn(str(bob_source["source_id"]), str(confirm_exc.exception))

    def test_contradictory_candidates_coexist(self) -> None:
        tea = self._create(content=b"Prefers tea")
        coffee = self._create(
            source_ids=[str(self._register(external_ref="coffee")["source_id"])],
            content=b"Prefers coffee",
        )
        listed = self.store.list_active_claims(ALICE)
        ids = {row["claim_id"] for row in listed}
        self.assertEqual(ids, {tea["claim_id"], coffee["claim_id"]})
        self.assertTrue(all(row["state"] == MemoryState.CANDIDATE for row in listed))

    def test_valid_time_confidence_explanation_and_sensitivity_preserved(self) -> None:
        created = self._create(
            confidence=0.0,
            explanation="",
            sensitivity="sensitive",
            valid_from="2026-01-01T00:00:00Z",
            valid_to="2026-06-01T00:00:00Z",
        )
        self.assertEqual(created["confidence"], 0.0)
        self.assertEqual(created["sensitivity"], "sensitive")
        row = self._rows(
            "SELECT valid_from, valid_to, expires_at, confidence, sensitivity "
            "FROM memory_claim_versions WHERE id = ?",
            (created["claim_version_id"],),
        )[0]
        self.assertEqual(row["valid_from"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["valid_to"], "2026-06-01T00:00:00Z")
        self.assertEqual(row["expires_at"], EXPIRES)
        self.assertEqual(float(row["confidence"]), 0.0)
        confirmation = self._register(
            source_kind="user_confirmation",
            external_ref="preserve-c",
            snapshot=b"confirmed sensitive note",
        )
        confirmed = self.store.confirm_claim(
            ALICE,
            str(created["claim_id"]),
            confirmation_source_id=str(confirmation["source_id"]),
        )
        self.assertEqual(confirmed["sensitivity"], "sensitive")
        self.assertEqual(confirmed["confidence"], 0.0)
        confirmed_row = self._rows(
            "SELECT valid_from, valid_to FROM memory_claim_versions WHERE id = ?",
            (confirmed["claim_version_id"],),
        )[0]
        self.assertEqual(confirmed_row["valid_from"], "2026-01-01T00:00:00Z")
        self.assertEqual(confirmed_row["valid_to"], "2026-06-01T00:00:00Z")

    def test_one_sided_correction_rejects_inverted_valid_time(self) -> None:
        created = self._create(
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
        )
        later_source = self._register(
            external_ref="invert-from",
            snapshot=b"later window",
        )
        earlier_source = self._register(
            external_ref="invert-to",
            snapshot=b"earlier window",
        )
        keep_source = self._register(
            external_ref="keep-range",
            snapshot=b"kept window",
        )
        with self.assertRaises(ValidationError) as later_from:
            self.store.correct_claim(
                ALICE,
                str(created["claim_id"]),
                content=b"Prefers aisle seats",
                explanation="User correction",
                source_ids=[str(later_source["source_id"])],
                valid_from="2027-06-01T00:00:00Z",
            )
        self.assertIn("valid_to", str(later_from.exception))
        self.assertIn("valid_from", str(later_from.exception))
        with self.assertRaises(ValidationError) as earlier_to:
            self.store.correct_claim(
                ALICE,
                str(created["claim_id"]),
                content=b"Prefers aisle seats",
                explanation="User correction",
                source_ids=[str(earlier_source["source_id"])],
                valid_to="2026-01-01T00:00:00Z",
            )
        self.assertIn("valid_to", str(earlier_to.exception))
        versions = self._rows(
            "SELECT version, state, valid_from, valid_to FROM memory_claim_versions "
            "WHERE claim_id = ? ORDER BY version",
            (created["claim_id"],),
        )
        self.assertEqual(len(versions), 1)
        self.assertEqual(int(versions[0]["version"]), 1)
        self.assertEqual(versions[0]["state"], MemoryState.CANDIDATE)
        self.assertEqual(versions[0]["valid_from"], "2026-08-01T00:00:00Z")
        self.assertEqual(versions[0]["valid_to"], "2026-12-31T23:59:59Z")
        rejected = [
            row
            for row in self._audit()
            if row[2] == "claim.correct" and row[5] == "rejected"
        ]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(row[6] == "validation_failed" for row in rejected))
        kept = self.store.correct_claim(
            ALICE,
            str(created["claim_id"]),
            content=b"Prefers aisle seats",
            explanation="User correction",
            source_ids=[str(keep_source["source_id"])],
            valid_from="2026-09-01T00:00:00Z",
        )
        self.assertEqual(kept["version"], 2)
        kept_row = self._rows(
            "SELECT valid_from, valid_to FROM memory_claim_versions WHERE id = ?",
            (kept["claim_version_id"],),
        )[0]
        self.assertEqual(kept_row["valid_from"], "2026-09-01T00:00:00Z")
        self.assertEqual(kept_row["valid_to"], "2026-12-31T23:59:59Z")

    def test_claim_payloads_use_one_dek_and_field_aad(self) -> None:
        created = self._create()
        tenant_id = self.store.ensure_principal(ALICE)
        keys = self._rows(
            "SELECT entity_kind, entity_id, version, wrapped_dek FROM memory_keys "
            "WHERE entity_kind = 'claim'"
        )
        self.assertEqual(len(keys), 1)
        self.assertEqual(str(keys[0]["entity_id"]), created["claim_id"])
        self.assertEqual(int(keys[0]["version"]), 1)
        version_row = self._rows(
            "SELECT content_enc, explanation_enc FROM memory_claim_versions WHERE id = ?",
            (created["claim_version_id"],),
        )[0]
        wrap_aad = record_aad(tenant_id, "claim", str(created["claim_id"]), 1)
        dek = unwrap_dek(self.store._kek, bytes(keys[0]["wrapped_dek"]), wrap_aad)
        content = decrypt_field(
            dek,
            bytes(version_row["content_enc"]),
            field_aad(tenant_id, "claim", str(created["claim_id"]), 1, FIELD_CONTENT),
        )
        explanation = decrypt_field(
            dek,
            bytes(version_row["explanation_enc"]),
            field_aad(
                tenant_id, "claim", str(created["claim_id"]), 1, FIELD_EXPLANATION
            ),
        )
        self.assertEqual(content, CONTENT)
        self.assertEqual(explanation, EXPLANATION.encode("utf-8"))
        with self.assertRaises(CryptoError):
            decrypt_field(
                dek,
                bytes(version_row["content_enc"]),
                field_aad(
                    tenant_id, "claim", str(created["claim_id"]), 1, FIELD_EXPLANATION
                ),
            )
        with self.assertRaises(CryptoError):
            decrypt_field(
                dek,
                bytes(version_row["content_enc"]),
                field_aad(tenant_id, "claim", str(created["claim_id"]), 1, FIELD_SNAPSHOT),
            )
        self.assertIn(FIELD_CONTENT, FIELD_LABELS)
        self.assertIn(FIELD_EXPLANATION, FIELD_LABELS)
        self.assertIn(FIELD_METADATA, FIELD_LABELS)

    def test_database_bytes_do_not_contain_plaintext_claim_fixtures(self) -> None:
        self._create()
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
            for needle in CLAIM_NEEDLES:
                self.assertNotIn(needle, data, msg=f"{needle!r} leaked into {path.name}")

    def test_short_numeric_boolean_and_empty_fixtures(self) -> None:
        source = self._register(external_ref="fix-1")
        source_ids = [str(source["source_id"])]
        for content in (b"x", b"7", b"true"):
            created = self._create(
                source_ids=source_ids,
                content=content,
                explanation="",
                confidence=1.0,
            )
            self.assertEqual(created["state"], MemoryState.CANDIDATE)
            self.assertEqual(created["confidence"], 1.0)
        with self.assertRaises(ValidationError):
            self._create(source_ids=source_ids, content=b"")
        with self.assertRaises(ValidationError):
            self._create(source_ids=source_ids, confidence=True)
        with self.assertRaises(ValidationError):
            self._create(source_ids=source_ids, confidence=7)
        with self.assertRaises(ValidationError):
            self._create(source_ids=source_ids, explanation=7)

    def test_secret_payment_id_and_third_party_content_rejected(self) -> None:
        source_ids = [str(self._register(external_ref="bad")["source_id"])]
        blocked = (
            b"ghp_" + b"A" * 36,
            b"card 4111111111111111",
            b"passport number 123456789",
            b"another person's private address",
        )
        for content in blocked:
            with self.assertRaises(ValidationError) as exc:
                self._create(source_ids=source_ids, content=content)
            self.assertNotIn(content.decode("utf-8")[:8], str(exc.exception))

    def test_audit_allowlists_and_redacts_claim_actions(self) -> None:
        created = self._create()
        with self.assertRaises(ValidationError):
            self.store.create_candidate(
                ALICE, **_claim_kwargs([str(self._register(external_ref="badk")["source_id"])], claim_kind="note")
            )
        missing_id = "00000000-0000-0000-0000-000000000000"
        with self.assertRaises(NotFoundError):
            self.store.confirm_claim(
                BOB,
                missing_id,
                confirmation_source_id=str(
                    self._register(BOB, external_ref="bob-miss")["source_id"]
                ),
            )
        confirmation = self._register(
            source_kind="user_confirmation",
            external_ref="aud-c",
            snapshot=b"yes",
        )
        self.store.confirm_claim(
            ALICE,
            str(created["claim_id"]),
            confirmation_source_id=str(confirmation["source_id"]),
        )
        self.store.correct_claim(
            ALICE,
            str(created["claim_id"]),
            content=b"Prefers aisle seats",
            explanation="User correction",
            source_ids=[str(self._register(external_ref="aud-corr")["source_id"])],
        )
        self.store.list_active_claims(ALICE)

        claim_rows = [row for row in self._audit() if str(row[2]).startswith("claim.")]
        actions = {row[2] for row in claim_rows}
        outcomes = {row[5] for row in claim_rows}
        self.assertIn("claim.create_candidate", actions)
        self.assertIn("claim.confirm", actions)
        self.assertIn("claim.correct", actions)
        self.assertNotIn("claim.list", actions)
        self.assertIn("created", outcomes)
        self.assertIn("confirmed", outcomes)
        self.assertIn("superseded", outcomes)
        self.assertIn("rejected", outcomes)
        self.assertIn("denied", outcomes)
        denied = [row for row in claim_rows if row[5] == "denied"]
        self.assertTrue(denied)
        self.assertEqual(denied[0][2], "claim.confirm")
        self.assertEqual(denied[0][4], "")
        self.assertEqual(denied[0][6], "not_found")
        for row in claim_rows:
            self.assertIn(row[2], AUDIT_ACTIONS)
            self.assertIn(row[3], AUDIT_TARGET_KINDS)
            self.assertIn(row[5], AUDIT_OUTCOMES)
            self.assertIn(row[6], AUDIT_REASONS)
            joined = "|".join("" if item is None else str(item) for item in row)
            self.assertNotIn("Prefers window", joined)
            self.assertNotIn("Extracted from booking", joined)
            self.assertNotIn("Prefers aisle", joined)


if __name__ == "__main__":
    unittest.main()

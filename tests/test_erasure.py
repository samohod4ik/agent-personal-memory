"""Immediate-exclusion, cascade, and physical-content tests for erasure."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

import agent_personal_memory.erasure as erasure_module
import agent_personal_memory.store as store_module
from agent_personal_memory.erasure import ErasureReceipt
from agent_personal_memory.lifecycle import MemoryState
from agent_personal_memory.store import (
    AUDIT_ACTIONS,
    AUDIT_OUTCOMES,
    AUDIT_TARGET_KINDS,
    NotFoundError,
    PersonalMemoryStore,
)
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import content_hash_for

REPO_ROOT = Path(__file__).resolve().parents[1]

ALICE = Actor(platform="t", user_id="1", display_name="")
BOB = Actor(platform="t", user_id="2", display_name="7")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"
SOURCE_PLAIN = b"pmX"
CLAIM_PLAIN = b"pm0"
EXPLANATION = ""
LOCATOR = "ref7"
METADATA = {"title": "", "numeric": 7, "boolean": True, "empty": ""}
SOURCE_NEEDLES = (
    SOURCE_PLAIN,
    LOCATOR.encode("utf-8"),
    b'"numeric":7',
    b'"boolean":true',
)
CLAIM_NEEDLES = (CLAIM_PLAIN,)
RFC3339_Z = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
RECEIPT_FIELDS = (
    "operation",
    "target_kind",
    "receipt_id",
    "outcome",
    "erased_at",
    "backup_retention_note",
)


def _source_kwargs(**overrides: object) -> dict[str, object]:
    snapshot = overrides.get("snapshot", SOURCE_PLAIN)
    if not isinstance(snapshot, bytes):
        snapshot = SOURCE_PLAIN
    values: dict[str, object] = {
        "source_kind": "document",
        "external_ref": LOCATOR,
        "content_hash": content_hash_for(snapshot),
        "snapshot": snapshot,
        "observed_at": NOW,
        "retention_class": "standard",
        "metadata": dict(METADATA),
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
        "content": CLAIM_PLAIN,
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


class PersonalMemoryErasureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-erase-")
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

    def _pragma(self, name: str) -> object:
        conn = self.store._connect()
        try:
            return conn.execute(f"PRAGMA {name}").fetchone()[0]
        finally:
            conn.close()

    def _store_file_bytes(self) -> bytes:
        parts: list[bytes] = []
        for path in sorted(self.data_dir.iterdir()):
            if not path.is_file() or path.name == "master.key":
                continue
            parts.append(path.read_bytes())
        return b"".join(parts)

    def _capture_source_material(self, source_id: str) -> list[bytes]:
        blobs: list[bytes] = []
        for row in self._rows(
            "SELECT snapshot_enc, locator_enc, metadata_enc "
            "FROM memory_source_versions WHERE source_id = ?",
            (source_id,),
        ):
            for column in ("snapshot_enc", "locator_enc", "metadata_enc"):
                value = row[column]
                if value is not None:
                    blobs.append(bytes(value))
        for row in self._rows(
            "SELECT wrapped_dek FROM memory_keys "
            "WHERE entity_kind = 'source' AND entity_id = ?",
            (source_id,),
        ):
            blobs.append(bytes(row["wrapped_dek"]))
        return [item for item in blobs if item]

    def _capture_claim_material(self, claim_id: str) -> list[bytes]:
        blobs: list[bytes] = []
        for row in self._rows(
            "SELECT content_enc, explanation_enc FROM memory_claim_versions "
            "WHERE claim_id = ?",
            (claim_id,),
        ):
            for column in ("content_enc", "explanation_enc"):
                value = row[column]
                if value is not None:
                    blobs.append(bytes(value))
        for row in self._rows(
            "SELECT wrapped_dek FROM memory_keys "
            "WHERE entity_kind = 'claim' AND entity_id = ?",
            (claim_id,),
        ):
            blobs.append(bytes(row["wrapped_dek"]))
        return [item for item in blobs if item]

    def _assert_receipt(
        self,
        receipt: ErasureReceipt,
        *,
        operation: str,
        target_kind: str,
        forbidden_ids: set[str],
    ) -> None:
        self.assertIsInstance(receipt, ErasureReceipt)
        self.assertTrue(ErasureReceipt.__dataclass_params__.frozen)
        self.assertEqual(tuple(item.name for item in fields(ErasureReceipt)), RECEIPT_FIELDS)
        self.assertEqual(receipt.operation, operation)
        self.assertEqual(receipt.target_kind, target_kind)
        self.assertEqual(receipt.outcome, "erased")
        self.assertRegex(receipt.erased_at, RFC3339_Z)
        self.assertIsInstance(receipt.receipt_id, str)
        self.assertTrue(receipt.receipt_id)
        self.assertNotIn(receipt.receipt_id, forbidden_ids)
        note = receipt.backup_retention_note.lower()
        self.assertIn("backup", note)
        self.assertIn("retain", note)
        self.assertIn("retention", note)
        self.assertNotIn("physically purge", note)
        self.assertNotIn("already purged", note)

    def _assert_absent_from_files(self, needles: list[bytes]) -> None:
        haystack = self._store_file_bytes()
        for needle in needles:
            self.assertNotIn(needle, haystack)

    def test_missing_and_foreign_erase_are_byte_identical_not_found(self) -> None:
        created = self._create()
        source_id = str(self._register(external_ref="keep")["source_id"])
        missing = "00000000-0000-4000-8000-000000000000"
        fixtures = ("", "0", "7", "false", missing)
        for claim_id in fixtures:
            with self.subTest(claim_id=claim_id):
                with self.assertRaises(NotFoundError) as exc:
                    self.store.erase_claim(ALICE, claim_id)
                self.assertEqual(str(exc.exception), "Claim not found")
        for source_key in fixtures:
            with self.subTest(source_id=source_key):
                with self.assertRaises(NotFoundError) as exc:
                    self.store.erase_source(ALICE, source_key)
                self.assertEqual(str(exc.exception), "Source not found")
        with self.assertRaises(NotFoundError) as foreign_claim:
            self.store.erase_claim(BOB, str(created["claim_id"]))
        self.assertEqual(str(foreign_claim.exception), "Claim not found")
        with self.assertRaises(NotFoundError) as foreign_source:
            self.store.erase_source(BOB, source_id)
        self.assertEqual(str(foreign_source.exception), "Source not found")

    def test_erase_claim_excludes_from_list_immediately(self) -> None:
        kept_source = self._register(external_ref="keep-src")
        erased = self._create()
        kept = self._create(
            source_ids=[str(kept_source["source_id"])],
            content=b"pm1",
        )
        receipt = self.store.erase_claim(ALICE, str(erased["claim_id"]))
        self._assert_receipt(
            receipt,
            operation="claim.erase",
            target_kind="claim",
            forbidden_ids={str(erased["claim_id"]), str(erased["claim_version_id"])},
        )
        listed = self.store.list_active_claims(ALICE)
        ids = {str(row["claim_id"]) for row in listed}
        self.assertNotIn(str(erased["claim_id"]), ids)
        self.assertEqual(ids, {str(kept["claim_id"])})
        support = self._rows(
            "SELECT id FROM memory_support WHERE claim_id = ?",
            (erased["claim_id"],),
        )
        self.assertEqual(support, [])
        with self.assertRaises(NotFoundError) as repeat:
            self.store.erase_claim(ALICE, str(erased["claim_id"]))
        self.assertEqual(str(repeat.exception), "Claim not found")
        loaded = self.store.get_source(ALICE, str(kept_source["source_id"]))
        self.assertEqual(loaded["snapshot"], SOURCE_PLAIN)

    def test_erase_source_excludes_get_and_list_immediately(self) -> None:
        erased = self._register(external_ref="gone")
        kept = self._register(external_ref="stay")
        receipt = self.store.erase_source(ALICE, str(erased["source_id"]))
        self._assert_receipt(
            receipt,
            operation="source.erase",
            target_kind="source",
            forbidden_ids={str(erased["source_id"]), str(erased["source_version_id"])},
        )
        with self.assertRaises(NotFoundError) as missing:
            self.store.get_source(ALICE, str(erased["source_id"]))
        self.assertEqual(str(missing.exception), "Source not found")
        listed = {str(row["source_id"]) for row in self.store.list_sources(ALICE)}
        self.assertNotIn(str(erased["source_id"]), listed)
        self.assertIn(str(kept["source_id"]), listed)
        with self.assertRaises(NotFoundError) as repeat:
            self.store.erase_source(ALICE, str(erased["source_id"]))
        self.assertEqual(str(repeat.exception), "Source not found")

    def test_erase_source_cascades_dependent_claims_and_keeps_unrelated(self) -> None:
        shared = self._register(external_ref="cascade")
        extra = self._register(external_ref="extra")
        survivor_source = self._register(external_ref="survivor")
        dependent = self._create(source_ids=[str(shared["source_id"])])
        multi = self._create(
            source_ids=[str(shared["source_id"]), str(extra["source_id"])],
            content=b"pm1",
        )
        confirmation = self._register(
            source_kind="user_confirmation",
            external_ref="yes",
            snapshot=b"pmOK",
        )
        self.store.confirm_claim(
            ALICE,
            str(multi["claim_id"]),
            confirmation_source_id=str(confirmation["source_id"]),
        )
        self.store.correct_claim(
            ALICE,
            str(multi["claim_id"]),
            content=b"pm2",
            explanation="",
            source_ids=[str(shared["source_id"])],
        )
        survivor = self._create(
            source_ids=[str(survivor_source["source_id"])],
            content=b"pm7",
        )
        self.store.erase_source(ALICE, str(shared["source_id"]))
        listed = {str(row["claim_id"]) for row in self.store.list_active_claims(ALICE)}
        self.assertNotIn(str(dependent["claim_id"]), listed)
        self.assertNotIn(str(multi["claim_id"]), listed)
        self.assertEqual(listed, {str(survivor["claim_id"])})
        with self.assertRaises(NotFoundError):
            self.store.get_source(ALICE, str(shared["source_id"]))
        kept_extra = self.store.get_source(ALICE, str(extra["source_id"]))
        self.assertEqual(kept_extra["snapshot"], SOURCE_PLAIN)
        kept_survivor = self.store.get_source(ALICE, str(survivor_source["source_id"]))
        self.assertEqual(kept_survivor["snapshot"], SOURCE_PLAIN)
        support = self._rows(
            "SELECT claim_id, source_id FROM memory_support "
            "WHERE claim_id IN (?, ?) OR source_id = ?",
            (dependent["claim_id"], multi["claim_id"], shared["source_id"]),
        )
        self.assertEqual(support, [])

    def test_erase_claim_destroys_all_versions_and_physical_content(self) -> None:
        source = self._register(external_ref="claim-src")
        created = self._create(source_ids=[str(source["source_id"])])
        confirmation = self._register(
            source_kind="user_confirmation",
            external_ref="c1",
            snapshot=b"pmOK",
        )
        self.store.confirm_claim(
            ALICE,
            str(created["claim_id"]),
            confirmation_source_id=str(confirmation["source_id"]),
        )
        self.store.correct_claim(
            ALICE,
            str(created["claim_id"]),
            content=b"pm2",
            explanation="",
            source_ids=[str(source["source_id"])],
        )
        claim_id = str(created["claim_id"])
        material = self._capture_claim_material(claim_id)
        self.assertGreaterEqual(len(material), 4)
        self.store.erase_claim(ALICE, claim_id)
        versions = self._rows(
            "SELECT state, content_enc, explanation_enc FROM memory_claim_versions "
            "WHERE claim_id = ?",
            (claim_id,),
        )
        self.assertTrue(versions)
        for row in versions:
            self.assertEqual(row["state"], MemoryState.DELETED)
            self.assertIsNone(row["content_enc"])
            self.assertIsNone(row["explanation_enc"])
        keys = self._rows(
            "SELECT wrapped_dek FROM memory_keys "
            "WHERE entity_kind = 'claim' AND entity_id = ?",
            (claim_id,),
        )
        self.assertEqual(keys, [])
        self._assert_absent_from_files(material + list(CLAIM_NEEDLES) + [b"pm2"])
        self.assertEqual(self.store.get_source(ALICE, str(source["source_id"]))["snapshot"], SOURCE_PLAIN)

    def test_erase_source_purges_ciphertext_deks_and_scans_store_files(self) -> None:
        first = self._register(external_ref="v1")
        second = self._register(external_ref="v1", snapshot=b"pmY")
        self.assertEqual(first["source_id"], second["source_id"])
        self.assertEqual(second["version"], 2)
        claim = self._create(source_ids=[str(first["source_id"])])
        kept = self._register(external_ref="kept")
        source_id = str(first["source_id"])
        claim_id = str(claim["claim_id"])
        material = self._capture_source_material(source_id) + self._capture_claim_material(claim_id)
        self.assertTrue(material)
        self.store.erase_source(ALICE, source_id)
        versions = self._rows(
            "SELECT snapshot_enc, locator_enc, metadata_enc, content_hash, "
            "deletion_state FROM memory_source_versions WHERE source_id = ?",
            (source_id,),
        )
        self.assertTrue(versions)
        for row in versions:
            self.assertIsNone(row["snapshot_enc"])
            self.assertIsNone(row["locator_enc"])
            self.assertIsNone(row["metadata_enc"])
            self.assertEqual(row["content_hash"], "")
            self.assertEqual(row["deletion_state"], "deleted")
        keys = self._rows(
            "SELECT wrapped_dek FROM memory_keys WHERE entity_id IN (?, ?)",
            (source_id, claim_id),
        )
        self.assertEqual(keys, [])
        self._assert_absent_from_files(material + list(SOURCE_NEEDLES) + list(CLAIM_NEEDLES) + [b"pmY"])
        self.assertEqual(self.store.get_source(ALICE, str(kept["source_id"]))["snapshot"], SOURCE_PLAIN)
        self.assertEqual(int(self._pragma("freelist_count")), 0)
        self.assertEqual(str(self._pragma("secure_delete")).lower(), "1")
        checkpoint = self._rows("PRAGMA wal_checkpoint")
        self.assertTrue(checkpoint)

    def test_tombstone_is_anonymized_and_schema_records_manifest(self) -> None:
        created = self._create()
        claim_id = str(created["claim_id"])
        source_id = str(
            self._rows(
                "SELECT source_id FROM memory_support WHERE claim_id = ?",
                (claim_id,),
            )[0]["source_id"]
        )
        receipt = self.store.erase_claim(ALICE, claim_id)
        audit = self._rows(
            "SELECT action, target_kind, target_id, outcome, policy_reason "
            "FROM memory_audit WHERE action = 'claim.erase'"
        )
        self.assertTrue(audit)
        for row in audit:
            self.assertEqual(row["outcome"], "erased")
            self.assertNotEqual(row["target_id"], claim_id)
            self.assertNotEqual(row["target_id"], source_id)
            self.assertNotIn(claim_id, str(row["target_id"]))
            self.assertNotIn("pm0", str(row["target_id"]))
        manifest = self._rows(
            "SELECT receipt_id, operation, target_kind, outcome, erased_at "
            "FROM memory_deletion_manifest"
        )
        self.assertTrue(manifest)
        ids = {str(row["receipt_id"]) for row in manifest}
        self.assertIn(receipt.receipt_id, ids)
        for row in manifest:
            packed = " ".join(str(item) for item in tuple(row))
            self.assertNotIn(claim_id, packed)
            self.assertNotIn(source_id, packed)
            self.assertEqual(row["outcome"], "erased")
        self.assertIn("claim.erase", AUDIT_ACTIONS)
        self.assertIn("source.erase", AUDIT_ACTIONS)
        self.assertIn("erased", AUDIT_OUTCOMES)
        self.assertIn("claim", AUDIT_TARGET_KINDS)
        self.assertEqual(self.store.SCHEMA_VERSION, 4)
        conn = self.store._connect()
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()
        self.assertIn("memory_deletion_manifest", names)
        self.assertTrue((self.store.db_path.parent / "personal_memory.db").exists())
        store_text = Path(store_module.__file__).read_text(encoding="utf-8")
        erase_text = Path(erasure_module.__file__).read_text(encoding="utf-8")
        self.assertIn("BEGIN IMMEDIATE", store_text)
        self.assertIn("wal_checkpoint(TRUNCATE)", store_text + erase_text)
        vacuum_at = erase_text.find("VACUUM")
        self.assertGreaterEqual(vacuum_at, 0)
        self.assertIn("wal_checkpoint(TRUNCATE)", erase_text[vacuum_at:])
        self.assertIn("secure_delete", store_text + erase_text)

    def test_compact_failure_after_committed_erase_still_returns_receipt(self) -> None:
        created = self._create()
        claim_id = str(created["claim_id"])
        held = {"lock": False}

        def boom(_conn: sqlite3.Connection) -> None:
            held["lock"] = self.store._lock._is_owned()
            raise sqlite3.OperationalError("vacuum failed")

        with patch("agent_personal_memory.store.compact_sqlite_store", side_effect=boom):
            with self.assertLogs("plugins.personal-memory", level="ERROR"):
                receipt = self.store.erase_claim(ALICE, claim_id)
        self.assertTrue(held["lock"])
        self._assert_receipt(
            receipt,
            operation="claim.erase",
            target_kind="claim",
            forbidden_ids={claim_id, str(created["claim_version_id"])},
        )
        listed = {str(row["claim_id"]) for row in self.store.list_active_claims(ALICE)}
        self.assertNotIn(claim_id, listed)
        adr = (
            REPO_ROOT / "docs" / "adr" / "0008-physical-erasure-and-tombstones.md"
        ).read_text(encoding="utf-8")
        self.assertIn("secure_delete", adr)
        self.assertIn("wal_checkpoint", adr)
        self.assertIn("tombstone", adr.lower())
        self.assertIn("backup", adr.lower())
        self.assertIn("DEK", adr)


if __name__ == "__main__":
    unittest.main()

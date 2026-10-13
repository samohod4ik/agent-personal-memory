"""KEK/DEK AES-GCM tests for the personal-memory crypto foundation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.crypto import (
    FIELD_CONTENT,
    FIELD_EXPLANATION,
    FIELD_LABELS,
    FIELD_LOCATOR,
    FIELD_METADATA,
    FIELD_SNAPSHOT,
    CryptoError,
    decrypt_field,
    decrypt_record,
    derive_lookup_key,
    encrypt_field,
    encrypt_record,
    ensure_master_key,
    field_aad,
    keyed_lookup_hash,
    new_dek,
    record_aad,
    unwrap_dek,
)
from agent_personal_memory.store import PersonalMemoryStore
from agent_personal_memory.types import Actor

FIXTURE_VALUES = (
    ("flag", "x"),
    ("numeric", "7"),
    ("boolean", "true"),
    ("empty", ""),
)


class PersonalMemoryCryptoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pm-crypto-")
        self.data_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_master_key_is_kek_and_records_use_wrapped_deks(self) -> None:
        kek = ensure_master_key(self.data_dir / "master.key")
        self.assertEqual(len(kek), 32)
        again = ensure_master_key(self.data_dir / "master.key")
        self.assertEqual(kek, again)

        wrapped_blobs = []
        ciphertexts = []
        for kind, value in FIXTURE_VALUES:
            wrapped, blob = encrypt_record(
                kek,
                value.encode("utf-8"),
                tenant_id="1",
                entity_kind="claim",
                entity_id=kind,
                version=1,
            )
            wrapped_blobs.append(wrapped)
            ciphertexts.append(blob)
            recovered = decrypt_record(
                kek,
                wrapped,
                blob,
                tenant_id="1",
                entity_kind="claim",
                entity_id=kind,
                version=1,
            )
            self.assertEqual(recovered.decode("utf-8"), value)
            self.assertNotEqual(wrapped, blob)
            if len(value) >= 4:
                self.assertNotIn(value.encode("utf-8"), wrapped)
                self.assertNotIn(value.encode("utf-8"), blob)

        self.assertEqual(len({item for item in wrapped_blobs}), len(FIXTURE_VALUES))
        self.assertEqual(len({item for item in ciphertexts}), len(FIXTURE_VALUES))
        with self.assertRaises(CryptoError):
            decrypt_record(
                kek,
                wrapped_blobs[0],
                ciphertexts[0],
                tenant_id="1",
                entity_kind="claim",
                entity_id="flag",
                version=2,
            )

    def test_record_aad_binds_tenant_kind_id_and_version(self) -> None:
        first = record_aad("1", "source", "s1", 1)
        self.assertNotEqual(first, record_aad("2", "source", "s1", 1))
        self.assertNotEqual(first, record_aad("1", "claim", "s1", 1))
        self.assertNotEqual(first, record_aad("1", "source", "s2", 1))
        self.assertNotEqual(first, record_aad("1", "source", "s1", 2))

    def test_store_persists_only_kek_wrapped_deks_in_memory_keys(self) -> None:
        store = PersonalMemoryStore(data_dir=self.data_dir)
        tenant_id = store.ensure_principal(Actor(platform="t", user_id="1", display_name=""))
        wrapped, blob = store.encrypt_and_persist_key(
            plaintext=b"true",
            tenant_id=tenant_id,
            entity_kind="claim",
            entity_id="c1",
            version=1,
        )
        recovered = store.decrypt_with_persisted_key(
            blob,
            tenant_id=tenant_id,
            entity_kind="claim",
            entity_id="c1",
            version=1,
        )
        self.assertEqual(recovered, b"true")
        conn = store._connect()
        try:
            row = conn.execute(
                "SELECT wrapped_dek FROM memory_keys "
                "WHERE tenant_id = ? AND entity_kind = ? AND entity_id = ? AND version = ?",
                (tenant_id, "claim", "c1", 1),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(bytes(row[0]), wrapped)
        self.assertNotIn(b"true", bytes(row[0]))

    def test_field_constants_and_aad_are_column_bound(self) -> None:
        self.assertEqual(FIELD_SNAPSHOT, "snapshot_enc")
        self.assertEqual(FIELD_LOCATOR, "locator_enc")
        self.assertEqual(FIELD_METADATA, "metadata_enc")
        self.assertEqual(FIELD_CONTENT, "content_enc")
        self.assertEqual(FIELD_EXPLANATION, "explanation_enc")
        self.assertEqual(
            FIELD_LABELS,
            frozenset(
                {
                    FIELD_SNAPSHOT,
                    FIELD_LOCATOR,
                    FIELD_METADATA,
                    FIELD_CONTENT,
                    FIELD_EXPLANATION,
                }
            ),
        )
        wrap = record_aad("1", "source", "s1", 1)
        snap = field_aad("1", "source", "s1", 1, FIELD_SNAPSHOT)
        loc = field_aad("1", "source", "s1", 1, FIELD_LOCATOR)
        meta = field_aad("1", "source", "s1", 1, FIELD_METADATA)
        self.assertTrue(snap.startswith(wrap + b"\0"))
        self.assertEqual({wrap, snap, loc, meta}, {wrap, snap, loc, meta})
        self.assertEqual(len({wrap, snap, loc, meta}), 4)
        wraps = {
            record_aad("t1", "source", source_id, version)
            for source_id in ("s1", "s2")
            for version in (1, 2, 11)
        }
        fields = {
            field_aad("t1", "source", source_id, version, label)
            for source_id in ("s1", "s2")
            for version in (1, 2, 11)
            for label in FIELD_LABELS
        }
        self.assertTrue(wraps.isdisjoint(fields))
        self.assertEqual(len(fields), 30)
        with self.assertRaises(CryptoError):
            field_aad("1", "source", "s1", 1, "external_ref")

    def test_encrypt_field_rejects_swapped_labels_and_versions(self) -> None:
        dek = new_dek()
        self.assertEqual(len(dek), 32)
        self.assertNotEqual(dek, new_dek())
        snap_aad = field_aad("1", "source", "s1", 1, FIELD_SNAPSHOT)
        meta_aad = field_aad("1", "source", "s1", 1, FIELD_METADATA)
        loc_aad = field_aad("1", "source", "s1", 1, FIELD_LOCATOR)
        v2_aad = field_aad("1", "source", "s1", 2, FIELD_SNAPSHOT)
        snapshot = encrypt_field(dek, b"x", snap_aad)
        metadata = encrypt_field(dek, b'{"flag":"x"}', meta_aad)
        locator = encrypt_field(dek, b"7", loc_aad)
        self.assertEqual(decrypt_field(dek, snapshot, snap_aad), b"x")
        self.assertEqual(decrypt_field(dek, metadata, meta_aad), b'{"flag":"x"}')
        self.assertEqual(decrypt_field(dek, locator, loc_aad), b"7")
        with self.assertRaises(CryptoError):
            decrypt_field(dek, snapshot, meta_aad)
        with self.assertRaises(CryptoError):
            decrypt_field(dek, snapshot, loc_aad)
        with self.assertRaises(CryptoError):
            decrypt_field(dek, metadata, snap_aad)
        with self.assertRaises(CryptoError):
            decrypt_field(dek, snapshot, v2_aad)

    def test_lookup_key_is_hkdf_and_hash_binds_tenant_kind_and_ref(self) -> None:
        kek = ensure_master_key(self.data_dir / "lookup.key")
        lookup = derive_lookup_key(kek)
        self.assertEqual(len(lookup), 32)
        self.assertEqual(lookup, derive_lookup_key(kek))
        self.assertNotEqual(lookup, kek)
        first = keyed_lookup_hash(lookup, "t1", "document", "msg-1")
        self.assertEqual(len(first), 64)
        self.assertTrue(all(ch in "0123456789abcdef" for ch in first))
        self.assertEqual(first, keyed_lookup_hash(lookup, "t1", "document", "msg-1"))
        self.assertNotEqual(first, keyed_lookup_hash(lookup, "t2", "document", "msg-1"))
        self.assertNotEqual(first, keyed_lookup_hash(lookup, "t1", "email_message", "msg-1"))
        self.assertNotEqual(first, keyed_lookup_hash(lookup, "t1", "document", "msg-2"))
        self.assertNotEqual(first, keyed_lookup_hash(lookup, "t1", "document", ""))


if __name__ == "__main__":
    unittest.main()

"""Closed-vocabulary and secret-rejection tests for source validation."""

from __future__ import annotations

import hashlib
import unittest

from agent_personal_memory.validation import (
    CLAIM_KINDS,
    CLAIM_KIND_VOCAB_VERSION,
    CREDENTIAL_MARKERS,
    OPAQUE_CREDENTIAL_RUN,
    RELIABLE_SOURCE_KINDS,
    RETENTION_CLASSES,
    RETENTION_CLASS_VOCAB_VERSION,
    SECRET_KEY_SEGMENTS,
    SENSITIVITY_CLASSES,
    SENSITIVITY_VOCAB_VERSION,
    SOURCE_KINDS,
    SOURCE_KIND_VOCAB_VERSION,
    ValidationError,
    canonicalize_metadata,
    content_hash_for,
    validate_claim_content,
    validate_claim_kind,
    validate_confidence,
    validate_correct_fields,
    validate_expires_at,
    validate_explanation,
    validate_external_ref,
    validate_list_limit,
    validate_metadata,
    validate_observed_at,
    validate_optional_claim_kind,
    validate_register_fields,
    validate_retention_class,
    validate_sensitivity,
    validate_snapshot,
    validate_source_kind,
    validate_query_claim_kinds,
    validate_query_source_kinds,
    validate_query_states,
    validate_query_time_window,
    validate_valid_time_order,
)

BENIGN_STRINGS = (
    "",
    "x",
    "7",
    "true",
    "Hotel booking confirmation for Lisbon",
    "2026-08-31T10:00:00Z",
    "9f2c1a4e8b7d6c5a9f2c1a4e8b7d6c5a9f2c1a4e8b7d6c5a9f2c1a4e8b7d6c5a",
    "b3f1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    "https://disk.yandex.ru/d/AbCdEf0123456789",
    "Author: Alex Muravev",
    "message_id=118824",
    "Dogovor arendy 2026",
)

MARKER_SECRETS = (
    "ghp_" + "A" * 36,
    "sk-" + "a1B2" * 12,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "Bearer ya29.abcdefghijklmnop",
    "-----BEGIN RSA PRIVATE KEY-----",
    "AKIAIOSFODNN7EXAMPLE",
)
OPAQUE_SECRET = "AbCdEf0123456789AbCdEf0123456789AbCdEf0123456789"

SECRET_KEYS = (
    "api_key",
    "auth_token",
    "refresh_token",
    "session_cookie",
    "aws_secret_access_key",
    "private_key",
    "seed_phrase",
)
ORDINARY_KEYS = (
    "author",
    "title",
    "message_id",
    "chat_id",
    "keyword",
    "mime_type",
    "trip_id",
    "page_count",
    "authored_at",
    "language",
)


class PersonalMemoryValidationTests(unittest.TestCase):
    def test_vocabularies_are_exact_and_versioned(self) -> None:
        self.assertEqual(SOURCE_KIND_VOCAB_VERSION, 2)
        self.assertEqual(RETENTION_CLASS_VOCAB_VERSION, 1)
        self.assertEqual(
            SOURCE_KINDS,
            frozenset(
                {
                    "user_message",
                    "user_confirmation",
                    "assistant_message",
                    "tool_result",
                    "document",
                    "email_message",
                    "calendar_event",
                    "travel_record",
                    "import_batch",
                    "auto_insight_turn",
                }
            ),
        )
        self.assertEqual(RETENTION_CLASSES, frozenset({"ephemeral", "standard", "durable"}))
        with self.assertRaises(ValidationError) as kind_exc:
            validate_source_kind("Document")
        self.assertNotIn("Document", str(kind_exc.exception))
        with self.assertRaises(ValidationError) as empty_exc:
            validate_source_kind("")
        self.assertEqual(empty_exc.exception.__class__.__mro__[1], ValueError)
        with self.assertRaises(ValidationError):
            validate_retention_class("STANDARD")
        with self.assertRaises(ValidationError):
            validate_retention_class("")

    def test_field_limits_and_content_hash(self) -> None:
        snapshot = b"x"
        digest = content_hash_for(snapshot)
        self.assertEqual(digest, hashlib.sha256(snapshot).hexdigest())
        self.assertEqual(validate_snapshot(snapshot), snapshot)
        self.assertEqual(validate_external_ref("7"), "7")
        self.assertEqual(validate_observed_at("2026-08-31T10:00:00Z"), "2026-08-31T10:00:00Z")
        with self.assertRaises(ValidationError) as snap_exc:
            validate_snapshot(b"")
        self.assertNotIn("b''", str(snap_exc.exception))
        with self.assertRaises(ValidationError):
            validate_snapshot(b"y" * 65537)
        with self.assertRaises(ValidationError) as ref_exc:
            validate_external_ref("")
        self.assertTrue(str(ref_exc.exception).startswith("external_ref:"))
        with self.assertRaises(ValidationError) as nul_exc:
            validate_external_ref("msg\0id")
        self.assertNotIn("msg", str(nul_exc.exception))
        with self.assertRaises(ValidationError):
            validate_external_ref("r" * 513)
        with self.assertRaises(ValidationError) as hash_exc:
            validate_register_fields(
                source_kind="document",
                external_ref="ref-1",
                content_hash="A" * 64,
                snapshot=snapshot,
                observed_at="2026-08-31T10:00:00Z",
                retention_class="standard",
                metadata=None,
            )
        self.assertNotIn("A" * 8, str(hash_exc.exception))
        with self.assertRaises(ValidationError):
            validate_observed_at("2026-08-31T10:00:00+00:00")
        with self.assertRaises(ValidationError):
            validate_observed_at("1999-12-31T23:59:59Z")
        with self.assertRaises(ValidationError):
            validate_observed_at("2099-01-01T00:00:00Z")
        with self.assertRaises(ValidationError) as calendar_exc:
            validate_observed_at("2026-02-31T10:00:00Z")
        self.assertIsInstance(calendar_exc.exception, ValidationError)
        self.assertTrue(str(calendar_exc.exception).startswith("observed_at:"))
        with self.assertRaises(ValidationError) as limit_exc:
            validate_list_limit(0)
        self.assertTrue(str(limit_exc.exception).startswith("limit:"))
        self.assertNotIn("limit: 0", str(limit_exc.exception))
        with self.assertRaises(ValidationError):
            validate_list_limit(501)
        self.assertEqual(validate_list_limit(1), 1)
        self.assertEqual(validate_list_limit(500), 500)

    def test_metadata_accepts_short_numeric_boolean_and_empty(self) -> None:
        payload = {"flag": "x", "numeric": 7, "boolean": True, "empty": "", "none": None}
        cleaned = validate_metadata(payload)
        self.assertIsInstance(cleaned["boolean"], bool)
        self.assertTrue(cleaned["boolean"] is True)
        self.assertIsInstance(cleaned["numeric"], int)
        self.assertFalse(isinstance(cleaned["numeric"], bool))
        self.assertEqual(cleaned["flag"], "x")
        self.assertEqual(cleaned["empty"], "")
        self.assertIsNone(cleaned["none"])
        self.assertEqual(
            canonicalize_metadata(None),
            canonicalize_metadata({}),
        )
        self.assertEqual(canonicalize_metadata(None), b"{}")
        self.assertEqual(validate_metadata(None), {})
        self.assertEqual(validate_metadata({}), {})
        with self.assertRaises(ValidationError):
            validate_metadata({"": "x"})
        with self.assertRaises(ValidationError):
            validate_metadata({"Title": "x"})
        with self.assertRaises(ValidationError):
            validate_metadata({"nested": {"a": 1}})
        with self.assertRaises(ValidationError):
            validate_metadata({"items": ["x"]})
        with self.assertRaises(ValidationError):
            validate_metadata({"ratio": 1.5})
        with self.assertRaises(ValidationError):
            validate_metadata({"too": 2**53 + 1})
        with self.assertRaises(ValidationError):
            validate_metadata({"note": "x" * 257})
        with self.assertRaises(ValidationError):
            validate_metadata({f"k{i}": "x" for i in range(21)})

    def test_secret_detection_is_asymmetric(self) -> None:
        for key in SECRET_KEYS:
            with self.assertRaises(ValidationError) as exc:
                validate_metadata({key: "x"})
            message = str(exc.exception)
            self.assertIn(key, message)
            self.assertNotIn("x", message.split(key, 1)[-1])
        for key in ORDINARY_KEYS:
            validate_metadata({key: "ok"})
        for value in MARKER_SECRETS:
            with self.assertRaises(ValidationError) as meta_exc:
                validate_metadata({"title": value})
            self.assertNotIn(value[:8], str(meta_exc.exception))
            with self.assertRaises(ValidationError) as ref_exc:
                validate_external_ref(value)
            self.assertNotIn(value[:8], str(ref_exc.exception))
            with self.assertRaises(ValidationError) as snap_exc:
                validate_snapshot(value.encode("utf-8"))
            self.assertNotIn(value[:8], str(snap_exc.exception))
        with self.assertRaises(ValidationError):
            validate_metadata({"title": OPAQUE_SECRET})
        self.assertEqual(validate_external_ref(OPAQUE_SECRET), OPAQUE_SECRET)
        self.assertEqual(validate_snapshot(OPAQUE_SECRET.encode("utf-8")), OPAQUE_SECRET.encode("utf-8"))
        self.assertEqual(validate_snapshot(b"\xff\x00binary"), b"\xff\x00binary")
        for value in BENIGN_STRINGS:
            if value == "":
                continue
            validate_external_ref(value)
            if 1 <= len(value.encode("utf-8")) <= 65536:
                validate_snapshot(value.encode("utf-8"))
            validate_metadata({"title": value} if value else {"title": "x", "empty": value})
        self.assertTrue(CREDENTIAL_MARKERS.search("-----BEGIN RSA PRIVATE KEY-----"))
        self.assertTrue(CREDENTIAL_MARKERS.search("Bearer ya29.abcdefghijklmnop"))
        embedded_tokens = (
            "token ghp_" + "A" * 36,
            "prefix sk-" + "a1B2" * 12,
            "id=AKIAIOSFODNN7EXAMPLE",
            "hdr eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk txt",
        )
        for value in embedded_tokens:
            self.assertTrue(CREDENTIAL_MARKERS.search(value), msg=value[:12])
            with self.assertRaises(ValidationError) as meta_exc:
                validate_metadata({"title": value})
            self.assertNotIn(value[:8], str(meta_exc.exception))
            with self.assertRaises(ValidationError) as ref_exc:
                validate_external_ref(value)
            self.assertNotIn(value[:8], str(ref_exc.exception))
            with self.assertRaises(ValidationError) as snap_exc:
                validate_snapshot(value.encode("utf-8"))
            self.assertNotIn(value[:8], str(snap_exc.exception))
        self.assertTrue(OPAQUE_CREDENTIAL_RUN.match(OPAQUE_SECRET))
        self.assertIn("key", SECRET_KEY_SEGMENTS)
        self.assertNotIn("access", SECRET_KEY_SEGMENTS)

    def test_hyphenated_words_do_not_match_embedded_credential_prefixes(self) -> None:
        value = "risk-based-retention"
        self.assertIsNone(CREDENTIAL_MARKERS.search(value))
        self.assertEqual(validate_external_ref(value), value)
        validate_metadata({"title": value})
        encoded = value.encode("utf-8")
        self.assertEqual(validate_snapshot(encoded), encoded)
        self.assertEqual(validate_claim_content(encoded), encoded)
        self.assertEqual(validate_explanation(value), value)
        embedded_tokens = (
            "token ghp_" + "A" * 36,
            "prefix sk-" + "a1B2" * 12,
            "prefix sk-live_" + "a1B2" * 8,
            "id=AKIAIOSFODNN7EXAMPLE",
        )
        for secret in embedded_tokens:
            self.assertTrue(CREDENTIAL_MARKERS.search(secret), msg=secret[:16])
            with self.assertRaises(ValidationError):
                validate_external_ref(secret)

    def test_claim_vocabularies_confidence_expiry_and_content(self) -> None:
        self.assertEqual(CLAIM_KIND_VOCAB_VERSION, 1)
        self.assertEqual(SENSITIVITY_VOCAB_VERSION, 1)
        self.assertEqual(
            CLAIM_KINDS,
            frozenset(
                {
                    "raw_event",
                    "candidate_insight",
                    "confirmed_fact",
                    "preference",
                    "task",
                    "decision",
                    "summary",
                }
            ),
        )
        self.assertEqual(
            SENSITIVITY_CLASSES, frozenset({"normal", "sensitive", "restricted"})
        )
        self.assertEqual(
            RELIABLE_SOURCE_KINDS,
            frozenset(
                {
                    "calendar_event",
                    "travel_record",
                    "email_message",
                    "tool_result",
                    "document",
                }
            ),
        )
        self.assertEqual(validate_claim_kind("preference"), "preference")
        self.assertEqual(validate_sensitivity("restricted"), "restricted")
        self.assertEqual(validate_confidence(0.0), 0.0)
        self.assertEqual(validate_confidence(1.0), 1.0)
        self.assertEqual(validate_expires_at("2099-01-01T00:00:00Z"), "2099-01-01T00:00:00Z")
        self.assertEqual(validate_claim_content(b"x"), b"x")
        self.assertEqual(validate_claim_content(b"7"), b"7")
        self.assertEqual(validate_claim_content(b"true"), b"true")
        self.assertEqual(validate_explanation(""), "")
        self.assertEqual(validate_explanation(None), "")
        self.assertIsNone(validate_optional_claim_kind(None))
        with self.assertRaises(ValidationError):
            validate_claim_kind("Preference")
        with self.assertRaises(ValidationError):
            validate_sensitivity("NORMAL")
        with self.assertRaises(ValidationError):
            validate_confidence(True)
        with self.assertRaises(ValidationError):
            validate_confidence(7)
        with self.assertRaises(ValidationError):
            validate_confidence(-0.1)
        with self.assertRaises(ValidationError):
            validate_expires_at("2020-01-01T00:00:00Z")
        with self.assertRaises(ValidationError):
            validate_expires_at("2099-01-01T00:00:00+00:00")
        with self.assertRaises(ValidationError):
            validate_claim_content(b"")
        with self.assertRaises(ValidationError):
            validate_claim_content(b"ghp_" + b"A" * 36)
        with self.assertRaises(ValidationError):
            validate_claim_content(b"card 4111111111111111")
        with self.assertRaises(ValidationError):
            validate_claim_content(b"passport number 123456789")
        with self.assertRaises(ValidationError):
            validate_claim_content(b"another person's private address")

    def test_one_sided_correct_fields_defer_range_until_merged_bounds(self) -> None:
        fields = validate_correct_fields(
            content=b"Prefers aisle seats",
            explanation="User correction",
            source_ids=["00000000-0000-0000-0000-000000000001"],
            valid_from="2027-06-01T00:00:00Z",
        )
        self.assertEqual(fields["valid_from"], "2027-06-01T00:00:00Z")
        self.assertIsNone(fields["valid_to"])
        with self.assertRaises(ValidationError) as merged:
            validate_valid_time_order(fields["valid_from"], "2026-12-31T23:59:59Z")
        self.assertEqual(str(merged.exception), "valid_to: must be on or after valid_from")
        with self.assertRaises(ValidationError):
            validate_valid_time_order("2026-08-01T00:00:00Z", "2026-01-01T00:00:00Z")
        validate_valid_time_order("2026-09-01T00:00:00Z", "2026-12-31T23:59:59Z")
        validate_valid_time_order("2026-08-01T00:00:00Z", None)
        validate_valid_time_order(None, "2026-01-01T00:00:00Z")
        with self.assertRaises(ValidationError):
            validate_correct_fields(
                content=b"Prefers aisle seats",
                explanation="User correction",
                source_ids=["00000000-0000-0000-0000-000000000001"],
                valid_from="2027-06-01T00:00:00Z",
                valid_to="2026-12-31T23:59:59Z",
            )

    def test_query_filter_collections_and_windows(self) -> None:
        self.assertIsNone(validate_query_states(None))
        self.assertIsNone(validate_query_claim_kinds(None))
        self.assertIsNone(validate_query_source_kinds(None))
        self.assertEqual(validate_query_states(["candidate", "confirmed"]), ["candidate", "confirmed"])
        self.assertEqual(validate_query_claim_kinds(["preference", "task"]), ["preference", "task"])
        self.assertEqual(validate_query_source_kinds(["document"]), ["document"])
        self.assertEqual(
            validate_query_time_window("valid_from", None, "valid_to", None),
            (None, None),
        )
        self.assertEqual(
            validate_query_time_window(
                "observed_from",
                "2026-08-01T00:00:00Z",
                "observed_to",
                "2026-08-31T00:00:00Z",
            ),
            ("2026-08-01T00:00:00Z", "2026-08-31T00:00:00Z"),
        )
        with self.assertRaises(ValidationError):
            validate_query_states(["Candidate"])
        with self.assertRaises(ValidationError):
            validate_query_claim_kinds("preference")
        with self.assertRaises(ValidationError):
            validate_query_source_kinds(["Document"])
        with self.assertRaises(ValidationError):
            validate_query_time_window(
                "valid_from",
                "2026-12-31T00:00:00Z",
                "valid_to",
                "2026-01-01T00:00:00Z",
            )
        with self.assertRaises(ValidationError):
            validate_query_time_window(
                "observed_from",
                "2026-08-31T10:00:00+00:00",
                "observed_to",
                None,
            )


if __name__ == "__main__":
    unittest.main()

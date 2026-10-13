"""Two-phase confirm-gate tests (adapted from source test_trusted_delete.py).

The source drove the gate through a host inbound-command dispatch seam; the
standalone module instead takes an injected ``send(text) -> bool`` transport
and an explicit ``SessionBinding``. FakeSend below mirrors the source's
FakeAdapter semantics (exception and falsy-result sends both fail).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_personal_memory.confirmation import (
    REGISTRY,
    BoundTarget,
    DeleteRegistry,
    DeleteRequest,
    SessionBinding,
    TrustedDeleteDenied,
    consume_delete_request,
    create_delete_preview,
    reject_legacy_confirm,
    reset_registry,
)
from agent_personal_memory.query import query_claims
from agent_personal_memory.store import ConflictError, PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import ValidationError, content_hash_for

ALICE = Actor(platform="t", user_id="41", display_name="")
BOB = Actor(platform="t", user_id="42", display_name="")
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"
SESSION_KEY = "agent:main:telegram:dm:41:41"
SESSION_ID = "sess-alice-1"


def _binding(**overrides: object) -> SessionBinding:
    values = {
        "platform": "telegram",
        "user_id": "41",
        "session_key": SESSION_KEY,
        "session_id": SESSION_ID,
        "chat_id": "41",
        "thread_id": "",
        "profile": "",
        "display_name": "",
    }
    values.update(overrides)
    return SessionBinding(**values)  # type: ignore[arg-type]


class FakeSend:
    """Injected transport: records payloads, controllable success/failure."""

    def __init__(self, *, fail: bool = False, delivered: bool = True) -> None:
        self.fail = fail
        self.delivered = delivered
        self.sent: list[str] = []

    def __call__(self, text: str) -> bool:
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append(str(text))
        return bool(self.delivered)


def _nonce_from_sent(send: FakeSend) -> str:
    body = send.sent[-1]
    for line in body.splitlines():
        if "/memory_confirm" in line:
            return line.split()[-1]
    raise AssertionError("nonce missing from preview")


def _dummy_target() -> BoundTarget:
    return BoundTarget(
        claim_id="claim-1",
        version=1,
        claim_version_id="ver-1",
        content_hash="hash-1",
    )


class ConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-confirm-")
        self.data_dir = Path(self.tmp.name)
        reset_registry()
        self.store = PersonalMemoryStore(data_dir=self.data_dir)
        source = self.store.register_source(
            ALICE,
            source_kind="document",
            external_ref="ref-1",
            content_hash=content_hash_for(b"pmA"),
            snapshot=b"pmA",
            observed_at=NOW,
            retention_class="standard",
            metadata={"title": "A"},
        )
        created = self.store.create_candidate(
            ALICE,
            claim_kind="preference",
            content=b"allergic to peanuts",
            confidence=0.7,
            explanation="",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
            expires_at=EXPIRES,
        )
        self.source_id = str(source["source_id"])
        self.claim_id = str(created["claim_id"])
        extra = self.store.create_candidate(
            ALICE,
            claim_kind="preference",
            content=b"also allergic to peanuts at night",
            confidence=0.6,
            explanation="",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
            expires_at=EXPIRES,
        )
        self.extra_id = str(extra["claim_id"])
        bob_source = self.store.register_source(
            BOB,
            source_kind="document",
            external_ref="ref-2",
            content_hash=content_hash_for(b"pmB"),
            snapshot=b"pmB",
            observed_at=NOW,
            retention_class="standard",
            metadata={"title": "B"},
        )
        bob = self.store.create_candidate(
            BOB,
            claim_kind="preference",
            content=b"bob secret",
            confidence=0.4,
            explanation="",
            sensitivity="normal",
            source_ids=[str(bob_source["source_id"])],
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
            expires_at=EXPIRES,
        )
        self.bob_claim_id = str(bob["claim_id"])

    def tearDown(self) -> None:
        reset_registry()
        self.tmp.cleanup()

    def _ids(self) -> set[str]:
        return {str(row["claim_id"]) for row in query_claims(ALICE, store=self.store)}

    def _preview(self, **kwargs: object) -> dict[str, object]:
        binding = kwargs.pop("session_binding", _binding())
        return create_delete_preview(
            ALICE,
            action="forget_personal_memory",
            claim_ids=[self.claim_id],
            language="en",
            store=self.store,
            session_binding=binding,  # type: ignore[arg-type]
            empty_kind="do_not_remember_none",
            preview_kind="forget_preview",
            **kwargs,
        )

    def _bulk_preview(self) -> dict[str, object]:
        return create_delete_preview(
            ALICE,
            action="do_not_remember",
            claim_ids=[self.claim_id, self.extra_id],
            language="en",
            store=self.store,
            session_binding=_binding(),
            empty_kind="do_not_remember_none",
            preview_kind="do_not_remember_preview",
        )

    def _open(self, request_id: object, send: FakeSend | None = None) -> FakeSend:
        transport = send if send is not None else FakeSend()
        REGISTRY.open_preview(str(request_id), _binding(), send=transport)
        return transport

    # --- happy path + nonce discipline -------------------------------------

    def test_valid_delete_after_preview_send_and_confirm(self) -> None:
        preview = self._preview()
        request_id = str(preview["request_id"])
        self.assertTrue(str(preview["delete_command"]).endswith(request_id))
        self.assertNotIn("nonce", preview)
        self.assertNotIn('"nonce"', json.dumps(preview))
        send = self._open(request_id)
        nonce = _nonce_from_sent(send)
        self.assertNotIn(nonce, json.dumps(preview))
        record = REGISTRY.consume_confirm(nonce, _binding())
        result = consume_delete_request(record, store=self.store)
        self.assertFalse(result["preview"])
        self.assertNotIn(self.claim_id, self._ids())
        self.assertIn(self.extra_id, self._ids())

    def test_no_binding_returns_non_authorizing_preview(self) -> None:
        preview = self._preview(session_binding=None)
        self.assertIsNone(preview["request_id"])
        self.assertIsNone(preview["delete_command"])
        self.assertIn("session", str(preview["confirmation"]).lower())
        self.assertIn(self.claim_id, self._ids())

    def test_empty_match_preview_registers_nothing(self) -> None:
        preview = create_delete_preview(
            ALICE,
            action="do_not_remember",
            claim_ids=[],
            language="en",
            store=self.store,
            session_binding=_binding(),
            empty_kind="do_not_remember_none",
            preview_kind="do_not_remember_preview",
        )
        self.assertEqual(preview["matched_claim_ids"], [])
        self.assertNotIn("request_id", preview)
        self.assertIn(self.claim_id, self._ids())

    def test_incomplete_binding_denied(self) -> None:
        for overrides in (
            {"session_key": ""},
            {"session_id": ""},
            {"chat_id": ""},
            {"platform": ""},
            {"user_id": ""},
            {"user_id": "unknown"},
        ):
            with self.assertRaises(ValidationError):
                self._preview(session_binding=_binding(**overrides))
        self.assertIn(self.claim_id, self._ids())

    # --- denials ------------------------------------------------------------

    def test_boolean_only_deny_does_not_erase(self) -> None:
        with self.assertRaises(ValidationError):
            reject_legacy_confirm(True)
        with self.assertRaises(ValidationError):
            reject_legacy_confirm("yes")
        reject_legacy_confirm(False)
        reject_legacy_confirm(None)
        self.assertIn(self.claim_id, self._ids())

    def test_forged_nonce_denied(self) -> None:
        preview = self._preview()
        self._open(preview["request_id"])
        with self.assertRaises(TrustedDeleteDenied):
            REGISTRY.consume_confirm("forged-nonce", _binding())
        self.assertIn(self.claim_id, self._ids())

    def test_other_actor_denied(self) -> None:
        preview = self._preview()
        send = self._open(preview["request_id"])
        nonce = _nonce_from_sent(send)
        other = _binding(
            user_id="42",
            chat_id="42",
            session_key="agent:main:telegram:dm:42:42",
        )
        with self.assertRaises(TrustedDeleteDenied):
            REGISTRY.consume_confirm(nonce, other)
        self.assertIn(self.claim_id, self._ids())
        record = REGISTRY.consume_confirm(nonce, _binding())
        consume_delete_request(record, store=self.store)
        self.assertNotIn(self.claim_id, self._ids())

    def test_other_session_profile_chat_denied(self) -> None:
        preview = self._preview()
        send = self._open(preview["request_id"])
        nonce = _nonce_from_sent(send)
        for binding in (
            _binding(chat_id="99"),
            _binding(profile="other"),
            _binding(thread_id="topic-1"),
            _binding(session_key="agent:main:telegram:dm:other"),
            _binding(session_id="new-session-generation"),
        ):
            with self.assertRaises(TrustedDeleteDenied):
                REGISTRY.consume_confirm(nonce, binding)
        self.assertIn(self.claim_id, self._ids())
        record = REGISTRY.consume_confirm(nonce, _binding())
        consume_delete_request(record, store=self.store)
        self.assertNotIn(self.claim_id, self._ids())

    def test_target_content_changed_denies_batch(self) -> None:
        preview = self._bulk_preview()
        self.assertEqual(
            set(preview["matched_claim_ids"]), {self.claim_id, self.extra_id}
        )
        confirmation = self.store.register_source(
            ALICE,
            source_kind="user_confirmation",
            external_ref="ref-changed",
            content_hash=content_hash_for(b"changed"),
            snapshot=b"changed",
            observed_at=NOW,
            retention_class="standard",
            metadata={"title": "changed"},
        )
        self.store.correct_claim(
            ALICE,
            self.claim_id,
            content=b"no longer peanuts",
            explanation="changed",
            source_ids=[str(confirmation["source_id"])],
        )
        send = self._open(preview["request_id"])
        nonce = _nonce_from_sent(send)
        record = REGISTRY.consume_confirm(nonce, _binding())
        with self.assertRaises(ConflictError):
            consume_delete_request(record, store=self.store)
        ids = self._ids()
        self.assertIn(self.claim_id, ids)
        self.assertIn(self.extra_id, ids)

    def test_fixed_bulk_set_does_not_search_again(self) -> None:
        preview = self._bulk_preview()
        bound = set(preview["matched_claim_ids"])
        later = self.store.create_candidate(
            ALICE,
            claim_kind="preference",
            content=b"new peanut fact",
            confidence=0.5,
            explanation="",
            sensitivity="normal",
            source_ids=[self.source_id],
            valid_from="2026-08-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
            expires_at=EXPIRES,
        )
        later_id = str(later["claim_id"])
        send = self._open(preview["request_id"])
        nonce = _nonce_from_sent(send)
        record = REGISTRY.consume_confirm(nonce, _binding())
        consume_delete_request(record, store=self.store)
        ids = self._ids()
        self.assertTrue(bound.isdisjoint(ids))
        self.assertIn(later_id, ids)

    def test_expired_request_denied(self) -> None:
        clock = {"now": 10.0}
        registry = DeleteRegistry(monotonic=lambda: clock["now"])
        record = registry.create_request(
            action="forget_personal_memory",
            actor=ALICE,
            binding=_binding(),
            targets=[_dummy_target()],
            language="en",
        )
        send = FakeSend()
        registry.open_preview(record.request_id, _binding(), send=send)
        nonce = _nonce_from_sent(send)
        clock["now"] = 10.0 + 601.0
        with self.assertRaises(TrustedDeleteDenied):
            registry.consume_confirm(nonce, _binding())
        self.assertIn(self.claim_id, self._ids())

    def test_replay_denied(self) -> None:
        preview = self._preview()
        send = self._open(preview["request_id"])
        nonce = _nonce_from_sent(send)
        record = REGISTRY.consume_confirm(nonce, _binding())
        consume_delete_request(record, store=self.store)
        self.assertNotIn(self.claim_id, self._ids())
        extra_before = self.extra_id in self._ids()
        with self.assertRaises(TrustedDeleteDenied):
            REGISTRY.consume_confirm(nonce, _binding())
        self.assertEqual(self.extra_id in self._ids(), extra_before)

    def test_concurrent_confirmations_one_winner(self) -> None:
        preview = self._preview()
        send = self._open(preview["request_id"])
        nonce = _nonce_from_sent(send)
        outcomes: list[object] = []

        def _confirm() -> None:
            try:
                outcomes.append(REGISTRY.consume_confirm(nonce, _binding()))
            except TrustedDeleteDenied as exc:
                outcomes.append(exc)

        workers = [threading.Thread(target=_confirm) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        winners = [item for item in outcomes if isinstance(item, DeleteRequest)]
        self.assertEqual(len(winners), 1)
        consume_delete_request(winners[0], store=self.store)
        self.assertNotIn(self.claim_id, self._ids())

    # --- send-failure discipline ---------------------------------------------

    def test_failed_send_does_not_enable_confirm(self) -> None:
        preview = self._preview()
        request_id = str(preview["request_id"])
        with self.assertRaises(TrustedDeleteDenied):
            REGISTRY.open_preview(request_id, _binding(), send=FakeSend(fail=True))
        record = REGISTRY.peek(request_id)
        self.assertIsNotNone(record)
        self.assertFalse(record.preview_sent)
        self.assertIsNone(record.nonce)
        with self.assertRaises(TrustedDeleteDenied):
            REGISTRY.consume_confirm("leftover", _binding())
        self.assertIn(self.claim_id, self._ids())

    def test_failed_sendresult_does_not_enable_confirm(self) -> None:
        preview = self._preview()
        request_id = str(preview["request_id"])
        send = FakeSend(delivered=False)
        with self.assertRaises(TrustedDeleteDenied):
            REGISTRY.open_preview(request_id, _binding(), send=send)
        self.assertTrue(send.sent)
        record = REGISTRY.peek(request_id)
        self.assertIsNotNone(record)
        self.assertFalse(record.preview_sent)
        self.assertIsNone(record.nonce)
        with self.assertRaises(TrustedDeleteDenied):
            REGISTRY.consume_confirm("leftover", _binding())
        self.assertIn(self.claim_id, self._ids())

    def test_async_send_coroutine_denied(self) -> None:
        preview = self._preview()
        request_id = str(preview["request_id"])

        async def _send(text: str) -> bool:
            del text
            return True

        with self.assertRaises(TrustedDeleteDenied):
            REGISTRY.open_preview(request_id, _binding(), send=_send)
        record = REGISTRY.peek(request_id)
        self.assertIsNotNone(record)
        self.assertFalse(record.preview_sent)
        self.assertIsNone(record.nonce)
        self.assertIn(self.claim_id, self._ids())

    def test_async_send_coroutine_on_running_loop_denied(self) -> None:
        preview = self._preview()
        request_id = str(preview["request_id"])

        async def _run() -> None:
            async def _send(text: str) -> bool:
                del text
                return True

            with self.assertRaises(TrustedDeleteDenied):
                REGISTRY.open_preview(request_id, _binding(), send=_send)

        asyncio.run(_run())
        record = REGISTRY.peek(request_id)
        self.assertIsNotNone(record)
        self.assertFalse(record.preview_sent)
        self.assertIn(self.claim_id, self._ids())

    # --- cancel / registry bounds / explicit store ---------------------------

    def test_cancel_denies_confirm(self) -> None:
        preview = self._preview()
        request_id = str(preview["request_id"])
        send = self._open(request_id)
        nonce = _nonce_from_sent(send)
        REGISTRY.cancel(request_id, _binding())
        with self.assertRaises(TrustedDeleteDenied):
            REGISTRY.consume_confirm(nonce, _binding())
        self.assertIn(self.claim_id, self._ids())

    def test_cancel_by_nonce_denies_confirm(self) -> None:
        preview = self._preview()
        send = self._open(preview["request_id"])
        nonce = _nonce_from_sent(send)
        REGISTRY.cancel(nonce, _binding())
        with self.assertRaises(TrustedDeleteDenied):
            REGISTRY.consume_confirm(nonce, _binding())
        self.assertIn(self.claim_id, self._ids())

    def test_registry_full_denied(self) -> None:
        registry = DeleteRegistry(max_items=1)
        registry.create_request(
            action="forget_personal_memory",
            actor=ALICE,
            binding=_binding(),
            targets=[_dummy_target()],
            language="en",
        )
        with self.assertRaises(ValidationError):
            registry.create_request(
                action="forget_personal_memory",
                actor=ALICE,
                binding=_binding(),
                targets=[_dummy_target()],
                language="en",
            )

    def test_consume_requires_explicit_store(self) -> None:
        preview = self._preview()
        send = self._open(preview["request_id"])
        nonce = _nonce_from_sent(send)
        record = REGISTRY.consume_confirm(nonce, _binding())
        with self.assertRaises(TypeError):
            consume_delete_request(record)  # type: ignore[call-arg]
        self.assertIn(self.claim_id, self._ids())

    def test_create_requires_explicit_store(self) -> None:
        with self.assertRaises(TypeError):
            create_delete_preview(  # type: ignore[call-arg]
                ALICE,
                action="forget_personal_memory",
                claim_ids=[self.claim_id],
                language="en",
                session_binding=_binding(),
                empty_kind="do_not_remember_none",
                preview_kind="forget_preview",
            )
        self.assertIn(self.claim_id, self._ids())

    def test_consume_against_different_store_denied(self) -> None:
        preview = self._preview()
        send = self._open(preview["request_id"])
        nonce = _nonce_from_sent(send)
        record = REGISTRY.consume_confirm(nonce, _binding())
        with tempfile.TemporaryDirectory(prefix="pm-confirm-other-") as other_dir:
            other_store = PersonalMemoryStore(data_dir=Path(other_dir))
            with self.assertRaises(ConflictError):
                consume_delete_request(record, store=other_store)
        self.assertIn(self.claim_id, self._ids())


if __name__ == "__main__":
    unittest.main()

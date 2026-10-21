"""Issue #3: change_reason vocabulary, retracted state, export links (T5-REASON).

T6-HISTORY extends this file with the personal_memory_history read path; the
T5 members below pin S-REASON-COMPAT: per-step change_reason, the retracted
terminal version, pending-link rejection on retract, export reasons/links and
forget-after-retract/K2 erasure.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.assembler import assemble_context
from agent_personal_memory.confirmation import (
    REGISTRY,
    SessionBinding,
    consume_delete_request,
    reset_registry,
)
from agent_personal_memory.export import explain_claim, export_owner_data
from agent_personal_memory.fts_index import (
    build_claim_fts_index,
    rank_claims_fts,
)
from agent_personal_memory.history import personal_memory_history
from agent_personal_memory.learn import remember_personal_insight
from agent_personal_memory.lifecycle import MemoryState
from agent_personal_memory.mutations import (
    forget_personal_memory,
    retract_personal_memory,
)
from agent_personal_memory.query import get_authorized_claim, query_claims
from agent_personal_memory.ranking import rank_claims
from agent_personal_memory.reconcile import (
    accept_memory_reconciliation,
    list_memory_reconciliations,
    propose_reconciliation,
)
from agent_personal_memory.store import NotFoundError, PersonalMemoryStore
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import ValidationError, content_hash_for
from tests.test_schema_v5 import _downgrade_to_v4

TENANT_A = Actor("test", "user-a")
TENANT_B = Actor("test", "user-b")
KAZAN = "я живу в Казани"
PERM_NEG = "я переехал в Пермь, больше не живу в Казани"
F4 = "я не ем мясо"
CAT = "у меня есть кот"
NOW = "2026-08-31T10:00:00Z"
EXPIRES = "2099-01-01T00:00:00Z"


def _source(store: PersonalMemoryStore, actor: Actor, ref: str) -> dict:
    return store.register_source(
        actor,
        source_kind="document",
        external_ref=ref,
        content_hash=content_hash_for(ref.encode("utf-8")),
        snapshot=ref.encode("utf-8"),
        observed_at=NOW,
        retention_class="standard",
    )


def _candidate(
    store: PersonalMemoryStore, actor: Actor, text: str, ref: str
) -> dict:
    source = _source(store, actor, ref)
    return store.create_candidate(
        actor,
        claim_kind="confirmed_fact",
        content=text.encode("utf-8"),
        confidence=0.9,
        explanation="seed",
        sensitivity="normal",
        source_ids=[str(source["source_id"])],
        expires_at=EXPIRES,
    )


def _binding() -> SessionBinding:
    return SessionBinding(
        platform="test",
        user_id="user-a",
        session_key="agent:test:dm:user-a",
        session_id="sess-a",
        chat_id="chat-a",
    )


def _forget(store: PersonalMemoryStore, actor: Actor, claim_id: str) -> dict:
    """Drive the full preview -> open -> consume confirm-gate flow."""
    preview = forget_personal_memory(
        actor, claim_id, store=store, session_binding=_binding()
    )
    request_id = str(preview["request_id"])
    sent: list[str] = []
    REGISTRY.open_preview(
        request_id, _binding(), send=lambda text: bool(sent.append(text) or True)
    )
    nonce = ""
    for line in sent[-1].splitlines():
        if "/memory_confirm" in line:
            nonce = line.split()[-1]
    if not nonce:
        raise AssertionError("nonce missing from preview")
    record = REGISTRY.consume_confirm(nonce, _binding())
    return consume_delete_request(record, store=store)


class ReasonStepTests(unittest.TestCase):
    """S-REASON-COMPAT 1: change_reason per transition step."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-reason-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _versions(self, claim_id: object) -> list[sqlite3.Row]:
        conn = self.store._connect()
        try:
            return list(
                conn.execute(
                    "SELECT version, state, change_reason "
                    "FROM memory_claim_versions "
                    "WHERE claim_id = ? ORDER BY version",
                    (str(claim_id),),
                )
            )
        finally:
            conn.close()

    def test_create_confirm_correct_reasons(self) -> None:
        source = _source(self.store, TENANT_A, "reason-src")
        created = self.store.create_candidate(
            TENANT_A,
            claim_kind="confirmed_fact",
            content=F4.encode("utf-8"),
            confidence=0.9,
            explanation="seed",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            expires_at=EXPIRES,
        )
        claim_id = str(created["claim_id"])
        confirmed = self.store.confirm_claim(
            TENANT_A, claim_id, reliable_source_id=str(source["source_id"])
        )
        self.assertEqual(confirmed["state"], "confirmed")
        corrected = self.store.correct_claim(
            TENANT_A,
            claim_id,
            content=b"ya ne em myaso segodnya",
            explanation="owner fix",
            source_ids=[str(source["source_id"])],
        )
        self.assertEqual(corrected["version"], 3)
        rows = self._versions(claim_id)
        self.assertEqual(
            [row["change_reason"] for row in rows],
            ["created", "promoted", "changed"],
        )

    def test_accept_reasons(self) -> None:
        k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        k2 = remember_personal_insight(TENANT_A, PERM_NEG, store=self.store)
        link = k2["reconciliations"][0]
        accept_memory_reconciliation(
            TENANT_A, link["link_id"], store=self.store
        )
        k2_rows = self._versions(k2["claim_id"])
        k1_rows = self._versions(k1["claim_id"])
        self.assertEqual(k2_rows[-1]["state"], "confirmed")
        self.assertEqual(k2_rows[-1]["change_reason"], "promoted")
        self.assertEqual(k1_rows[-1]["state"], "superseded")
        self.assertEqual(k1_rows[-1]["change_reason"], "changed")

    def test_retract_reason(self) -> None:
        created = remember_personal_insight(TENANT_A, F4, store=self.store)
        retract_personal_memory(
            TENANT_A, str(created["claim_id"]), store=self.store
        )
        rows = self._versions(created["claim_id"])
        self.assertEqual(rows[-1]["state"], "retracted")
        self.assertEqual(rows[-1]["change_reason"], "retracted")
        self.assertEqual(rows[-1]["version"], rows[-2]["version"] + 1)

    def test_v5_versions_report_reasons_not_inferred(self) -> None:
        remember_personal_insight(TENANT_A, F4, store=self.store)
        payload = json.loads(
            export_owner_data(TENANT_A, store=self.store).decode("utf-8")
        )
        versions = [
            item
            for claim in payload["claims"]
            for item in claim["lifecycle"]
        ]
        self.assertTrue(versions)
        for item in versions:
            self.assertIsNotNone(item["change_reason"])
            self.assertIs(item["change_reason_inferred"], False)

    def test_insert_claim_version_rejects_unknown_reason(self) -> None:
        created = _candidate(self.store, TENANT_A, F4, "reason-guard")
        tenant_id = self.store.ensure_principal(TENANT_A)
        with self.assertRaises(ValueError):
            with self.store._tx() as conn:
                self.store._insert_claim_version(
                    conn,
                    tenant_id=tenant_id,
                    claim_id=str(created["claim_id"]),
                    version=9,
                    state=MemoryState.CANDIDATE,
                    sensitivity="normal",
                    content=b"x",
                    confidence=0.5,
                    explanation="",
                    valid_from=None,
                    valid_to=None,
                    expires_at=None,
                    supersedes_id=None,
                    now=NOW,
                    change_reason="bogus",
                )


class RetractReadTests(unittest.TestCase):
    """S-REASON-COMPAT 2: retracted leaves every default read."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-retract-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))
        self.f4 = remember_personal_insight(TENANT_A, F4, store=self.store)
        self.f4_id = str(self.f4["claim_id"])
        remember_personal_insight(TENANT_A, CAT, store=self.store)
        reset_registry()

    def tearDown(self) -> None:
        reset_registry()
        self.tmp.cleanup()

    def test_retract_excludes_from_default_reads(self) -> None:
        build_claim_fts_index(TENANT_A, store=self.store)
        result = retract_personal_memory(
            TENANT_A, self.f4_id, store=self.store
        )
        self.assertEqual(result["state"], "retracted")
        self.assertEqual(result["claim_id"], self.f4_id)
        self.assertEqual(int(result["version"]), int(self.f4["version"]) + 1)
        self.assertIsInstance(result["confirmation"], str)
        row = get_authorized_claim(TENANT_A, self.f4_id, store=self.store)
        self.assertEqual(row["state"], "retracted")
        default_ids = {
            str(item["claim_id"])
            for item in query_claims(TENANT_A, store=self.store)
        }
        self.assertNotIn(self.f4_id, default_ids)
        ranked_ids = {
            str(item["claim_id"])
            for item in rank_claims(TENANT_A, "мясо", store=self.store)
        }
        self.assertNotIn(self.f4_id, ranked_ids)
        fts_ids = {
            str(item["claim_id"])
            for item in rank_claims_fts(TENANT_A, "мясо", store=self.store)
        }
        self.assertNotIn(self.f4_id, fts_ids)
        active_ids = {
            str(item["claim_id"])
            for item in self.store.list_active_claims(TENANT_A)
        }
        self.assertNotIn(self.f4_id, active_ids)
        assembled = assemble_context(TENANT_A, memory_store=self.store)
        assembled_ids = {
            str(item["claim_id"]) for item in assembled["personal"]["items"]
        }
        self.assertNotIn(self.f4_id, assembled_ids)
        explicit = query_claims(
            TENANT_A, states=["retracted"], store=self.store
        )
        self.assertEqual(
            [str(item["claim_id"]) for item in explicit], [self.f4_id]
        )

    def test_retract_then_forget_erases_all_versions(self) -> None:
        retract_personal_memory(TENANT_A, self.f4_id, store=self.store)
        result = _forget(self.store, TENANT_A, self.f4_id)
        self.assertIn(self.f4_id, result["erased_claim_ids"])
        conn = self.store._connect()
        try:
            rows = list(
                conn.execute(
                    "SELECT state, content_enc, explanation_enc "
                    "FROM memory_claim_versions WHERE claim_id = ?",
                    (self.f4_id,),
                )
            )
            keys = list(
                conn.execute(
                    "SELECT wrapped_dek FROM memory_keys "
                    "WHERE entity_kind = 'claim' AND entity_id = ?",
                    (self.f4_id,),
                )
            )
        finally:
            conn.close()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["state"], "deleted")
            self.assertIsNone(row["content_enc"])
            self.assertIsNone(row["explanation_enc"])
        self.assertEqual(keys, [])
        with self.assertRaises(NotFoundError):
            get_authorized_claim(TENANT_A, self.f4_id, store=self.store)


class RetractErrorTests(unittest.TestCase):
    """S-REASON-COMPAT: retract denials register no orphan source."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-retract-err-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _source_count(self) -> int:
        conn = self.store._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM memory_sources").fetchone()
            return int(row[0])
        finally:
            conn.close()

    def _retract_rejections(self) -> int:
        conn = self.store._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_audit "
                "WHERE action = 'claim.retract' AND outcome = 'rejected' "
                "AND policy_reason = 'invalid_transition'"
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()

    def test_missing_and_foreign_are_byte_identical_no_source(self) -> None:
        created = remember_personal_insight(TENANT_A, F4, store=self.store)
        claim_id = str(created["claim_id"])
        before = self._source_count()
        missing = "00000000-0000-4000-8000-000000000000"
        with self.assertRaises(NotFoundError) as not_found:
            retract_personal_memory(TENANT_A, missing, store=self.store)
        with self.assertRaises(NotFoundError) as foreign:
            retract_personal_memory(TENANT_B, claim_id, store=self.store)
        self.assertEqual(str(not_found.exception), "Claim not found")
        self.assertEqual(str(foreign.exception), "Claim not found")
        self.assertEqual(repr(not_found.exception), repr(foreign.exception))
        self.assertEqual(self._source_count(), before)

    def test_retract_of_retracted_is_invalid_no_source(self) -> None:
        created = remember_personal_insight(TENANT_A, F4, store=self.store)
        claim_id = str(created["claim_id"])
        retract_personal_memory(TENANT_A, claim_id, store=self.store)
        before = self._source_count()
        before_audit = self._retract_rejections()
        with self.assertRaises(ValidationError) as exc:
            retract_personal_memory(TENANT_A, claim_id, store=self.store)
        self.assertEqual(str(exc.exception), "claim: invalid transition")
        self.assertEqual(self._source_count(), before)
        self.assertEqual(self._retract_rejections() - before_audit, 1)

    def test_retract_of_superseded_is_invalid_no_source(self) -> None:
        k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        k2 = remember_personal_insight(TENANT_A, PERM_NEG, store=self.store)
        link = k2["reconciliations"][0]
        accept_memory_reconciliation(
            TENANT_A, link["link_id"], store=self.store
        )
        before = self._source_count()
        with self.assertRaises(ValidationError) as exc:
            retract_personal_memory(
                TENANT_A, str(k1["claim_id"]), store=self.store
            )
        self.assertEqual(str(exc.exception), "claim: invalid transition")
        self.assertEqual(self._source_count(), before)

    def test_store_call_validates_source_ids(self) -> None:
        created = remember_personal_insight(TENANT_A, F4, store=self.store)
        claim_id = str(created["claim_id"])
        with self.assertRaises(ValidationError):
            self.store.retract_claim(TENANT_A, claim_id, source_ids=[])
        with self.assertRaises(ValidationError):
            self.store.retract_claim(
                TENANT_A, claim_id, source_ids=["not-a-source"]
            )
        row = get_authorized_claim(TENANT_A, claim_id, store=self.store)
        self.assertEqual(row["state"], "confirmed")


class RetractLinkTests(unittest.TestCase):
    """S-REASON-COMPAT: retract rejects pending links on either side."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-retract-link-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _link_row(self, link_id: object) -> sqlite3.Row:
        conn = self.store._connect()
        try:
            return conn.execute(
                "SELECT status, resolved_at FROM memory_claim_links WHERE id = ?",
                (str(link_id),),
            ).fetchone()
        finally:
            conn.close()

    def _pending_ids(self) -> set[str]:
        listed = list_memory_reconciliations(TENANT_A, store=self.store)
        return {str(item["link_id"]) for item in listed["items"]}

    def test_retract_new_side_rejects_pending_link(self) -> None:
        k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        k2 = _candidate(self.store, TENANT_A, PERM_NEG, "link-new")
        link = propose_reconciliation(
            TENANT_A,
            str(k2["claim_id"]),
            str(k1["claim_id"]),
            store=self.store,
        )
        self.assertIn(str(link["link_id"]), self._pending_ids())
        retract_personal_memory(
            TENANT_A, str(k2["claim_id"]), store=self.store
        )
        row = self._link_row(link["link_id"])
        self.assertEqual(row["status"], "rejected")
        self.assertIsNotNone(row["resolved_at"])
        self.assertNotIn(str(link["link_id"]), self._pending_ids())

    def test_retract_old_side_rejects_pending_link(self) -> None:
        k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        k2 = _candidate(self.store, TENANT_A, PERM_NEG, "link-old")
        link = propose_reconciliation(
            TENANT_A,
            str(k2["claim_id"]),
            str(k1["claim_id"]),
            store=self.store,
        )
        retract_personal_memory(
            TENANT_A, str(k1["claim_id"]), store=self.store
        )
        row = self._link_row(link["link_id"])
        self.assertEqual(row["status"], "rejected")
        self.assertIsNotNone(row["resolved_at"])
        self.assertNotIn(str(link["link_id"]), self._pending_ids())


class ExportAndErasureTests(unittest.TestCase):
    """S-REASON-COMPAT 4+5: export reasons/links, forget-after-accept."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-export-link-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))
        reset_registry()

    def tearDown(self) -> None:
        reset_registry()
        self.tmp.cleanup()

    def _accepted_pair(self) -> tuple[dict, dict, dict]:
        k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        k2 = remember_personal_insight(TENANT_A, PERM_NEG, store=self.store)
        link = k2["reconciliations"][0]
        accept_memory_reconciliation(
            TENANT_A, link["link_id"], store=self.store
        )
        return k1, k2, link

    def test_export_includes_reasons_and_links(self) -> None:
        k1, k2, link = self._accepted_pair()
        payload = json.loads(
            export_owner_data(TENANT_A, store=self.store).decode("utf-8")
        )
        versions = [
            item
            for claim in payload["claims"]
            for item in claim["lifecycle"]
        ]
        self.assertTrue(versions)
        for item in versions:
            self.assertIn("change_reason", item)
            self.assertIsNotNone(item["change_reason"])
            self.assertIn("change_reason_inferred", item)
        self.assertIn("links", payload)
        accepted = [
            item
            for item in payload["links"]
            if item["link_id"] == str(link["link_id"])
        ]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["status"], "accepted")
        self.assertEqual(accepted[0]["from_claim_id"], str(k2["claim_id"]))
        self.assertEqual(accepted[0]["to_claim_id"], str(k1["claim_id"]))

    def test_forget_k2_after_accept_keeps_k1_superseded(self) -> None:
        k1, k2, link = self._accepted_pair()
        _forget(self.store, TENANT_A, str(k2["claim_id"]))
        conn = self.store._connect()
        try:
            k2_rows = list(
                conn.execute(
                    "SELECT state FROM memory_claim_versions WHERE claim_id = ?",
                    (str(k2["claim_id"]),),
                )
            )
            links = list(conn.execute("SELECT id FROM memory_claim_links"))
        finally:
            conn.close()
        self.assertTrue(k2_rows)
        for row in k2_rows:
            self.assertEqual(row["state"], "deleted")
        self.assertEqual(links, [])
        kept = get_authorized_claim(
            TENANT_A, str(k1["claim_id"]), store=self.store
        )
        self.assertEqual(kept["state"], "superseded")


class HistoryChainTests(unittest.TestCase):
    """S-HISTORY 1: candidate -> confirm -> correct history trail."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-history-chain-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))
        source = _source(self.store, TENANT_A, "hist-chain-src")
        self.source_id = str(source["source_id"])
        created = self.store.create_candidate(
            TENANT_A,
            claim_kind="confirmed_fact",
            content="я предпочитаю чай".encode("utf-8"),
            confidence=0.9,
            explanation="seed",
            sensitivity="normal",
            source_ids=[self.source_id],
            expires_at=EXPIRES,
        )
        self.claim_id = str(created["claim_id"])
        self.store.confirm_claim(
            TENANT_A, self.claim_id, reliable_source_id=self.source_id
        )
        self.store.correct_claim(
            TENANT_A,
            self.claim_id,
            content="я предпочитаю кофе".encode("utf-8"),
            explanation="owner fix",
            source_ids=[self.source_id],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_collapsed_history_two_entries(self) -> None:
        result = personal_memory_history(
            TENANT_A, self.claim_id, store=self.store
        )
        self.assertEqual(result["claim_id"], self.claim_id)
        self.assertEqual(result["current_claim_id"], self.claim_id)
        entries = result["entries"]
        self.assertEqual(
            [item["reason"] for item in entries], ["created", "changed"]
        )
        self.assertEqual([item["version"] for item in entries], [1, 3])
        self.assertEqual(
            [item["content"] for item in entries],
            ["я предпочитаю чай", "я предпочитаю кофе"],
        )
        self.assertIsNotNone(entries[0]["promoted_at"])
        self.assertIsNone(entries[0]["linked_claim_id"])
        self.assertIsNone(entries[1]["promoted_at"])
        self.assertIsNone(entries[1]["linked_claim_id"])
        self.assertEqual(entries[1]["state"], "confirmed")
        ats = [item["at"] for item in entries]
        self.assertEqual(ats, sorted(ats))
        self.assertTrue(all(item["inferred"] is False for item in entries))

    def test_uncollapsed_history_three_entries(self) -> None:
        result = personal_memory_history(
            TENANT_A,
            self.claim_id,
            collapse_promoted=False,
            store=self.store,
        )
        entries = result["entries"]
        self.assertEqual(
            [item["reason"] for item in entries],
            ["created", "promoted", "changed"],
        )
        # The (at, claim_id, version) sort pins version order on ties.
        self.assertEqual([item["version"] for item in entries], [1, 2, 3])
        self.assertEqual(
            [item["content"] for item in entries],
            ["я предпочитаю чай", "я предпочитаю чай", "я предпочитаю кофе"],
        )
        self.assertEqual(
            [item["state"] for item in entries],
            ["superseded", "superseded", "confirmed"],
        )
        for item in entries:
            self.assertIsNone(item["promoted_at"])
            self.assertIsNone(item["linked_claim_id"])

    def test_query_mode_selects_the_claim_deterministically(self) -> None:
        _candidate(self.store, TENANT_A, CAT, "hist-chain-cat")
        first = personal_memory_history(
            TENANT_A, query="кофе", store=self.store
        )
        second = personal_memory_history(
            TENANT_A, query="кофе", store=self.store
        )
        self.assertEqual(first["claim_id"], self.claim_id)
        self.assertEqual(second["claim_id"], self.claim_id)

    def test_query_mode_no_match_is_not_found(self) -> None:
        with self.assertRaises(NotFoundError) as exc:
            personal_memory_history(
                TENANT_A, query="несуществующееслово", store=self.store
            )
        self.assertEqual(str(exc.exception), "Claim not found")


class HistoryLinkTests(unittest.TestCase):
    """S-HISTORY 2 + 8: accepted-link traversal in both directions."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-history-link-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))
        self.k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        self.k2 = remember_personal_insight(
            TENANT_A, PERM_NEG, store=self.store
        )
        self.k1_id = str(self.k1["claim_id"])
        self.k2_id = str(self.k2["claim_id"])
        link = self.k2["reconciliations"][0]
        accept_memory_reconciliation(
            TENANT_A, link["link_id"], store=self.store
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_history_from_either_side_is_identical(self) -> None:
        from_k1 = personal_memory_history(
            TENANT_A, self.k1_id, store=self.store
        )
        from_k2 = personal_memory_history(
            TENANT_A, self.k2_id, store=self.store
        )
        self.assertEqual(from_k1["entries"], from_k2["entries"])
        self.assertEqual(from_k1["current_claim_id"], self.k2_id)
        self.assertEqual(from_k2["current_claim_id"], self.k2_id)
        claim_ids = {item["claim_id"] for item in from_k1["entries"]}
        self.assertEqual(claim_ids, {self.k1_id, self.k2_id})
        supersede = [
            item
            for item in from_k1["entries"]
            if item["claim_id"] == self.k1_id and item["reason"] == "changed"
        ]
        self.assertEqual(len(supersede), 1)
        self.assertEqual(supersede[0]["linked_claim_id"], self.k2_id)

    def test_restricted_linked_claim_disappears(self) -> None:
        source = _source(self.store, TENANT_A, "hist-restrict-src")
        self.store.correct_claim(
            TENANT_A,
            self.k2_id,
            content="секретный адрес".encode("utf-8"),
            explanation="hide",
            source_ids=[str(source["source_id"])],
            sensitivity="restricted",
        )
        result = personal_memory_history(
            TENANT_A, self.k1_id, store=self.store
        )
        self.assertEqual(result["current_claim_id"], self.k1_id)
        self.assertTrue(
            all(item["claim_id"] != self.k2_id for item in result["entries"])
        )
        self.assertTrue(
            any(item["claim_id"] == self.k1_id for item in result["entries"])
        )
        self.assertTrue(
            all(
                item["linked_claim_id"] is None
                for item in result["entries"]
            )
        )

    def test_expired_linked_claim_disappears(self) -> None:
        conn = self.store._connect()
        try:
            conn.execute(
                "UPDATE memory_claim_versions SET expires_at = ? "
                "WHERE claim_id = ?",
                ("2020-01-01T00:00:00Z", self.k2_id),
            )
            conn.commit()
        finally:
            conn.close()
        result = personal_memory_history(
            TENANT_A, self.k1_id, store=self.store
        )
        self.assertEqual(result["current_claim_id"], self.k1_id)
        self.assertTrue(
            all(item["claim_id"] != self.k2_id for item in result["entries"])
        )
        self.assertTrue(
            any(item["claim_id"] == self.k1_id for item in result["entries"])
        )
        self.assertTrue(
            all(
                item["linked_claim_id"] is None
                for item in result["entries"]
            )
        )


class HistoryV4Tests(unittest.TestCase):
    """S-HISTORY 4: inferred reasons on a v4-downgraded store."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-history-v4-")
        self.data_dir = Path(self.tmp.name)
        self.db_path = self.data_dir / "personal_memory.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_downgraded_store_reports_inferred_reasons(self) -> None:
        store = PersonalMemoryStore(data_dir=self.data_dir)
        source = _source(store, TENANT_A, "hist-v4-src")
        chain = store.create_candidate(
            TENANT_A,
            claim_kind="confirmed_fact",
            content="я предпочитаю чай".encode("utf-8"),
            confidence=0.9,
            explanation="seed",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            expires_at=EXPIRES,
        )
        chain_id = str(chain["claim_id"])
        store.confirm_claim(
            TENANT_A, chain_id, reliable_source_id=str(source["source_id"])
        )
        store.correct_claim(
            TENANT_A,
            chain_id,
            content="я предпочитаю кофе".encode("utf-8"),
            explanation="owner fix",
            source_ids=[str(source["source_id"])],
        )
        _downgrade_to_v4(self.db_path)
        reopened = PersonalMemoryStore(data_dir=self.data_dir)
        result = personal_memory_history(
            TENANT_A,
            chain_id,
            collapse_promoted=False,
            store=reopened,
        )
        self.assertEqual(
            [item["reason"] for item in result["entries"]],
            ["created", "promoted", "changed"],
        )
        self.assertTrue(all(item["inferred"] for item in result["entries"]))

    def test_retracted_version_reason_is_unknown_after_downgrade(self) -> None:
        store = PersonalMemoryStore(data_dir=self.data_dir)
        created = remember_personal_insight(TENANT_A, F4, store=store)
        claim_id = str(created["claim_id"])
        retract_personal_memory(TENANT_A, claim_id, store=store)
        _downgrade_to_v4(self.db_path)
        reopened = PersonalMemoryStore(data_dir=self.data_dir)
        result = personal_memory_history(
            TENANT_A, claim_id, store=reopened
        )
        last = result["entries"][-1]
        self.assertEqual(last["state"], "retracted")
        self.assertEqual(last["reason"], "unknown")
        self.assertIs(last["inferred"], True)


class HistoryVisibilityTests(unittest.TestCase):
    """S-HISTORY 5+6: explain history key, denials, active reads."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-history-vis-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))
        self.k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        self.k2 = remember_personal_insight(
            TENANT_A, PERM_NEG, store=self.store
        )
        self.k1_id = str(self.k1["claim_id"])
        self.k2_id = str(self.k2["claim_id"])
        link = self.k2["reconciliations"][0]
        accept_memory_reconciliation(
            TENANT_A, link["link_id"], store=self.store
        )
        restricted_source = _source(
            self.store, TENANT_A, "hist-restricted-src"
        )
        restricted = self.store.create_candidate(
            TENANT_A,
            claim_kind="confirmed_fact",
            content="секретный адрес".encode("utf-8"),
            confidence=0.9,
            explanation="seed",
            sensitivity="restricted",
            source_ids=[str(restricted_source["source_id"])],
            expires_at=EXPIRES,
        )
        self.restricted_id = str(restricted["claim_id"])
        expired = _candidate(self.store, TENANT_A, CAT, "hist-expired")
        self.expired_id = str(expired["claim_id"])
        conn = self.store._connect()
        try:
            conn.execute(
                "UPDATE memory_claim_versions SET expires_at = ? "
                "WHERE claim_id = ?",
                ("2020-01-01T00:00:00Z", self.expired_id),
            )
            conn.commit()
        finally:
            conn.close()
        retracted = _candidate(
            self.store, TENANT_A, "я живу в Сочи", "hist-retracted"
        )
        self.retracted_id = str(retracted["claim_id"])
        retract_personal_memory(
            TENANT_A, self.retracted_id, store=self.store
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_explain_claim_carries_history_and_superseded_state(self) -> None:
        explained = explain_claim(TENANT_A, self.k2_id, store=self.store)
        self.assertIn("history", explained)
        self.assertTrue(explained["history"])
        explained_k1 = explain_claim(TENANT_A, self.k1_id, store=self.store)
        self.assertEqual(explained_k1["state"], "superseded")
        self.assertIn("history", explained_k1)
        got = get_authorized_claim(TENANT_A, self.k1_id, store=self.store)
        self.assertEqual(got["state"], "superseded")

    def test_history_of_retracted_root_by_id(self) -> None:
        result = personal_memory_history(
            TENANT_A, self.retracted_id, store=self.store
        )
        self.assertEqual(result["claim_id"], self.retracted_id)
        self.assertEqual(result["current_claim_id"], self.retracted_id)
        last = result["entries"][-1]
        self.assertEqual(last["state"], "retracted")
        self.assertEqual(last["reason"], "retracted")

    def test_explain_history_entries_carry_no_content(self) -> None:
        explained = explain_claim(TENANT_A, self.k2_id, store=self.store)
        history = explained["history"]
        self.assertTrue(history)
        expected_keys = {
            "claim_id",
            "version",
            "at",
            "state",
            "reason",
            "inferred",
            "linked_claim_id",
            "promoted_at",
        }
        for item in history:
            self.assertNotIn("content", item)
            for key in expected_keys:
                self.assertIn(key, item)

    def test_explain_restricted_and_expired_still_deny(self) -> None:
        for claim_id in (self.restricted_id, self.expired_id):
            with self.subTest(claim_id=claim_id):
                with self.assertRaises(NotFoundError) as denied:
                    explain_claim(TENANT_A, claim_id, store=self.store)
                self.assertEqual(str(denied.exception), "Claim not found")

    def test_history_denials_are_byte_identical(self) -> None:
        missing = "00000000-0000-4000-8000-000000000000"
        with self.assertRaises(NotFoundError) as missing_exc:
            personal_memory_history(TENANT_A, missing, store=self.store)
        self.assertEqual(str(missing_exc.exception), "Claim not found")
        cases = {
            "foreign": (TENANT_B, self.k1_id),
            "restricted": (TENANT_A, self.restricted_id),
            "expired": (TENANT_A, self.expired_id),
        }
        for name, (actor, claim_id) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(NotFoundError) as denied:
                    personal_memory_history(actor, claim_id, store=self.store)
                self.assertEqual(str(denied.exception), "Claim not found")
                self.assertEqual(
                    repr(denied.exception), repr(missing_exc.exception)
                )

    def test_active_reads_exclude_superseded_and_retracted(self) -> None:
        ranked_ids = {
            str(item["claim_id"])
            for item in rank_claims(TENANT_A, "живу", store=self.store)
        }
        self.assertNotIn(self.k1_id, ranked_ids)
        self.assertNotIn(self.retracted_id, ranked_ids)
        active_ids = {
            str(item["claim_id"])
            for item in self.store.list_active_claims(TENANT_A)
        }
        self.assertNotIn(self.k1_id, active_ids)
        self.assertNotIn(self.retracted_id, active_ids)


class HistoryValidationTests(unittest.TestCase):
    """S-HISTORY 7: exactly one selector; collapse flag is bool."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-history-val-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_requires_exactly_one_selector(self) -> None:
        claim = _candidate(self.store, TENANT_A, CAT, "hist-val")
        claim_id = str(claim["claim_id"])
        with self.assertRaises(ValidationError):
            personal_memory_history(
                TENANT_A, claim_id, query="кот", store=self.store
            )
        with self.assertRaises(ValidationError):
            personal_memory_history(TENANT_A, store=self.store)
        with self.assertRaises(ValidationError):
            personal_memory_history(TENANT_A, "", store=self.store)
        with self.assertRaises(ValidationError):
            personal_memory_history(TENANT_A, query="", store=self.store)

    def test_selector_values_must_be_non_empty_strings(self) -> None:
        with self.assertRaises(ValidationError):
            personal_memory_history(
                TENANT_A, 123, query="кофе", store=self.store
            )
        with self.assertRaises(ValidationError):
            personal_memory_history(
                TENANT_A, "", query=None, store=self.store
            )
        with self.assertRaises(ValidationError):
            personal_memory_history(
                TENANT_A, None, query=5, store=self.store
            )

    def test_collapse_promoted_must_be_bool(self) -> None:
        claim = _candidate(self.store, TENANT_A, CAT, "hist-val-bool")
        with self.assertRaises(ValidationError):
            personal_memory_history(
                TENANT_A,
                str(claim["claim_id"]),
                collapse_promoted="yes",
                store=self.store,
            )


class HistoryAuditTests(unittest.TestCase):
    """S-HISTORY 9: one explained audit on success, denial on refusal."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-history-audit-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _history_audit(self) -> list[sqlite3.Row]:
        conn = self.store._connect()
        try:
            return list(
                conn.execute(
                    "SELECT action, outcome, target_kind, target_id "
                    "FROM memory_audit WHERE action = 'claim.history' "
                    "ORDER BY rowid"
                )
            )
        finally:
            conn.close()

    def test_success_appends_exactly_one_explained_row(self) -> None:
        claim = _candidate(self.store, TENANT_A, CAT, "hist-audit")
        claim_id = str(claim["claim_id"])
        before = self._history_audit()
        personal_memory_history(TENANT_A, claim_id, store=self.store)
        after = self._history_audit()
        self.assertEqual(len(after) - len(before), 1)
        row = after[-1]
        self.assertEqual(row["outcome"], "explained")
        self.assertEqual(row["target_kind"], "claim")
        self.assertEqual(row["target_id"], claim_id)

    def test_denial_appends_denied_row_and_no_explained(self) -> None:
        _candidate(self.store, TENANT_A, CAT, "hist-audit-deny")
        before = self._history_audit()
        with self.assertRaises(NotFoundError):
            personal_memory_history(
                TENANT_A,
                "00000000-0000-4000-8000-000000000000",
                store=self.store,
            )
        after = self._history_audit()
        self.assertEqual(len(after) - len(before), 1)
        self.assertEqual(after[-1]["outcome"], "denied")
        self.assertFalse(
            any(row["outcome"] == "explained" for row in after[len(before):])
        )


if __name__ == "__main__":
    unittest.main()

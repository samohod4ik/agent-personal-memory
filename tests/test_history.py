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
from agent_personal_memory.export import export_owner_data
from agent_personal_memory.fts_index import (
    build_claim_fts_index,
    rank_claims_fts,
)
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
        with self.assertRaises(ValidationError) as exc:
            retract_personal_memory(TENANT_A, claim_id, store=self.store)
        self.assertEqual(str(exc.exception), "claim: invalid transition")
        self.assertEqual(self._source_count(), before)

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


if __name__ == "__main__":
    unittest.main()

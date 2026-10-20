"""Issue #2: slot-rule reconciliation proposals and resolution (T3+T4)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from agent_personal_memory.fts_index import (
    build_claim_fts_index,
    rank_claims_fts,
)
from agent_personal_memory.learn import learn_from_turn, remember_personal_insight
from agent_personal_memory.mutations import correct_personal_memory
from agent_personal_memory.query import get_authorized_claim, query_claims
from agent_personal_memory.ranking import rank_claims
from agent_personal_memory.reconcile import (
    DEFAULT_JUDGE,
    Relation,
    SlotRuleJudge,
    accept_memory_reconciliation,
    list_memory_reconciliations,
    propose_reconciliation,
    propose_reconciliations,
    reject_memory_reconciliation,
)
from agent_personal_memory.review import confirm_candidate_memory
from agent_personal_memory.store import (
    ConflictError,
    NotFoundError,
    PersonalMemoryStore,
)
from agent_personal_memory.text_norm import stem_text
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import (
    MEMORY_MODE_CONFIRM_REQUIRED,
    ValidationError,
    content_hash_for,
)

TENANT_A = Actor("test", "user-a")
TENANT_B = Actor("test", "user-b")
KAZAN = "я живу в Казани"
PERM = "я переехал в Пермь"
PERM_NEG = "я переехал в Пермь, больше не живу в Казани"


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return ""


def _source(store: PersonalMemoryStore, actor: Actor, ref: str) -> dict:
    return store.register_source(
        actor,
        source_kind="document",
        external_ref=ref,
        content_hash=content_hash_for(ref.encode("utf-8")),
        snapshot=ref.encode("utf-8"),
        observed_at="2026-08-31T10:00:00Z",
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
        expires_at="2099-01-01T00:00:00Z",
    )


def _confirmed(
    store: PersonalMemoryStore, actor: Actor, text: str, ref: str
) -> dict:
    source = _source(store, actor, ref)
    created = store.create_candidate(
        actor,
        claim_kind="confirmed_fact",
        content=text.encode("utf-8"),
        confidence=0.9,
        explanation="seed",
        sensitivity="normal",
        source_ids=[str(source["source_id"])],
        expires_at="2099-01-01T00:00:00Z",
    )
    return store.confirm_claim(
        actor,
        str(created["claim_id"]),
        reliable_source_id=str(source["source_id"]),
    )


def _restricted(
    store: PersonalMemoryStore, actor: Actor, text: str, ref: str
) -> dict:
    source = _source(store, actor, ref)
    return store.create_candidate(
        actor,
        claim_kind="confirmed_fact",
        content=text.encode("utf-8"),
        confidence=0.9,
        explanation="seed",
        sensitivity="restricted",
        source_ids=[str(source["source_id"])],
        expires_at="2099-01-01T00:00:00Z",
    )


class ReconProposeTests(unittest.TestCase):
    """S-RECON-PROPOSE: proposals change the auto_insight write path."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-recon-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_auto_insight_pending_link_keeps_new_claim_candidate(self) -> None:
        k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        self.assertEqual(k1["state"], "confirmed")
        self.assertEqual(k1["reconciliations"], [])
        k2 = remember_personal_insight(TENANT_A, PERM_NEG, store=self.store)
        self.assertEqual(k2["state"], "candidate")
        links = k2["reconciliations"]
        self.assertEqual(len(links), 1)
        link = links[0]
        self.assertIsInstance(link["link_id"], str)
        self.assertTrue(link["link_id"])
        self.assertEqual(link["new_claim_id"], k2["claim_id"])
        self.assertEqual(link["old_claim_id"], k1["claim_id"])
        self.assertEqual(link["relation"], "supersedes")
        self.assertEqual(link["status"], "pending")
        self.assertEqual(link["origin"], "slot_rule.v1")
        listed = self.store.list_claim_links(TENANT_A)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["link_id"], link["link_id"])
        self.assertEqual(listed[0]["to_version"], 2)
        # A direct confirm on a linked claim stays allowed and leaves the
        # link pending (owner resolution is a later task).
        source = _source(self.store, TENANT_A, "manual-confirm")
        confirmed = self.store.confirm_claim(
            TENANT_A,
            str(k2["claim_id"]),
            reliable_source_id=str(source["source_id"]),
        )
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(
            self.store.list_claim_links(TENANT_A)[0]["status"], "pending"
        )

    def test_learn_from_turn_proposes_same_shape(self) -> None:
        k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        result = learn_from_turn(
            TENANT_A, "Запомни: я переехал в Пермь", store=self.store
        )
        self.assertEqual(len(result["persisted"]), 1)
        persisted = result["persisted"][0]
        self.assertEqual(persisted["state"], "candidate")
        links = persisted["reconciliations"]
        self.assertEqual(len(links), 1)
        link = links[0]
        self.assertIsInstance(link["link_id"], str)
        self.assertEqual(link["new_claim_id"], persisted["claim_id"])
        self.assertEqual(link["old_claim_id"], k1["claim_id"])
        self.assertEqual(link["relation"], "supersedes")
        self.assertEqual(link["status"], "pending")
        self.assertEqual(link["origin"], "slot_rule.v1")

    def test_confirm_required_remember_still_proposes(self) -> None:
        self.store.set_memory_mode(TENANT_A, MEMORY_MODE_CONFIRM_REQUIRED)
        k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        self.assertEqual(k1["state"], "candidate")
        self.assertEqual(k1["reconciliations"], [])
        confirm_candidate_memory(TENANT_A, str(k1["claim_id"]), store=self.store)
        k2 = remember_personal_insight(TENANT_A, PERM, store=self.store)
        self.assertEqual(k2["state"], "candidate")
        links = k2["reconciliations"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["old_claim_id"], k1["claim_id"])
        self.assertEqual(links[0]["new_claim_id"], k2["claim_id"])
        self.assertEqual(links[0]["status"], "pending")

    def test_rank_confirmed_returns_only_old_claim(self) -> None:
        remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        remember_personal_insight(TENANT_A, PERM_NEG, store=self.store)
        rows = rank_claims(
            TENANT_A, "где я живу", states=["confirmed"], store=self.store
        )
        contents = [_as_text(row.get("content")) for row in rows]
        self.assertEqual(contents, [KAZAN])

    def test_other_tenant_produces_no_links(self) -> None:
        k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        result_b = remember_personal_insight(
            TENANT_B, "я живу в Перми", store=self.store
        )
        self.assertEqual(result_b["state"], "confirmed")
        self.assertEqual(result_b["reconciliations"], [])
        self.assertEqual(self.store.list_claim_links(TENANT_B, status=None), [])
        conn = self.store._connect()
        try:
            rows = list(
                conn.execute(
                    "SELECT from_claim_id, to_claim_id FROM memory_claim_links"
                )
            )
        finally:
            conn.close()
        for row in rows:
            self.assertNotEqual(row["to_claim_id"], k1["claim_id"])
            self.assertNotEqual(row["from_claim_id"], k1["claim_id"])

    def test_version_chain_regression(self) -> None:
        source = _source(self.store, TENANT_A, "chain")
        created = self.store.create_candidate(
            TENANT_A,
            claim_kind="confirmed_fact",
            content=b"chain fact v1",
            confidence=0.8,
            explanation="seed",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            expires_at="2099-01-01T00:00:00Z",
        )
        claim_id = str(created["claim_id"])
        confirmed = self.store.confirm_claim(
            TENANT_A, claim_id, reliable_source_id=str(source["source_id"])
        )
        corrected = self.store.correct_claim(
            TENANT_A,
            claim_id,
            content=b"chain fact v3",
            explanation="fix",
            source_ids=[str(source["source_id"])],
        )
        conn = self.store._connect()
        try:
            rows = list(
                conn.execute(
                    "SELECT id, version, state, supersedes_id "
                    "FROM memory_claim_versions "
                    "WHERE claim_id = ? ORDER BY version",
                    (claim_id,),
                )
            )
        finally:
            conn.close()
        self.assertEqual([row["version"] for row in rows], [1, 2, 3])
        self.assertEqual(rows[0]["state"], "superseded")
        self.assertEqual(rows[1]["state"], "superseded")
        self.assertEqual(rows[2]["state"], "confirmed")
        self.assertIsNone(rows[0]["supersedes_id"])
        self.assertEqual(rows[1]["supersedes_id"], rows[0]["id"])
        self.assertEqual(rows[2]["supersedes_id"], rows[1]["id"])
        self.assertEqual(confirmed["supersedes_id"], created["claim_version_id"])
        self.assertEqual(corrected["supersedes_id"], confirmed["claim_version_id"])


class SlotRuleJudgeTests(unittest.TestCase):
    """S-RECON-JUDGE: unit coverage of the slot rule judge."""

    def setUp(self) -> None:
        self.judge = SlotRuleJudge()

    def test_judge_id_and_default(self) -> None:
        self.assertEqual(self.judge.judge_id, "slot_rule.v1")
        self.assertIsInstance(DEFAULT_JUDGE, SlotRuleJudge)
        self.assertTrue(callable(DEFAULT_JUDGE.judge))

    def test_contradicts_same_slot_disjoint_values(self) -> None:
        self.assertEqual(self.judge.judge(PERM, KAZAN), Relation.CONTRADICTS)
        self.assertEqual(
            self.judge.judge("I moved to Perm", "I live in Kazan"),
            Relation.CONTRADICTS,
        )
        self.assertEqual(
            self.judge.judge("I relocated to Perm", "I live in Kazan"),
            Relation.CONTRADICTS,
        )

    def test_refines_same_slot_overlapping_values(self) -> None:
        self.assertEqual(
            self.judge.judge("я живу в Казани, в центре", KAZAN),
            Relation.REFINES,
        )
        self.assertEqual(
            self.judge.judge("меня зовут Саша Иванов", "меня зовут Саша"),
            Relation.REFINES,
        )

    def test_temporary_marker_checked_before_slots(self) -> None:
        self.assertEqual(
            self.judge.judge("на этой неделе я живу в Сочи", KAZAN),
            Relation.TEMPORARY,
        )
        self.assertEqual(
            self.judge.judge("I temporarily live in Sochi", "I live in Kazan"),
            Relation.TEMPORARY,
        )
        self.assertEqual(
            self.judge.judge("сегодня я живу в гостинице", KAZAN),
            Relation.TEMPORARY,
        )

    def test_unrelated(self) -> None:
        self.assertEqual(
            self.judge.judge("я люблю чай", KAZAN), Relation.UNRELATED
        )
        self.assertEqual(
            self.judge.judge("меня зовут Саша", KAZAN), Relation.UNRELATED
        )
        self.assertEqual(
            self.judge.judge("я люблю чай", "я люблю кофе"),
            Relation.UNRELATED,
        )

    def test_stop_words_do_not_fill_slot_value(self) -> None:
        self.assertEqual(
            self.judge.judge(
                "я переехал в Пермь на юг", "я живу в Казани на севере"
            ),
            Relation.CONTRADICTS,
        )
        self.assertEqual(
            self.judge.judge(
                "I moved to Perm on Monday", "I live in Kazan on the hill"
            ),
            Relation.CONTRADICTS,
        )
        self.assertEqual(
            self.judge.judge("я живу в Казани, в центре", KAZAN),
            Relation.REFINES,
        )

    def test_stop_word_only_value_yields_no_slot(self) -> None:
        self.assertIsNone(self.judge.slot_of("я живу в на"))
        self.assertEqual(
            self.judge.judge("я живу в на", KAZAN), Relation.UNRELATED
        )

    def test_negation_does_not_set_slot_value(self) -> None:
        self.assertIsNone(self.judge.slot_of("больше не живу в Казани"))
        self.assertIsNone(self.judge.slot_of("I no longer live in Kazan"))
        slot = self.judge.slot_of(PERM_NEG)
        self.assertIsNotNone(slot)
        assert slot is not None
        self.assertEqual(slot[0], "home")
        self.assertEqual(slot[1], frozenset({stem_text("пермь")}))

    def test_english_slots(self) -> None:
        self.assertEqual(self.judge.slot_of("my name is Sasha")[0], "name")
        self.assertEqual(self.judge.slot_of("call me Sasha")[0], "name_style")
        self.assertEqual(self.judge.slot_of("I live in Kazan")[0], "home")
        self.assertEqual(self.judge.slot_of("меня зовут Саша")[0], "name")
        self.assertEqual(self.judge.slot_of("называй меня Сашей")[0], "name_style")
        self.assertEqual(
            self.judge.judge("называй меня Сашей", "меня зовут Саша"),
            Relation.UNRELATED,
        )

    def test_case_and_temporary_markers(self) -> None:
        self.assertEqual(
            self.judge.judge(PERM.upper(), KAZAN), Relation.CONTRADICTS
        )
        self.assertEqual(
            self.judge.judge("в командировке я живу в отеле", KAZAN),
            Relation.TEMPORARY,
        )


class StoreLinkTests(unittest.TestCase):
    """I-LINKS / I-SCHEMA-V5: link table, API, and tenant triggers."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-link-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_v5_objects_present(self) -> None:
        conn = self.store._connect()
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type IN ('table','index','trigger')"
                )
            }
            self.assertIn("memory_claim_links", names)
            self.assertIn("trg_claim_links_tenant_insert", names)
            self.assertIn("trg_claim_links_tenant_update", names)
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(memory_claim_versions)")
            }
            self.assertIn("change_reason", columns)
            link_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(memory_claim_links)")
            }
            for column in (
                "id", "tenant_id", "from_claim_id", "to_claim_id", "relation",
                "status", "to_version", "origin", "created_at", "resolved_at",
            ):
                self.assertIn(column, link_columns)
        finally:
            conn.close()

    def test_tenant_trigger_aborts_cross_tenant_insert(self) -> None:
        a_claim = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        b_claim = _confirmed(self.store, TENANT_B, "я живу в Перми", "b-perm")
        a_tenant = self.store.ensure_principal(TENANT_A)
        with self.assertRaises(sqlite3.IntegrityError) as exc:
            with self.store._tx() as conn:
                conn.execute(
                    "INSERT INTO memory_claim_links("
                    "id, tenant_id, from_claim_id, to_claim_id, relation, "
                    "status, to_version, origin, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "link-x",
                        a_tenant,
                        str(a_claim["claim_id"]),
                        str(b_claim["claim_id"]),
                        "supersedes",
                        "pending",
                        2,
                        "hand",
                        "2026-08-31T10:00:00Z",
                    ),
                )
        self.assertIn("tenant mismatch", str(exc.exception))

    def test_create_list_and_repeat_returns_existing(self) -> None:
        old = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        new = _candidate(self.store, TENANT_A, PERM, "a-perm")
        link = self.store.create_claim_link(
            TENANT_A,
            from_claim_id=str(new["claim_id"]),
            to_claim_id=str(old["claim_id"]),
            origin="manual",
        )
        self.assertEqual(link["relation"], "supersedes")
        self.assertEqual(link["status"], "pending")
        self.assertEqual(link["origin"], "manual")
        self.assertEqual(link["to_version"], 2)
        self.assertIsNone(link["resolved_at"])
        again = self.store.create_claim_link(
            TENANT_A,
            from_claim_id=str(new["claim_id"]),
            to_claim_id=str(old["claim_id"]),
            origin="other",
        )
        self.assertEqual(again["link_id"], link["link_id"])
        self.assertEqual(again["origin"], "manual")
        self.assertEqual(len(self.store.list_claim_links(TENANT_A, status=None)), 1)
        self.assertEqual(len(self.store.list_claim_links(TENANT_A)), 1)
        self.assertEqual(self.store.list_claim_links(TENANT_A, status="accepted"), [])
        for claim_id in (new["claim_id"], old["claim_id"]):
            self.assertEqual(
                len(self.store.list_claim_links(TENANT_A, claim_id=str(claim_id))),
                1,
            )
        self.assertEqual(
            self.store.list_claim_links(TENANT_A, claim_id="missing"),
            [],
        )
        conn = self.store._connect()
        try:
            audit = list(
                conn.execute(
                    "SELECT action, target_kind, target_id, outcome "
                    "FROM memory_audit WHERE action = 'claim.link_propose'"
                )
            )
        finally:
            conn.close()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["target_kind"], "claim_link")
        self.assertEqual(audit[0]["target_id"], link["link_id"])
        self.assertEqual(audit[0]["outcome"], "proposed")

    def test_rejected_link_is_not_resurrected(self) -> None:
        old = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        new = _candidate(self.store, TENANT_A, PERM, "a-perm")
        link = self.store.create_claim_link(
            TENANT_A,
            from_claim_id=str(new["claim_id"]),
            to_claim_id=str(old["claim_id"]),
            origin="manual",
        )
        conn = self.store._connect()
        try:
            conn.execute(
                "UPDATE memory_claim_links SET status = 'rejected', "
                "resolved_at = '2026-09-01T00:00:00Z' WHERE id = ?",
                (link["link_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        again = self.store.create_claim_link(
            TENANT_A,
            from_claim_id=str(new["claim_id"]),
            to_claim_id=str(old["claim_id"]),
            origin="manual",
        )
        self.assertEqual(again["link_id"], link["link_id"])
        self.assertEqual(again["status"], "rejected")
        self.assertEqual(
            len(self.store.list_claim_links(TENANT_A, status=None)), 1
        )

    def test_link_validations(self) -> None:
        old = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        old_id = str(old["claim_id"])
        with self.assertRaises(ValidationError):
            self.store.create_claim_link(
                TENANT_A, from_claim_id=old_id, to_claim_id=old_id, origin="x"
            )
        with self.assertRaises(ValidationError):
            self.store.create_claim_link(
                TENANT_A, from_claim_id=old_id, to_claim_id="other", origin=""
            )
        with self.assertRaises(NotFoundError):
            self.store.create_claim_link(
                TENANT_A, from_claim_id=old_id, to_claim_id="missing", origin="x"
            )
        b_claim = _confirmed(self.store, TENANT_B, "я живу в Перми", "b-perm")
        with self.assertRaises(NotFoundError):
            self.store.create_claim_link(
                TENANT_A,
                from_claim_id=old_id,
                to_claim_id=str(b_claim["claim_id"]),
                origin="x",
            )

    def test_link_not_found_text_identical_for_missing_and_foreign(self) -> None:
        old = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        b_claim = _confirmed(self.store, TENANT_B, "я живу в Перми", "b-perm")
        messages: list[str] = []
        for to_id in (str(uuid.uuid4()), str(b_claim["claim_id"])):
            with self.assertRaises(NotFoundError) as ctx:
                self.store.create_claim_link(
                    TENANT_A,
                    from_claim_id=str(old["claim_id"]),
                    to_claim_id=to_id,
                    origin="x",
                )
            messages.append(str(ctx.exception))
        self.assertEqual(len(set(messages)), 1)
        self.assertEqual(messages[0], "Claim not found")

    def test_create_link_requires_confirmed_target(self) -> None:
        target = _candidate(self.store, TENANT_A, KAZAN, "a-kazan")
        new = _candidate(self.store, TENANT_A, PERM, "a-perm")
        with self.assertRaises(ValidationError) as ctx:
            self.store.create_claim_link(
                TENANT_A,
                from_claim_id=str(new["claim_id"]),
                to_claim_id=str(target["claim_id"]),
                origin="manual",
            )
        self.assertEqual(
            str(ctx.exception), "claim link: target claim must be confirmed"
        )
        conn = self.store._connect()
        try:
            audit = conn.execute(
                "SELECT outcome, policy_reason FROM memory_audit "
                "WHERE action = 'claim.link_propose'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(audit["outcome"], "rejected")
        self.assertEqual(audit["policy_reason"], "invalid_transition")
        self.assertEqual(self.store.list_claim_links(TENANT_A, status=None), [])

    def _expire_newest_version(self, claim_id: str) -> None:
        conn = self.store._connect()
        try:
            conn.execute(
                "UPDATE memory_claim_versions "
                "SET expires_at = '2000-01-01T00:00:00Z' "
                "WHERE claim_id = ? AND version = ("
                "SELECT MAX(version) FROM memory_claim_versions "
                "WHERE claim_id = ?)",
                (claim_id, claim_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_link_from_superseded_claim_rejected(self) -> None:
        old = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        new = _candidate(self.store, TENANT_A, PERM, "a-perm")
        link = self.store.create_claim_link(
            TENANT_A,
            from_claim_id=str(new["claim_id"]),
            to_claim_id=str(old["claim_id"]),
            origin="manual",
        )
        accept_memory_reconciliation(
            TENANT_A, link["link_id"], store=self.store
        )
        third = _confirmed(self.store, TENANT_A, "я живу в Уфе", "a-ufa")
        with self.assertRaises(ValidationError) as ctx:
            self.store.create_claim_link(
                TENANT_A,
                from_claim_id=str(old["claim_id"]),
                to_claim_id=str(third["claim_id"]),
                origin="manual",
            )
        self.assertEqual(
            str(ctx.exception), "claim link: source claim must be active"
        )
        conn = self.store._connect()
        try:
            audit = conn.execute(
                "SELECT outcome, policy_reason FROM memory_audit "
                "WHERE action = 'claim.link_propose' AND outcome = 'rejected'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(audit)
        self.assertEqual(audit["policy_reason"], "invalid_transition")
        self.assertEqual(
            self.store.list_claim_links(
                TENANT_A, status=None, claim_id=str(third["claim_id"])
            ),
            [],
        )

    def test_link_with_expired_claim_is_not_found(self) -> None:
        old = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        new = _candidate(self.store, TENANT_A, PERM, "a-perm")
        expired = _confirmed(self.store, TENANT_A, "я живу в Уфе", "a-ufa")
        self._expire_newest_version(str(expired["claim_id"]))
        messages: list[str] = []
        for from_id, to_id in (
            (str(new["claim_id"]), str(expired["claim_id"])),
            (str(expired["claim_id"]), str(old["claim_id"])),
            (str(new["claim_id"]), str(uuid.uuid4())),
        ):
            with self.assertRaises(NotFoundError) as ctx:
                self.store.create_claim_link(
                    TENANT_A,
                    from_claim_id=from_id,
                    to_claim_id=to_id,
                    origin="x",
                )
            messages.append(str(ctx.exception))
        self.assertEqual(messages, ["Claim not found"] * 3)
        conn = self.store._connect()
        try:
            audits = list(
                conn.execute(
                    "SELECT outcome, policy_reason FROM memory_audit "
                    "WHERE action = 'claim.link_propose'"
                )
            )
        finally:
            conn.close()
        self.assertEqual(
            [(row["outcome"], row["policy_reason"]) for row in audits],
            [("denied", "not_found")] * 3,
        )

    def test_repeat_returns_existing_link_after_target_superseded(self) -> None:
        old = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        new = _candidate(self.store, TENANT_A, PERM, "a-perm")
        third = _candidate(self.store, TENANT_A, "я живу в Уфе", "a-ufa")
        link = self.store.create_claim_link(
            TENANT_A,
            from_claim_id=str(new["claim_id"]),
            to_claim_id=str(old["claim_id"]),
            origin="manual",
        )
        other = self.store.create_claim_link(
            TENANT_A,
            from_claim_id=str(third["claim_id"]),
            to_claim_id=str(old["claim_id"]),
            origin="manual",
        )
        accept_memory_reconciliation(
            TENANT_A, other["link_id"], store=self.store
        )
        again = self.store.create_claim_link(
            TENANT_A,
            from_claim_id=str(new["claim_id"]),
            to_claim_id=str(old["claim_id"]),
            origin="other",
        )
        self.assertEqual(again["link_id"], link["link_id"])
        self.assertEqual(again["status"], "pending")

    def test_restricted_claims_are_invisible_to_create_link(self) -> None:
        normal = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        restricted = _restricted(self.store, TENANT_A, "код 4321", "a-secret")
        normal_id = str(normal["claim_id"])
        restricted_id = str(restricted["claim_id"])
        messages: list[str] = []
        for from_id, to_id in (
            (normal_id, restricted_id),
            (restricted_id, normal_id),
            (normal_id, str(uuid.uuid4())),
        ):
            with self.assertRaises(NotFoundError) as ctx:
                self.store.create_claim_link(
                    TENANT_A,
                    from_claim_id=from_id,
                    to_claim_id=to_id,
                    origin="x",
                )
            messages.append(str(ctx.exception))
        self.assertEqual(messages, ["Claim not found"] * 3)

    def test_tenant_trigger_aborts_cross_tenant_update(self) -> None:
        a_claim = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        b_claim = _confirmed(self.store, TENANT_B, "я живу в Перми", "b-perm")
        new = _candidate(self.store, TENANT_A, PERM, "a-perm")
        link = self.store.create_claim_link(
            TENANT_A,
            from_claim_id=str(new["claim_id"]),
            to_claim_id=str(a_claim["claim_id"]),
            origin="hand",
        )
        with self.assertRaises(sqlite3.IntegrityError) as exc:
            with self.store._tx() as conn:
                conn.execute(
                    "UPDATE memory_claim_links SET from_claim_id = ? "
                    "WHERE id = ?",
                    (str(b_claim["claim_id"]), link["link_id"]),
                )
        self.assertIn("tenant mismatch", str(exc.exception))
        self.assertEqual(
            self.store.list_claim_links(TENANT_A)[0]["from_claim_id"],
            str(new["claim_id"]),
        )

    def test_manual_propose_shape(self) -> None:
        old = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        new = _candidate(self.store, TENANT_A, PERM, "a-perm")
        item = propose_reconciliation(
            TENANT_A,
            str(new["claim_id"]),
            str(old["claim_id"]),
            store=self.store,
        )
        self.assertIsInstance(item["link_id"], str)
        self.assertEqual(item["new_claim_id"], str(new["claim_id"]))
        self.assertEqual(item["old_claim_id"], str(old["claim_id"]))
        self.assertEqual(item["relation"], "supersedes")
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["origin"], "manual")


class _ContradictStub:
    judge_id = "stub.always_contradicts"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def judge(self, new_text: str, old_text: str) -> Relation:
        self.calls.append((new_text, old_text))
        return Relation.CONTRADICTS


class ProposeBudgetTests(unittest.TestCase):
    """S-RECON-JUDGE-4: at most five judged candidates, tenant-scoped."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-budget-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_at_most_five_candidates_tenant_a_only(self) -> None:
        a_texts = [f"я живу в Городе{i}" for i in range(7)]
        a_claims = [
            _confirmed(self.store, TENANT_A, text, f"a-{i}")
            for i, text in enumerate(a_texts)
        ]
        b_claim = _confirmed(self.store, TENANT_B, "я живу в Городе9", "b-9")
        new = _candidate(self.store, TENANT_A, "я живу в Омске", "a-omsk")
        stub = _ContradictStub()
        results = propose_reconciliations(
            TENANT_A, str(new["claim_id"]), judge=stub, store=self.store
        )
        self.assertEqual(len(stub.calls), 5)
        old_texts = {old for _, old in stub.calls}
        self.assertTrue(old_texts <= set(a_texts))
        self.assertTrue(
            all(new_text == "я живу в Омске" for new_text, _ in stub.calls)
        )
        self.assertEqual(len(results), 5)
        for item in results:
            self.assertEqual(item["relation"], "supersedes")
            self.assertEqual(item["status"], "pending")
            self.assertEqual(item["origin"], "stub.always_contradicts")
            self.assertEqual(item["new_claim_id"], str(new["claim_id"]))
        a_ids = {str(claim["claim_id"]) for claim in a_claims}
        self.assertTrue({item["old_claim_id"] for item in results} <= a_ids)
        links = self.store.list_claim_links(TENANT_A, status=None)
        self.assertEqual(len(links), 5)
        for link in links:
            self.assertNotEqual(link["to_claim_id"], str(b_claim["claim_id"]))
            self.assertNotEqual(link["from_claim_id"], str(b_claim["claim_id"]))
        self.assertEqual(self.store.list_claim_links(TENANT_B, status=None), [])

    def test_repeat_proposal_returns_existing_link(self) -> None:
        old = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        new = _candidate(self.store, TENANT_A, PERM, "a-perm")
        first = propose_reconciliations(
            TENANT_A, str(new["claim_id"]), store=self.store
        )
        self.assertEqual(len(first), 1)
        second = propose_reconciliations(
            TENANT_A, str(new["claim_id"]), store=self.store
        )
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["link_id"], first[0]["link_id"])
        self.assertEqual(second[0]["status"], "pending")
        self.assertEqual(
            len(self.store.list_claim_links(TENANT_A, status=None)), 1
        )


class _EraseInsideJudge:
    judge_id = "stub.erase_inside"

    def __init__(
        self, store: PersonalMemoryStore, actor: Actor, target_id: str
    ) -> None:
        self.store = store
        self.actor = actor
        self.target_id = target_id

    def judge(self, new_text: str, old_text: str) -> Relation:
        self.store.erase_claim(self.actor, self.target_id)
        return Relation.CONTRADICTS


class _FlipStateInsideJudge:
    judge_id = "stub.flip_state"

    def __init__(self, store: PersonalMemoryStore, target_id: str) -> None:
        self.store = store
        self.target_id = target_id

    def judge(self, new_text: str, old_text: str) -> Relation:
        conn = self.store._connect()
        try:
            conn.execute(
                "UPDATE memory_claim_versions SET state = 'superseded' "
                "WHERE claim_id = ?",
                (self.target_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return Relation.CONTRADICTS


class ProposeRaceTests(unittest.TestCase):
    """PH-2 r1 F3: a candidate vanishing or changing state between the
    scan and create_claim_link skips it instead of aborting the call."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-race-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_erased_candidate_mid_scan_is_skipped(self) -> None:
        old = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        new = _candidate(self.store, TENANT_A, PERM, "a-perm")
        stub = _EraseInsideJudge(
            self.store, TENANT_A, str(old["claim_id"])
        )
        results = propose_reconciliations(
            TENANT_A, str(new["claim_id"]), judge=stub, store=self.store
        )
        self.assertEqual(results, [])
        self.assertEqual(
            self.store.list_claim_links(TENANT_A, status=None), []
        )

    def test_state_changed_candidate_mid_scan_is_skipped(self) -> None:
        old = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        new = _candidate(self.store, TENANT_A, PERM, "a-perm")
        stub = _FlipStateInsideJudge(self.store, str(old["claim_id"]))
        results = propose_reconciliations(
            TENANT_A, str(new["claim_id"]), judge=stub, store=self.store
        )
        self.assertEqual(results, [])
        self.assertEqual(
            self.store.list_claim_links(TENANT_A, status=None), []
        )


class ReconJudgeIntegrationTests(unittest.TestCase):
    """S-RECON-JUDGE 1-3: temporary/refines/unrelated do not link."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-judge-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_temporary_text_confirms_without_link(self) -> None:
        k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        k2 = remember_personal_insight(
            TENANT_A, "на этой неделе я живу в Сочи", store=self.store
        )
        self.assertEqual(k2["state"], "confirmed")
        self.assertEqual(k2["reconciliations"], [])
        self.assertEqual(self.store.list_claim_links(TENANT_A, status=None), [])
        rows = query_claims(TENANT_A, states=["confirmed"], store=self.store)
        self.assertEqual(len(rows), 2)
        self.assertEqual(k1["state"], "confirmed")

    def test_refines_confirms_without_link(self) -> None:
        remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        k2 = remember_personal_insight(
            TENANT_A, "я живу в Казани, в центре", store=self.store
        )
        self.assertEqual(k2["state"], "confirmed")
        self.assertEqual(k2["reconciliations"], [])
        self.assertEqual(self.store.list_claim_links(TENANT_A, status=None), [])

    def test_unrelated_confirms_without_link(self) -> None:
        remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        k2 = remember_personal_insight(TENANT_A, "я люблю чай", store=self.store)
        self.assertEqual(k2["state"], "confirmed")
        self.assertEqual(k2["reconciliations"], [])
        self.assertEqual(self.store.list_claim_links(TENANT_A, status=None), [])


class ReconResolveTests(unittest.TestCase):
    """S-RECON-RESOLVE: owner accept/reject of pending replacement links."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-resolve-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _linked_pair(self) -> tuple[dict, dict, dict]:
        k1 = remember_personal_insight(TENANT_A, KAZAN, store=self.store)
        k2 = remember_personal_insight(TENANT_A, PERM_NEG, store=self.store)
        links = k2["reconciliations"]
        self.assertEqual(len(links), 1)
        return k1, k2, links[0]

    def _version_rows(self, claim_id: object) -> list[sqlite3.Row]:
        conn = self.store._connect()
        try:
            return list(
                conn.execute(
                    "SELECT version, state, change_reason, supersedes_id "
                    "FROM memory_claim_versions "
                    "WHERE claim_id = ? ORDER BY version",
                    (str(claim_id),),
                )
            )
        finally:
            conn.close()

    def _link_row(self, link_id: object) -> sqlite3.Row:
        conn = self.store._connect()
        try:
            return conn.execute(
                "SELECT status, resolved_at FROM memory_claim_links WHERE id = ?",
                (str(link_id),),
            ).fetchone()
        finally:
            conn.close()

    def test_accept_promotes_new_and_supersedes_old(self) -> None:
        k1, k2, link = self._linked_pair()
        result = accept_memory_reconciliation(
            TENANT_A, link["link_id"], store=self.store
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["link_id"], link["link_id"])
        self.assertEqual(result["new_claim_id"], k2["claim_id"])
        self.assertEqual(result["old_claim_id"], k1["claim_id"])
        self.assertEqual(result["new_state"], "confirmed")
        self.assertEqual(result["old_state"], "superseded")
        self.assertIsInstance(result["confirmation"], str)
        k2_rows = self._version_rows(k2["claim_id"])
        k1_rows = self._version_rows(k1["claim_id"])
        self.assertEqual(k2_rows[-1]["state"], "confirmed")
        self.assertEqual(k2_rows[-1]["change_reason"], "promoted")
        self.assertEqual(k2_rows[-2]["state"], "superseded")
        self.assertEqual(k1_rows[-1]["state"], "superseded")
        self.assertEqual(k1_rows[-1]["change_reason"], "changed")
        self.assertEqual(k1_rows[-1]["version"], k1_rows[-2]["version"] + 1)
        link_row = self._link_row(link["link_id"])
        self.assertEqual(link_row["status"], "accepted")
        self.assertIsNotNone(link_row["resolved_at"])
        conn = self.store._connect()
        try:
            confirmations = list(
                conn.execute(
                    "SELECT id FROM memory_sources "
                    "WHERE source_kind = 'user_confirmation'"
                )
            )
        finally:
            conn.close()
        self.assertTrue(confirmations)

    def test_accepted_link_ranks_only_new_claim(self) -> None:
        k1, k2, link = self._linked_pair()
        accept_memory_reconciliation(TENANT_A, link["link_id"], store=self.store)
        rows = rank_claims(
            TENANT_A, "где я живу", states=["confirmed"], store=self.store
        )
        contents = [_as_text(row.get("content")) for row in rows]
        self.assertEqual(contents, [PERM_NEG])

    def test_accept_fts_drops_old_and_keeps_new(self) -> None:
        k1, k2, link = self._linked_pair()
        build_claim_fts_index(TENANT_A, store=self.store)
        accept_memory_reconciliation(TENANT_A, link["link_id"], store=self.store)
        old_hits = [
            row
            for row in rank_claims_fts(TENANT_A, "Казани", store=self.store)
            if str(row["claim_id"]) == str(k1["claim_id"])
        ]
        self.assertEqual(old_hits, [])
        new_ids = {
            str(row["claim_id"])
            for row in rank_claims_fts(TENANT_A, "Пермь", store=self.store)
        }
        self.assertIn(str(k2["claim_id"]), new_ids)
        terminal = get_authorized_claim(
            TENANT_A,
            str(k1["claim_id"]),
            include_content=True,
            store=self.store,
        )
        self.assertEqual(terminal["state"], "superseded")
        self.assertEqual(_as_text(terminal.get("content")), KAZAN)

    def test_reject_keeps_facts_and_blocks_reproposal(self) -> None:
        k1, k2, link = self._linked_pair()
        result = reject_memory_reconciliation(
            TENANT_A, link["link_id"], store=self.store
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["new_state"], "candidate")
        self.assertEqual(result["old_state"], "confirmed")
        link_row = self._link_row(link["link_id"])
        self.assertEqual(link_row["status"], "rejected")
        self.assertIsNotNone(link_row["resolved_at"])
        self.assertEqual(self._version_rows(k1["claim_id"])[-1]["state"], "confirmed")
        self.assertEqual(self._version_rows(k2["claim_id"])[-1]["state"], "candidate")
        again = propose_reconciliation(
            TENANT_A,
            str(k2["claim_id"]),
            str(k1["claim_id"]),
            store=self.store,
        )
        self.assertEqual(again["link_id"], link["link_id"])
        self.assertEqual(again["status"], "rejected")

    def test_direct_confirm_then_accept_adds_no_second_version(self) -> None:
        k1, k2, link = self._linked_pair()
        confirm_candidate_memory(TENANT_A, str(k2["claim_id"]), store=self.store)
        self.assertEqual(self._link_row(link["link_id"])["status"], "pending")
        k2_before = len(self._version_rows(k2["claim_id"]))
        result = accept_memory_reconciliation(
            TENANT_A, link["link_id"], store=self.store
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["new_state"], "confirmed")
        self.assertEqual(len(self._version_rows(k2["claim_id"])), k2_before)
        self.assertEqual(self._version_rows(k1["claim_id"])[-1]["state"], "superseded")

    def test_accept_after_old_corrected_raises_conflict(self) -> None:
        k1, k2, link = self._linked_pair()
        correct_personal_memory(
            TENANT_A,
            str(k1["claim_id"]),
            "я живу в Казани у Кремля",
            store=self.store,
        )
        k1_count = len(self._version_rows(k1["claim_id"]))
        k2_count = len(self._version_rows(k2["claim_id"]))
        with self.assertRaises(ConflictError):
            accept_memory_reconciliation(
                TENANT_A, link["link_id"], store=self.store
            )
        self.assertEqual(self._link_row(link["link_id"])["status"], "pending")
        self.assertEqual(len(self._version_rows(k1["claim_id"])), k1_count)
        self.assertEqual(len(self._version_rows(k2["claim_id"])), k2_count)
        self.assertEqual(self._version_rows(k1["claim_id"])[-1]["state"], "confirmed")
        self.assertEqual(self._version_rows(k2["claim_id"])[-1]["state"], "candidate")

    def test_cross_tenant_and_missing_denials_identical(self) -> None:
        _k1, _k2, link = self._linked_pair()
        messages: list[str] = []
        for flow in (accept_memory_reconciliation, reject_memory_reconciliation):
            for link_id in (str(link["link_id"]), str(uuid.uuid4())):
                with self.assertRaises(NotFoundError) as ctx:
                    flow(TENANT_B, link_id, store=self.store)
                messages.append(str(ctx.exception))
        self.assertEqual(len(set(messages)), 1)
        self.assertEqual(self._link_row(link["link_id"])["status"], "pending")

    def test_accept_preflight_registers_nothing_for_unknown_link(self) -> None:
        _k1, _k2, link = self._linked_pair()
        a_tenant = self.store.ensure_principal(TENANT_A)
        b_tenant = self.store.ensure_principal(TENANT_B)

        def counts() -> tuple[int, int, int]:
            conn = self.store._connect()
            try:
                a_sources = conn.execute(
                    "SELECT COUNT(*) FROM memory_sources WHERE tenant_id = ?",
                    (a_tenant,),
                ).fetchone()[0]
                b_sources = conn.execute(
                    "SELECT COUNT(*) FROM memory_sources WHERE tenant_id = ?",
                    (b_tenant,),
                ).fetchone()[0]
                audits = conn.execute(
                    "SELECT COUNT(*) FROM memory_audit "
                    "WHERE action = 'source.register'"
                ).fetchone()[0]
                return int(a_sources), int(b_sources), int(audits)
            finally:
                conn.close()

        before = counts()
        with self.assertRaises(NotFoundError) as missing_ctx:
            accept_memory_reconciliation(
                TENANT_A, str(uuid.uuid4()), store=self.store
            )
        self.assertEqual(str(missing_ctx.exception), "Claim link not found")
        with self.assertRaises(NotFoundError) as foreign_ctx:
            accept_memory_reconciliation(
                TENANT_B, str(link["link_id"]), store=self.store
            )
        self.assertEqual(
            str(foreign_ctx.exception), str(missing_ctx.exception)
        )
        self.assertEqual(counts(), before)
        self.assertEqual(self._link_row(link["link_id"])["status"], "pending")

    def test_accept_with_restricted_new_claim_raises_conflict(self) -> None:
        k1, k2, link = self._linked_pair()
        source = _source(self.store, TENANT_A, "restrict-fix")
        self.store.correct_claim(
            TENANT_A,
            str(k2["claim_id"]),
            content="чувствительный факт".encode("utf-8"),
            explanation="restrict",
            source_ids=[str(source["source_id"])],
            sensitivity="restricted",
        )
        k1_count = len(self._version_rows(k1["claim_id"]))
        k2_count = len(self._version_rows(k2["claim_id"]))
        with self.assertRaises(ConflictError):
            accept_memory_reconciliation(
                TENANT_A, link["link_id"], store=self.store
            )
        self.assertEqual(self._link_row(link["link_id"])["status"], "pending")
        self.assertEqual(len(self._version_rows(k1["claim_id"])), k1_count)
        self.assertEqual(len(self._version_rows(k2["claim_id"])), k2_count)
        self.assertEqual(
            self._version_rows(k1["claim_id"])[-1]["state"], "confirmed"
        )

    def test_list_memory_reconciliations_shows_contents(self) -> None:
        k1, k2, link = self._linked_pair()
        listing = list_memory_reconciliations(TENANT_A, store=self.store)
        self.assertEqual(listing["count"], 1)
        item = listing["items"][0]
        self.assertEqual(item["link_id"], link["link_id"])
        self.assertEqual(item["from_claim_id"], k2["claim_id"])
        self.assertEqual(item["to_claim_id"], k1["claim_id"])
        self.assertEqual(item["new_claim_id"], k2["claim_id"])
        self.assertEqual(item["old_claim_id"], k1["claim_id"])
        self.assertIsInstance(item["new_content"], str)
        self.assertIsInstance(item["old_content"], str)
        self.assertEqual(item["new_content"], PERM_NEG)
        self.assertEqual(item["old_content"], KAZAN)
        self.assertEqual(listing["language"], "en")
        self.assertEqual(
            listing["heading"], "Proposed memory replacements:"
        )
        ru_listing = list_memory_reconciliations(
            TENANT_A, language="ru", store=self.store
        )
        self.assertEqual(ru_listing["language"], "ru")
        self.assertEqual(
            ru_listing["heading"], "Предложенные замены фактов:"
        )
        empty = list_memory_reconciliations(
            TENANT_A, status="accepted", store=self.store
        )
        self.assertEqual(empty["count"], 0)
        self.assertEqual(empty["items"], [])
        with self.assertRaises(ValidationError):
            list_memory_reconciliations(
                TENANT_A, status="bogus", store=self.store
            )

    def test_list_memory_reconciliations_marks_binary_content(self) -> None:
        old = _confirmed(self.store, TENANT_A, KAZAN, "a-kazan")
        source = _source(self.store, TENANT_A, "a-binary")
        new = self.store.create_candidate(
            TENANT_A,
            claim_kind="confirmed_fact",
            content=b"\xff\xfe\x01binary",
            confidence=0.9,
            explanation="seed",
            sensitivity="normal",
            source_ids=[str(source["source_id"])],
            expires_at="2099-01-01T00:00:00Z",
        )
        link = propose_reconciliation(
            TENANT_A,
            str(new["claim_id"]),
            str(old["claim_id"]),
            store=self.store,
        )
        listing = list_memory_reconciliations(TENANT_A, store=self.store)
        self.assertEqual(listing["count"], 1)
        item = listing["items"][0]
        self.assertEqual(item["link_id"], link["link_id"])
        self.assertEqual(item["new_content"], "<binary>")
        self.assertEqual(item["old_content"], KAZAN)

    def test_erase_old_claim_removes_link(self) -> None:
        k1, k2, link = self._linked_pair()
        self.store.erase_claim(TENANT_A, str(k1["claim_id"]))
        conn = self.store._connect()
        try:
            rows = list(
                conn.execute(
                    "SELECT id FROM memory_claim_links "
                    "WHERE from_claim_id = ? OR to_claim_id = ?",
                    (str(k1["claim_id"]), str(k1["claim_id"])),
                )
            )
        finally:
            conn.close()
        self.assertEqual(rows, [])
        survivor = get_authorized_claim(
            TENANT_A, str(k2["claim_id"]), store=self.store
        )
        self.assertEqual(survivor["state"], "candidate")

    def test_erase_new_claim_removes_link(self) -> None:
        k1, k2, link = self._linked_pair()
        self.store.erase_claim(TENANT_A, str(k2["claim_id"]))
        conn = self.store._connect()
        try:
            rows = list(
                conn.execute(
                    "SELECT id FROM memory_claim_links "
                    "WHERE from_claim_id = ? OR to_claim_id = ?",
                    (str(k2["claim_id"]), str(k2["claim_id"])),
                )
            )
        finally:
            conn.close()
        self.assertEqual(rows, [])
        survivor = get_authorized_claim(
            TENANT_A, str(k1["claim_id"]), store=self.store
        )
        self.assertEqual(survivor["state"], "confirmed")


if __name__ == "__main__":
    unittest.main()

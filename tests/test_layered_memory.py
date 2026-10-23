"""Layered memory behavioral, privacy, budget and migration regression tests."""
import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

import agent_personal_memory as apm
from agent_personal_memory.consolidation import bounds, valid_projections

ALICE = apm.Actor("test", "alice")
BOB = apm.Actor("test", "bob")
NOW = "2026-10-01T10:00:00Z"
POLICY = apm.CapturePolicy(True, 3650)


class LayeredMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = apm.PersonalMemoryStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, text="I prefer tea.", identity="event-1", **kw):
        return apm.record_turn(ALICE, event_id=identity, text=text, occurred_at=kw.pop("occurred_at", NOW), policy=kw.pop("policy", POLICY), store=self.store, **kw)

    def fact(self, text="I prefer tea.", identity="event-1"):
        event = self.event(text, identity)
        claim = apm.remember_from_evidence(ALICE, source_id=event["source_id"], source_version=event["version"], start=0, end=len(text), store=self.store)
        return event, claim

    def run_period(self, **kw):
        return apm.consolidate_period(ALICE, period=kw.pop("period", "day"), start=kw.pop("start", "2026-10-01"), store=self.store, **kw)

    def test_event_replay_edit_identity_and_source_preservation(self):
        text = "I prefer tea. This is my original wording."
        first = self.event(text)
        self.assertEqual(self.event(text)["status"], "replayed")
        self.assertNotEqual(self.event(text, "event-2")["source_id"], first["source_id"])
        self.assertEqual(self.event(text, session_id="session-2")["version"], 2)
        self.assertEqual(apm.query_journal(ALICE, store=self.store, include_text=True)["items"][0]["text"], text)
        reopened = apm.PersonalMemoryStore(self.tmp.name)
        self.assertEqual(len(apm.query_journal(ALICE, store=reopened)["items"]), 2)

    def test_capture_exclusions_retention_and_foreign_actor(self):
        self.assertEqual(self.event(policy=apm.CapturePolicy())["status"], "skipped")
        self.assertEqual(self.event("Не запоминай это сообщение.")["status"], "skipped")
        self.assertEqual(self.event("password is very-private")["status"], "skipped")
        self.assertEqual(self.event(occurred_at="2020-01-01T00:00:00Z", policy=apm.CapturePolicy(True, 1))["reason"], "expired")
        event, claim = self.fact()
        self.assertEqual(apm.query_journal(BOB, store=self.store)["items"], [])
        self.assertEqual(apm.build_core(BOB, store=self.store)["text"], "")
        with self.assertRaises(apm.NotFoundError):
            apm.evidence_for_claim(BOB, claim["claim_id"], store=self.store)
        apm.prune_journal(ALICE, store=self.store, now="2040-01-01T00:00:00Z")
        self.assertEqual(apm.build_core(ALICE, store=self.store)["text"], "")
        self.assertEqual(self.event()["status"], "forgotten")

    def test_forwarded_assistant_derived_cannot_be_owner_fact(self):
        for i, kwargs in enumerate(({"role": "assistant"}, {"origin": "forwarded"}, {"origin": "derived", "attachments": ("audio-ref",)})):
            event = self.event(identity=str(i), **kwargs)
            with self.assertRaises(apm.ValidationError):
                apm.remember_from_evidence(ALICE, source_id=event["source_id"], source_version=1, start=0, end=13, store=self.store)
        self.assertEqual(apm.build_core(ALICE, store=self.store)["text"], "")

    def test_assistant_parent_authority_edit_and_forget_cascade(self):
        event, claim = self.fact()
        bound = dict(role="assistant", origin="derived", parent_source_id=event["source_id"], parent_source_version=1)
        self.assertEqual(apm.get_journal_event(ALICE, event_id="event-1", store=self.store)["version"], 1)
        self.assertIsNone(apm.get_journal_event(BOB, event_id="event-1", store=self.store))
        reply = self.event("You prefer tea.", "reply-v1", **bound)
        with self.assertRaises(apm.ConflictError):
            self.event("A user statement.", "reply-v1", origin="derived", parent_source_id=event["source_id"], parent_source_version=1)
        with self.assertRaises(apm.NotFoundError):
            apm.record_turn(BOB, event_id="foreign", text="You prefer tea.", occurred_at=NOW, policy=POLICY, store=self.store, **bound)
        with self.assertRaises(apm.NotFoundError):
            self.event("Stale reply.", "stale", **{**bound, "parent_source_version": 2})
        # A response to a different prompt can recall the forgotten older fact.
        unrelated = self.event("Tell me a joke.", "unrelated")
        self.event("You prefer tea, so here is a joke.", "recall-reply", role="assistant", origin="derived", parent_source_id=unrelated["source_id"], parent_source_version=1)
        apm.record_turn(BOB, event_id="bob-reply", text="Another conversation.", occurred_at=NOW, role="assistant", policy=POLICY, store=self.store)
        self.store.erase_claim(ALICE, claim["claim_id"])
        remaining = apm.query_journal(ALICE, store=self.store, include_text=True)["items"]
        self.assertTrue(any(x["text"] == "Tell me a joke." for x in remaining))
        self.assertFalse(any(x["role"] == "assistant" for x in remaining))
        self.assertEqual(len(apm.query_journal(BOB, store=self.store)["items"]), 1)
        self.assertIsNone(apm.get_journal_event(ALICE, event_id="reply-v1", store=self.store))
        self.assertEqual(self.event("You prefer tea.", "reply-v1", **bound)["status"], "forgotten")
        with self.assertRaises(apm.NotFoundError):
            self.event("You prefer tea.", "new-reply", **bound)
        second = self.event("A new question.", "question")
        second_bound = dict(role="assistant", origin="derived", parent_source_id=second["source_id"], parent_source_version=1)
        self.event("A previous answer.", "second-response", **second_bound)
        self.event("An edited question.", "question")
        self.assertIsNone(apm.get_journal_event(ALICE, event_id="second-response", store=self.store))
        self.assertEqual(apm.get_journal_event(ALICE, event_id="question", store=self.store)["version"], 2)
        legacy, legacy_claim = self.fact("I prefer coffee.", "legacy")
        with self.store._tx() as conn:
            conn.execute("DELETE FROM memory_evidence WHERE claim_id=?", (legacy_claim["claim_id"],))
        self.event("You prefer coffee.", "legacy-response", role="assistant")
        self.store.erase_claim(ALICE, legacy_claim["claim_id"])
        self.assertIsNone(apm.get_journal_event(ALICE, event_id="legacy-response", store=self.store))

    def test_parent_retention_erases_later_assistant_response(self):
        event = self.event("A question.", "expiring", occurred_at="2026-10-01T10:00:00Z")
        self.event("A generated answer.", "later-answer", occurred_at="2026-10-02T10:00:00Z", role="assistant", origin="derived", parent_source_id=event["source_id"], parent_source_version=1)
        result = apm.prune_journal(ALICE, store=self.store, now="2036-09-28T12:00:00Z")
        self.assertGreaterEqual(result["expired"], 1)
        self.assertEqual(apm.query_journal(ALICE, store=self.store)["items"], [])

    def test_inflight_reply_cannot_resurrect_invalidated_memory(self):
        for action in ("forget", "edit", "correct", "retract", "prune"):
            with self.subTest(action=action):
                old, claim = self.fact("I prefer violet tea.", "old-" + action)
                parent_id = "question-" + action
                parent = self.event("What tea do I prefer?", parent_id, occurred_at="2026-10-02T10:00:00Z")
                generation = apm.get_journal_event(ALICE, event_id=parent_id, store=self.store)["generation"]
                bob = apm.record_turn(BOB, event_id=parent_id, text="My own question.", occurred_at=NOW, policy=POLICY, store=self.store)
                if action == "forget":
                    self.store.erase_claim(ALICE, claim["claim_id"])
                elif action == "edit":
                    self.event("I prefer amber tea.", "old-" + action)
                elif action == "correct":
                    self.store.correct_claim(ALICE, claim["claim_id"], content=b"I prefer amber tea.", explanation="Correction", source_ids=[old["source_id"]])
                elif action == "retract":
                    self.store.retract_claim(ALICE, claim["claim_id"], source_ids=[old["source_id"]])
                else:
                    apm.prune_journal(ALICE, store=self.store, now="2036-09-28T12:00:00Z")
                # The unchanged question remains visible, but its pre-mutation
                # generation is not authority to save the in-flight old answer.
                current = apm.get_journal_event(ALICE, event_id=parent_id, store=self.store)
                self.assertIsNotNone(current)
                self.assertEqual(current["generation"], generation)
                with self.assertRaises(apm.NotFoundError):
                    self.event("You prefer violet tea.", "late-" + action, role="assistant", origin="derived", parent_source_id=parent["source_id"], parent_source_version=1)
                # Replaying the old prompt cannot refresh its captured epoch.
                self.event("What tea do I prefer?", parent_id, occurred_at="2026-10-02T10:00:00Z")
                self.assertEqual(apm.get_journal_event(ALICE, event_id=parent_id, store=self.store)["generation"], generation)
                fresh = self.event("New question.", "fresh-" + action)
                self.assertEqual(self.event("A new answer.", "fresh-answer-" + action, role="assistant", origin="derived", parent_source_id=fresh["source_id"], parent_source_version=1)["status"], "recorded")
                self.assertEqual(apm.record_turn(BOB, event_id="bob-answer-" + action, text="Independent reply.", occurred_at=NOW, role="assistant", origin="derived", parent_source_id=bob["source_id"], parent_source_version=1, policy=POLICY, store=self.store)["status"], "recorded")

    def test_no_dropped_negation_condition_or_speaker(self):
        for text, fragment in (("I do not like coffee.", "like coffee"), ("If I move, I prefer tea.", "I prefer tea"), ("Alice said I prefer tea.", "I prefer tea")):
            event = self.event(text, text)
            start = text.index(fragment)
            with self.assertRaises(apm.ValidationError):
                apm.remember_from_evidence(ALICE, source_id=event["source_id"], source_version=1, start=start, end=start + len(fragment), store=self.store)
        _, claim = self.fact("I do not like coffee.", "full-negative")
        self.assertIn("not", apm.build_core(ALICE, store=self.store)["text"])
        self.assertEqual(apm.evidence_for_claim(ALICE, claim["claim_id"], store=self.store)[0]["quote"], "I do not like coffee.")

    def test_confirm_required_and_restricted_never_enter_core(self):
        self.store.set_memory_mode(ALICE, "confirm_required")
        _, claim = self.fact()
        self.assertEqual(claim["state"], "candidate")
        self.assertEqual(apm.build_core(ALICE, store=self.store)["text"], "")
        self.store.set_memory_mode(ALICE, "auto_insight")
        event, claim = self.fact("I prefer coffee.", "second")
        self.store.correct_claim(ALICE, claim["claim_id"], content=b"I prefer coffee.", explanation="private", source_ids=[event["source_id"]], sensitivity="restricted")
        self.assertEqual(apm.build_core(ALICE, store=self.store)["text"], "")

    def test_edit_revokes_old_claim_and_projection(self):
        event, claim = self.fact()
        self.run_period()
        changed = self.event("I prefer water.")
        self.assertEqual(changed["version"], 2)
        self.assertEqual(apm.build_core(ALICE, store=self.store)["text"], "")
        self.assertEqual(valid_projections(ALICE, store=self.store), [])
        with self.assertRaises(apm.NotFoundError):
            apm.remember_from_evidence(ALICE, source_id=event["source_id"], source_version=1, start=0, end=13, store=self.store)
        self.assertEqual(apm.evidence_for_claim(ALICE, claim["claim_id"], store=self.store), [])

    def test_correction_updates_projection_without_false_exact_source(self):
        event, claim = self.fact()
        self.run_period()
        self.store.correct_claim(ALICE, claim["claim_id"], content=b"I prefer water.", explanation="owner correction", source_ids=[event["source_id"]])
        self.assertEqual(valid_projections(ALICE, store=self.store), [])
        self.assertIn("water", self.run_period()["projection"]["text"])
        self.assertEqual(apm.evidence_for_claim(ALICE, claim["claim_id"], store=self.store), [])
        self.assertNotIn("tea", apm.build_core(ALICE, store=self.store)["text"])

    def test_atomic_source_correction_erases_old_and_new_originals(self):
        old, claim = self.fact()
        text = "Исправь договорённость: I prefer water."
        new = self.event(text, "correction")
        begin = text.index("I prefer")
        result = apm.correct_from_evidence(ALICE, claim["claim_id"], source_id=new["source_id"], source_version=1, start=begin, end=len(text), store=self.store)
        self.assertGreater(result["version"], claim["version"])
        self.assertEqual(apm.evidence_for_claim(ALICE, claim["claim_id"], store=self.store)[0]["quote"], "I prefer water.")
        self.event("Historical original changed.")
        self.assertIn("water", apm.build_core(ALICE, store=self.store)["text"])
        self.store.erase_claim(ALICE, claim["claim_id"])
        originals = " ".join(i["text"] for i in apm.query_journal(ALICE, store=self.store, include_text=True)["items"])
        self.assertNotIn("tea", originals)
        self.assertNotIn("water", originals)
        self.assertEqual(self.event(text, "correction")["status"], "forgotten")

    def test_forget_redacts_only_target_span_and_blocks_replay(self):
        text = "I prefer tea. I prefer tea. I prefer water."
        event = self.event(text)
        first = apm.remember_from_evidence(ALICE, source_id=event["source_id"], source_version=1, start=0, end=13, store=self.store)
        second = apm.remember_from_evidence(ALICE, source_id=event["source_id"], source_version=1, start=14, end=27, store=self.store)
        self.run_period()
        self.store.erase_claim(ALICE, first["claim_id"])
        actual = apm.query_journal(ALICE, store=self.store, include_text=True)["items"][0]["text"]
        self.assertEqual(actual[:13], " " * 13)
        self.assertEqual(actual[14:], text[14:])
        self.assertEqual(apm.evidence_for_claim(ALICE, second["claim_id"], store=self.store)[0]["quote"], "I prefer tea.")
        self.assertEqual(self.event(text)["status"], "forgotten")
        self.assertEqual(valid_projections(ALICE, store=self.store), [])

    def test_forget_scrubs_all_retained_versions_and_trusted_bound_flow(self):
        event, claim = self.fact()
        self.event("I prefer tea.", session_id="next")
        self.store.erase_claim(ALICE, claim["claim_id"])
        with self.store._tx() as conn:
            tenant = self.store._ensure_principal_on(conn, ALICE)
            for row in conn.execute("SELECT * FROM memory_source_versions WHERE source_id=?", (event["source_id"],)):
                self.assertNotIn(b"tea", self.store._decrypt_version(conn, tenant, event["source_id"], row)[0])
        event2, claim2 = self.fact("I prefer water.", "water")
        current = apm.get_authorized_claim(ALICE, claim2["claim_id"], include_content=True, store=self.store)
        from agent_personal_memory.validation import content_hash_for
        self.store.erase_bound_claims(ALICE, [{"claim_id": claim2["claim_id"], "version": current["version"], "claim_version_id": current["claim_version_id"], "content_hash": content_hash_for(current["content"])}])
        self.assertEqual(self.event("I prefer water.", "water")["status"], "forgotten")

    def test_correction_cannot_borrow_another_claims_evidence(self):
        _, first = self.fact()
        source, second = self.fact("I prefer water.", "water")
        with self.assertRaises(apm.ValidationError):
            apm.correct_from_evidence(ALICE, first["claim_id"], source_id=source["source_id"], source_version=1, start=0, end=15, store=self.store)
        self.assertEqual(apm.get_authorized_claim(ALICE, first["claim_id"], include_content=True, store=self.store)["content"], b"I prefer tea.")
        self.store.erase_claim(ALICE, first["claim_id"])
        self.assertEqual(apm.evidence_for_claim(ALICE, second["claim_id"], store=self.store)[0]["quote"], "I prefer water.")

    def test_forget_maps_changed_punctuation_and_preserves_unrelated_statement(self):
        old = "I prefer tea. I prefer water."
        event = self.event(old)
        tea = apm.remember_from_evidence(ALICE, source_id=event["source_id"], source_version=1, start=0, end=13, store=self.store)
        edited = "I prefer tea! I prefer water."
        event = self.event(edited)
        water = apm.remember_from_evidence(ALICE, source_id=event["source_id"], source_version=2, start=14, end=len(edited), store=self.store)
        self.store.erase_claim(ALICE, tea["claim_id"])
        with self.store._tx() as conn:
            tenant = self.store._ensure_principal_on(conn, ALICE)
            for row in conn.execute("SELECT * FROM memory_source_versions WHERE source_id=?", (event["source_id"],)):
                snapshot = self.store._decrypt_version(conn, tenant, event["source_id"], row)[0]
                self.assertNotIn(b"tea", snapshot)
                self.assertIn(b"I prefer water.", snapshot)
        self.assertEqual(apm.evidence_for_claim(ALICE, water["claim_id"], store=self.store)[0]["quote"], "I prefer water.")

    def test_forget_removes_relocated_statement_in_all_versions(self):
        unrelated = "The unrelated bicycle belongs to my neighbour."
        old = "I prefer tea. " + unrelated
        source = self.event(old)
        claim = apm.remember_from_evidence(ALICE, source_id=source["source_id"], source_version=1, start=0, end=13, store=self.store)
        self.event(unrelated + " I prefer tea.")
        self.store.erase_claim(ALICE, claim["claim_id"])
        with self.store._tx() as conn:
            tenant = self.store._ensure_principal_on(conn, ALICE)
            for row in conn.execute("SELECT * FROM memory_source_versions WHERE source_id=?", (source["source_id"],)):
                snapshot = self.store._decrypt_version(conn, tenant, source["source_id"], row)[0]
                self.assertNotIn(b"tea", snapshot)
                self.assertIn(unrelated.encode(), snapshot)

    def test_forget_ambiguous_move_redacts_changes_and_invalidates_their_claim(self):
        unrelated = "The unrelated bicycle belongs to my neighbour."
        source = self.event("I prefer tea. " + unrelated)
        old = apm.remember_from_evidence(ALICE, source_id=source["source_id"], source_version=1, start=0, end=13, store=self.store)
        changed = unrelated + " I have switched to green tea."
        self.event(changed)
        keep = apm.remember_from_evidence(ALICE, source_id=source["source_id"], source_version=2, start=0, end=len(unrelated), store=self.store)
        removed = apm.remember_from_evidence(ALICE, source_id=source["source_id"], source_version=2, start=len(unrelated) + 1, end=len(changed), store=self.store)
        self.store.erase_claim(ALICE, old["claim_id"])
        current = apm.query_journal(ALICE, store=self.store, include_text=True)["items"][0]["text"]
        self.assertNotIn("tea", current)
        self.assertEqual(apm.evidence_for_claim(ALICE, keep["claim_id"], store=self.store)[0]["quote"], unrelated)
        with self.assertRaises(apm.NotFoundError):
            apm.get_authorized_claim(ALICE, removed["claim_id"], store=self.store)

    def test_raw_source_only_and_mixed_bound_erasure_are_atomic(self):
        raw = self.event("Unclassified original message.", "raw")
        source = self.store.get_source(ALICE, raw["source_id"])
        target = {k: source[k] for k in ("source_id", "version", "source_version_id", "content_hash")}
        self.store.erase_bound_sources(ALICE, [target])
        self.assertEqual(self.event("Unclassified original message.", "raw")["status"], "forgotten")
        _, claim = self.fact()
        current = apm.get_authorized_claim(ALICE, claim["claim_id"], include_content=True, store=self.store)
        from agent_personal_memory.validation import content_hash_for
        ctarget = {k: current[k] for k in ("claim_id", "version", "claim_version_id")}
        ctarget["content_hash"] = content_hash_for(current["content"])
        raw = self.event("Second unclassified message.", "raw-2")
        source = self.store.get_source(ALICE, raw["source_id"])
        target = {k: source[k] for k in ("source_id", "version", "source_version_id", "content_hash")}
        self.event("Changed raw message.", "raw-2")
        with self.assertRaises(apm.ConflictError):
            self.store.erase_bound_memory(ALICE, claim_targets=[ctarget], source_targets=[target])
        self.assertEqual(apm.get_authorized_claim(ALICE, claim["claim_id"], store=self.store)["state"], "confirmed")
        source = self.store.get_source(ALICE, raw["source_id"])
        target = {k: source[k] for k in ("source_id", "version", "source_version_id", "content_hash")}
        with self.assertRaises(apm.ConflictError):
            self.store.erase_bound_sources(BOB, [target])
        self.store.erase_bound_memory(ALICE, claim_targets=[ctarget], source_targets=[target])
        self.assertEqual(apm.build_core(ALICE, store=self.store)["text"], "")
        self.assertEqual(self.event("Changed raw message.", "raw-2")["status"], "forgotten")

    def test_old_event_beyond_500_is_searchable_and_pagination_complete(self):
        self.event("Old agreement with Ivan: meet by the bridge.", "old", occurred_at="2026-01-01T12:00:00Z")
        for i in range(505):
            self.event(f"Unrelated event number {i}.", f"item-{i}")
        result = apm.recall(ALICE, query="Ivan", store=self.store, include_journal=True)
        self.assertTrue(any("bridge" in i["content"] for i in result["items"]))
        cursor, count = None, 0
        while True:
            page = apm.query_journal(ALICE, store=self.store, limit=101, cursor=cursor)
            count += len(page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(count, 506)
        self.assertEqual(apm.recall(ALICE, query="unknown platypus", store=self.store)["items"], [])

    def test_budget_and_explicit_original_opening(self):
        self.fact()
        normal = apm.recall(ALICE, query="tea", store=self.store)
        self.assertFalse(any(i["layer"] == "journal" for i in normal["items"]))
        for budget in (1, 5, 100):
            result = apm.recall(ALICE, query="tea", token_budget=budget, store=self.store)
            self.assertLessEqual(result["budgets"]["tokens_estimated"], budget)
            result = apm.assemble_context(ALICE, memory_store=self.store, token_budget=budget, include_core=True)
            self.assertLessEqual(result["budgets"]["tokens_estimated"], budget)

    def test_period_boundaries_hierarchy_and_pending_progress(self):
        self.fact()
        self.event("Boundary event.", "boundary", occurred_at="2026-09-30T21:00:00Z")
        self.assertEqual(bounds("day", "2026-10-01", "Europe/Moscow")[:2], ("2026-09-30T21:00:00Z", "2026-10-01T21:00:00Z"))
        for _ in range(20):
            todo = apm.pending_periods(ALICE, store=self.store, timezone="Europe/Moscow", limit=1)
            if not todo:
                break
            apm.consolidate_period(ALICE, store=self.store, timezone="Europe/Moscow", **todo[0])
        self.assertEqual(todo, [])
        projections = valid_projections(ALICE, store=self.store)
        self.assertEqual({p["period"] for p in projections}, {"day", "week", "month", "year"})
        for p in projections:
            self.assertEqual(len(p["evidence"]["sources"]), 2)
            if p["period"] != "day":
                self.assertTrue(p["children"])
        empty = self.run_period(start="2026-09-29")["projection"]
        self.assertEqual(empty["coverage"]["event_count"], 0)
        self.assertEqual(empty["coverage"]["missing_days"], 1)
        apm.mark_capture_complete(ALICE, day="2026-09-29", store=self.store)
        empty = self.run_period(start="2026-09-29")["projection"]
        self.assertEqual(empty["coverage"]["missing_days"], 0)
        self.assertEqual(empty["coverage"]["known_empty_days"], ["2026-09-29"])
        self.event("Late event.", "late-empty", occurred_at="2026-09-29T12:00:00Z")
        late = self.run_period(start="2026-09-29")["projection"]
        self.assertEqual(late["coverage"]["known_empty_days"], [])

    def test_projection_read_reuses_one_transaction_snapshot(self):
        for day in ("2026-09-01", "2026-09-02", "2026-10-01"):
            self.event("I prefer tea.", day, occurred_at=day + "T10:00:00Z")
        for _ in range(20):
            todo = apm.pending_periods(ALICE, store=self.store, limit=4)
            if not todo:
                break
            for period in todo:
                apm.consolidate_period(ALICE, store=self.store, **period)
        from agent_personal_memory import consolidation as c
        with patch.object(c, "current_claims_on", wraps=c.current_claims_on) as facts, patch.object(c, "journal_rows_on", wraps=c.journal_rows_on) as rows, patch.object(c, "event_on", wraps=c.event_on) as originals:
            projections = c.valid_projections(ALICE, store=self.store)
            self.assertGreater(len(projections), 4)
            self.assertEqual(facts.call_count, 1)
            self.assertEqual(rows.call_count, 1)
            self.assertEqual(originals.call_count, 0)
        self.event("A late event.", "late", occurred_at="2026-09-01T12:00:00Z")
        self.assertLess(len(c.valid_projections(ALICE, store=self.store)), len(projections))

    def test_adapter_cache_usage_and_no_repeat_failed_charge(self):
        self.fact()
        calls = []
        def adapter(request):
            calls.append(request)
            nested = self.run_period(adapter=adapter)
            self.assertEqual(nested["reason"], "in_progress")
            return {"selected_claim_ids": [f["claim_id"] for f in request["facts"]], "usage": {"input_tokens": 10, "output_tokens": 10}}
        self.assertEqual(self.run_period(adapter=adapter)["status"], "completed")
        self.assertEqual(self.run_period(adapter=adapter)["status"], "cached")
        self.assertEqual(len(calls), 1)
        self.event("new event", "second")
        def failure(request):
            calls.append(request)
            raise TimeoutError("private error must not persist")
        self.assertEqual(self.run_period(adapter=failure)["status"], "pending")
        self.assertEqual(self.run_period(adapter=failure)["reason"], "uncertain_provider_outcome")
        self.assertEqual(len(calls), 2)

    def test_lost_lease_never_admits_a_second_paid_call(self):
        self.fact()
        from agent_personal_memory import consolidation as c
        original_encode = c.encode
        calls, interleaved = [], []
        def provider(request):
            calls.append(request)
            return {"selected_claim_ids": [f["claim_id"] for f in request["facts"]], "usage": {"input_tokens": 10, "output_tokens": 10}}
        def paused_request(value):
            if isinstance(value, dict) and "contract" in value and not interleaved:
                interleaved.append(True)
                with self.store._tx() as conn:
                    conn.execute("UPDATE memory_jobs SET lease_until=0")
                self.assertEqual(self.run_period(adapter=provider)["status"], "completed")
            return original_encode(value)
        with patch.object(c, "encode", side_effect=paused_request):
            result = self.run_period(adapter=provider)
        self.assertEqual(result["reason"], "admission_lost")
        self.assertEqual(result["usage"]["calls"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.run_period(adapter=provider)["status"], "cached")

    def test_optional_extractor_is_candidate_only_and_caches_batch(self):
        event = self.event("Ivan will bring the bicycle on Friday.", "arbitrary-agreement")
        calls = []
        def extractor(request):
            calls.append(request)
            source = request["events"][0]
            return {"selected_claim_ids": [], "proposals": [{"source_id": source["source_id"], "source_version": source["version"], "start": 0, "end": len(source["text"]), "claim_kind": "decision"}], "usage": {"input_tokens": 10, "output_tokens": 10}}
        result = self.run_period(adapter=extractor)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["projection"]["candidates"][0]["state"], "candidate")
        self.assertEqual(apm.build_core(ALICE, store=self.store)["text"], "")
        self.assertEqual(self.run_period(adapter=extractor)["status"], "cached")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(apm.query_claims(ALICE, states=["candidate"], store=self.store)), 1)

    def test_budget_rejection_unknown_usage_and_stale_result(self):
        self.fact()
        called = []
        def adapter(request):
            called.append(request)
            return {}
        self.assertEqual(self.run_period(adapter=adapter, budget=apm.ConsolidationBudget(max_input_tokens=1))["status"], "budget_exhausted")
        self.assertFalse(called)
        self.assertEqual(self.run_period(adapter=adapter)["status"], "pending")
        self.assertEqual(self.run_period(adapter=adapter)["reason"], "uncertain_provider_outcome")
        self.assertEqual(len(called), 1)
        self.event("I prefer coffee.")
        def stale(request):
            self.event("I prefer water.")
            return {"selected_claim_ids": [], "usage": {"input_tokens": 1, "output_tokens": 1}}
        self.assertEqual(self.run_period(adapter=stale)["status"], "stale")
        self.assertEqual(valid_projections(ALICE, store=self.store), [])

    def test_relations_acl_current_version_and_markdown(self):
        text = "Ivan repairs bicycles. Ivan meets me tomorrow."
        event = self.event(text)
        a = apm.remember_from_evidence(ALICE, source_id=event["source_id"], source_version=1, start=0, end=22, store=self.store)
        b = apm.remember_from_evidence(ALICE, source_id=event["source_id"], source_version=1, start=23, end=len(text), store=self.store)
        kwargs = dict(from_claim_id=a["claim_id"], to_claim_id=b["claim_id"], relation="related_to", source_id=event["source_id"], source_version=1, start=0, end=len(text), store=self.store)
        apm.link_memories(ALICE, **kwargs)
        with self.assertRaises(apm.NotFoundError):
            apm.link_memories(BOB, **kwargs)
        found = apm.recall(ALICE, query="bicycles", store=self.store)["items"]
        self.assertTrue(any(i["layer"] == "related_claim" for i in found))
        self.assertIn("Source: [[", apm.export_markdown(ALICE, store=self.store))
        self.store.erase_claim(ALICE, b["claim_id"])
        self.assertFalse(any(i["layer"] == "related_claim" for i in apm.recall(ALICE, query="bicycles", store=self.store)["items"]))

    def test_migration_and_encrypted_projection_storage(self):
        event, _ = self.fact("A very unique private memory phrase.")
        self.run_period()
        with self.store._connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertNotIn(b"unique private memory", Path(self.store.db_path).read_bytes())
        self.assertNotIn(b"unique private memory", Path(self.store.key_path).read_bytes())
        # Minimal old-v5 database with its existing encrypted sources intact.
        with self.store._tx() as conn:
            for name in ("memory_relations", "memory_evidence", "memory_jobs", "memory_projections", "memory_journal"):
                conn.execute(f"DROP TABLE {name}")
            conn.execute("UPDATE memory_schema SET version=5")
        reopened = apm.PersonalMemoryStore(self.tmp.name)
        self.assertEqual(reopened.get_source(ALICE, event["source_id"])["snapshot"], b"A very unique private memory phrase.")
        with reopened._connect() as conn:
            self.assertEqual(conn.execute("SELECT version FROM memory_schema").fetchone()[0], 6)


if __name__ == "__main__":
    unittest.main()

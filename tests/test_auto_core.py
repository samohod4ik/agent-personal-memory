"""Automatic core promotion is an explicitly enabled host policy."""
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest import TestCase

import agent_personal_memory as apm
from agent_personal_memory.consolidation import valid_projections


BRIEF = "Во всех чатах я предпочитаю краткие ответы."
DETAILED = "Во всех чатах я предпочитаю подробные ответы."


class AutoCoreTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = apm.PersonalMemoryStore(data_dir=directory.name)
        self.actor = apm.Actor("bb", "test-owner")
        self.day = datetime.now(timezone.utc).date() - timedelta(days=3)
        self.calls = 0

    def record(self, event_id, text, days=0, minute=0, **kwargs):
        day = self.day + timedelta(days=days)
        kwargs.setdefault("whole_owner_turn", True)
        return apm.record_turn(
            self.actor, event_id=event_id, text=text,
            occurred_at=f"{day.isoformat()}T12:{minute:02}:00Z",
            store=self.store, policy=apm.CapturePolicy(enabled=True), **kwargs,
        )

    def judge(self, request):
        self.calls += 1
        return {
            "selected_claim_ids": [],
            "proposals": [{
                "source_id": event["source_id"], "source_version": event["version"],
                "start": 0, "end": len(event["text"]),
            } for event in request["events"] if event["role"] == "user"],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    def run_day(self, days=0, enabled=True, adapter=None):
        return apm.consolidate_period(
            self.actor, period="day", start=(self.day + timedelta(days=days)).isoformat(),
            store=self.store, adapter=adapter if adapter is not None else self.judge,
            budget=apm.ConsolidationBudget(max_input_tokens=65536), auto_core=enabled,
        )

    def test_replay_caches_against_own_change_and_keeps_judged_input(self):
        source = self.record("brief", BRIEF)
        result = self.run_day()
        projection = result["projection"]
        self.assertEqual(projection["evidence"]["claims"], [])
        self.assertEqual(projection["claim_ids"], [])
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], f"- {BRIEF}")
        claim, = apm.build_core(self.actor, store=self.store)["items"]
        self.assertEqual(claim["claim_kind"], "preference")
        self.assertEqual(claim["evidence"][0]["source_id"], source["source_id"])
        self.assertEqual(projection["candidates"][0]["version"], claim["version"])
        self.assertEqual(self.run_day()["status"], "cached")
        self.assertEqual(len(valid_projections(self.actor, store=self.store)), 1)
        self.assertEqual(self.calls, 1)

    def test_newer_opposite_turn_hides_old_claim_then_replaces_it(self):
        self.record("brief", BRIEF)
        self.run_day()
        self.record("detailed", DETAILED, days=1)
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], "")
        self.run_day(days=1)
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], f"- {DETAILED}")
        self.assertEqual(self.run_day(days=1)["status"], "cached")
        self.assertEqual(self.calls, 2)

    def test_newer_identical_turn_rebinds_to_new_evidence_only_after_judgment(self):
        self.record("brief", BRIEF)
        self.run_day()
        new_source = self.record("repeat", BRIEF, days=1)
        self.assertEqual(apm.build_core(self.actor, store=self.store)["items"], [])
        self.run_day(days=1)
        item, = apm.build_core(self.actor, store=self.store)["items"]
        self.assertEqual(item["evidence"][0]["source_id"], new_source["source_id"])

    def test_older_turn_and_same_timestamp_contradiction_cannot_promote(self):
        self.record("brief", BRIEF)
        self.record("detailed", DETAILED, days=1)
        self.run_day()
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], "")
        self.record("contested", BRIEF, days=1)
        self.run_day(days=1)
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], "")

    def test_manual_replacement_mode_disables_auto_promotion(self):
        self.record("brief", BRIEF)
        self.store.set_memory_mode(self.actor, "confirm_required")
        self.assertEqual(self.run_day()["projection"]["candidates"][0]["state"], "candidate")
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], "")

    def test_generic_library_default_remains_candidate_only(self):
        self.record("brief", BRIEF)
        self.assertEqual(self.run_day(enabled=False)["projection"]["candidates"][0]["state"], "candidate")
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], "")

    def test_legacy_turn_without_complete_capture_attestation_is_history_only(self):
        self.record("legacy", BRIEF, whole_owner_turn=False)
        self.run_day()
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], "")
        self.record("verified", DETAILED, days=1)
        self.run_day(days=1)
        self.record("legacy-later", BRIEF, days=2, whole_owner_turn=False)
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], f"- {DETAILED}")

    def test_qualified_statement_and_attached_turn_are_not_admitted(self):
        self.record("qualified", f"Для этого проекта. {BRIEF}")
        self.record("attached", BRIEF, minute=1, attachments=["context"])
        result = self.run_day()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], "")

    def test_attached_turn_cannot_suppress_an_eligible_owner_preference(self):
        self.record("brief", BRIEF)
        self.run_day()
        self.record("attachment", DETAILED, days=1, attachments=["context-present"])
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], f"- {BRIEF}")
        self.run_day(days=1)
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], f"- {BRIEF}")

    def test_edit_during_judge_keeps_batch_unconfirmed(self):
        self.record("brief", BRIEF)

        def edit(request):
            self.record("brief", "Synthetic correction.")
            return self.judge(request)

        self.assertEqual(self.run_day(adapter=edit)["status"], "stale")
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], "")

    def test_identical_later_source_does_not_revalidate_erased_proof(self):
        source = self.record("brief", BRIEF)
        self.run_day()
        self.record("repeated", BRIEF, days=1)
        self.store.erase_source(self.actor, source["source_id"])
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], "")
        self.assertEqual(self.record("brief", BRIEF)["status"], "forgotten")

    def test_expired_proof_does_not_reappear_from_identical_newer_source(self):
        source = self.record("brief", BRIEF)
        self.run_day()
        self.record("repeated", BRIEF, days=1)
        with self.store._tx() as conn:
            conn.execute("UPDATE memory_journal SET expires_at=? WHERE source_id=?",
                         ("2000-01-01T00:00:00Z", source["source_id"]))
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], "")

    def test_untyped_manual_claims_block_automatic_conflicts(self):
        manual = "I prefer long answers in all chats."
        source = self.record("manual", manual, days=-1)
        result = apm.remember_from_evidence(
            self.actor, source_id=source["source_id"], source_version=source["version"],
            start=0, end=len(manual), store=self.store,
        )
        self.assertEqual(result["state"], "confirmed")
        self.record("brief", BRIEF)
        self.assertEqual(self.run_day()["projection"]["candidates"][0]["state"], "candidate")
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], f"- {manual}")

    def test_manually_confirmed_claim_hides_earlier_automatic_claim(self):
        self.record("brief", BRIEF)
        self.run_day()
        manual = "Keep your explanations as long and detailed as possible."
        source = self.record("manual", manual, days=1)
        apm.remember_from_evidence(
            self.actor, source_id=source["source_id"], source_version=source["version"],
            start=0, end=len(manual), store=self.store,
        )
        self.assertEqual(apm.build_core(self.actor, store=self.store)["text"], f"- {manual}")

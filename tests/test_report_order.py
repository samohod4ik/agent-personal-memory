"""The gated BB report-order policy uses versioned accepted sources."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import agent_personal_memory as apm
from agent_personal_memory import consolidation
from agent_personal_memory.query import get_authorized_claim
from agent_personal_memory.store import NotFoundError


COMMAND = "For failed test reports, put the reproduction command before the diagnosis."
CAUSE = "For failed test reports, put the diagnosis before the reproduction command."


class ReportOrderTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = apm.PersonalMemoryStore(directory.name)
        self.actor = apm.Actor("bb", "dev-owner")
        self.day = datetime.now(timezone.utc).date() - timedelta(days=12)

    def capture(self, identity, day, text="Вынеси команду запуска наверх.", *, actor=None, minute=0, **overrides):
        args = {
            "whole_owner_turn": True, "report_order_enabled": True,
            "report_order_scope": "personal", "report_order_task_id": identity,
        }
        args.update(overrides)
        return apm.record_turn(
            actor or self.actor, event_id=identity, text=text,
            occurred_at=f"{(self.day + timedelta(days=day)).isoformat()}T12:{minute:02}:00Z",
            store=self.store, policy=apm.CapturePolicy(enabled=True), **args,
        )

    def judge(self, day, kind="command_first", *, callback=None, allowed_only=True, enabled=True):
        def adapter(request):
            if callback is not None:
                callback()
            return {
                "selected_claim_ids": [],
                "report_order_observations": [{
                    "source_id": row["source_id"], "source_version": row["version"],
                    "start": 0, "end": len(row["text"]), "kind": kind,
                } for row in request["events"] if not allowed_only or row.get("report_order_eligible")],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        return apm.consolidate_period(
            self.actor, period="day", start=(self.day + timedelta(days=day)).isoformat(),
            store=self.store, adapter=adapter, report_order_enabled=enabled,
        )

    def check_value(self, expected):
        core = apm.build_core(self.actor, store=self.store, report_order_enabled=True)
        recall = apm.recall(self.actor, store=self.store, query="reproduction command diagnosis failed test",
                            report_order_enabled=True)
        self.assertEqual(core["items"], recall["core"]["items"])
        self.assertEqual([item["content"] for item in core["items"]], [expected] if expected else [])
        self.assertEqual([item["content"] for item in recall["items"]], [expected] if expected else [])
        return core["items"]

    def learn(self):
        first = self.capture("task-1", 0)
        self.assertEqual(self.judge(0)["status"], "completed")
        self.check_value(None)
        second = self.capture("task-2", 1, "Покажи, как получить это падение, уже в первой строке.")
        self.assertEqual(self.judge(1)["status"], "completed")
        return first, second

    def test_two_tasks_produce_a_normalized_versioned_claim_after_restart(self):
        sources = self.learn()
        self.store = apm.PersonalMemoryStore(self.store.data_dir)
        item, = self.check_value(COMMAND)
        self.assertEqual(item["state"], "confirmed")
        self.assertEqual(item["claim_kind"], "preference")
        self.assertEqual(
            {(row["source_id"], row["source_version"]) for row in item["evidence"]},
            {(row["source_id"], row["version"]) for row in sources},
        )
        self.assertFalse(apm.build_core(self.actor, store=self.store)["items"])
        self.assertFalse(apm.recall(self.actor, query="test report", store=self.store)["items"])
        self.assertFalse(apm.build_core(apm.Actor("bb", "foreign"), store=self.store,
                                        report_order_enabled=True)["items"])
        with self.assertRaises(NotFoundError):
            get_authorized_claim(self.actor, item["claim_id"], store=self.store)
        spans = apm.evidence_for_claim(self.actor, item["claim_id"], store=self.store,
                                       report_order_enabled=True)
        self.assertEqual(len(spans), 2)
        self.assertTrue(all(row["role"] == "user" and row["origin"] == "direct"
                            and row["end"] - row["start"] == len(row["quote"]) for row in spans))

    def test_exceptions_opposite_series_replacement_and_retraction(self):
        self.learn()
        self.capture("local", 2, "Только сейчас покажи сначала причину, а команду после неё.")
        self.assertEqual(self.judge(2, "cause_first")["status"], "completed")
        self.check_value(COMMAND)
        self.capture("task-3", 3, "Сначала объясни причину, потом дай команду.")
        self.judge(3, "cause_first")
        self.check_value(COMMAND)
        self.capture("task-4", 4, "В этом отчёте объяснение должно стоять перед запуском теста.")
        self.judge(4, "cause_first")
        self.check_value(CAUSE)
        self.capture("correction", 5, "For failed-test reports, now put the command first.")
        self.check_value(COMMAND)
        self.capture("retraction", 6, "Больше не ставь команду первой в отчётах об упавших тестах.")
        self.check_value(None)
        self.assertEqual(self.judge(2, "command_first")["status"], "cached")
        self.check_value(None)
        self.capture("explicit", 7, "В отчётах об упавших тестах теперь начинай с причины.")
        item, = self.check_value(CAUSE)
        self.assertEqual(len(item["evidence"]), 1)

    def test_replay_private_unknown_author_and_same_task_do_not_add_votes(self):
        first = self.capture("first", 0)
        self.assertEqual(self.capture("first", 0)["status"], "replayed")
        self.judge(0)
        self.capture("same-task", 1, report_order_task_id="first")
        self.judge(1)
        self.capture("private", 2, report_order_scope="project_private")
        self.capture("unknown-author", 3, whole_owner_turn=False)
        self.capture("foreign", 4, actor=apm.Actor("bb", "other-owner"))
        self.capture("echo", 5, "«Вынеси команду запуска наверх» — это цитата.")
        self.judge(2, allowed_only=False)
        self.judge(3, allowed_only=False)
        self.judge(5, allowed_only=False)
        self.check_value(None)
        self.assertEqual(first["version"], 1)

    def test_late_adapter_and_pre_retraction_source_cannot_reactivate(self):
        self.learn()
        self.capture("queued-old-task", 2)
        self.capture("late-arrival", 3)
        self.judge(2, callback=lambda: self.capture(
            "retraction", 4, "Больше не ставь команду первой в отчётах об упавших тестах."
        ))
        self.judge(3)
        self.check_value(None)

    def test_expired_evidence_is_hidden_before_pruning_and_after_restart(self):
        first, _ = self.learn()
        with self.store._tx() as conn:
            conn.execute("UPDATE memory_journal SET expires_at='2000-01-01T00:00:00Z' WHERE source_id=?",
                         (first["source_id"],))
        self.store = apm.PersonalMemoryStore(self.store.data_dir)
        self.check_value(None)

    def test_unrelated_confirmed_preference_remains_and_manual_slot_value_wins(self):
        self.learn()
        manual = "For reports about failed tests, lead with the diagnosis and follow with the command."
        for identity, text in (("unrelated", "Use ordinary metric units."), ("manual", manual)):
            recorded = self.capture(identity, 5, text, report_order_enabled=False)
            value = apm.remember_from_evidence(
                self.actor, source_id=recorded["source_id"], source_version=recorded["version"],
                start=0, end=len(text), claim_kind="preference", store=self.store,
            )
            self.assertEqual(value["state"], "confirmed")
        items = apm.build_core(self.actor, store=self.store, report_order_enabled=True)["items"]
        self.assertCountEqual([row["content"] for row in items], ["Use ordinary metric units.", manual])

    def test_explicit_changes_hide_older_same_property_manual_preference_only_in_pilot(self):
        text = "For reports about failed tests, lead with the diagnosis and follow with the command."
        recorded = self.capture("manual", 0, text, report_order_enabled=False)
        manual = apm.remember_from_evidence(
            self.actor, source_id=recorded["source_id"], source_version=recorded["version"],
            start=0, end=len(text), claim_kind="preference", store=self.store,
        )
        with self.store._tx() as conn:
            conn.execute("UPDATE memory_claim_versions SET created_at=? WHERE claim_id=?",
                         (f"{self.day.isoformat()}T12:01:00Z", manual["claim_id"]))
        self.check_value(text)
        summary = apm.consolidate_period(self.actor, period="day", start=self.day.isoformat(), store=self.store)
        self.assertIn(text, summary["projection"]["text"])
        self.capture("replace", 1, "For failed-test reports, now put the command first.")
        self.check_value(COMMAND)
        self.capture("revoke", 2, "Stop putting the command first in failed-test reports.")
        self.check_value(None)
        core = apm.build_core(self.actor, store=self.store)
        self.assertEqual([row["content"] for row in core["items"]], [text])
        preserved = get_authorized_claim(self.actor, manual["claim_id"], store=self.store)
        self.assertEqual(preserved["version"], manual["version"])

    def test_short_corrections_require_attested_report_context(self):
        self.learn()
        self.capture("unauthorized", 3, "Больше не ставь команду первой.")
        self.check_value(COMMAND)
        self.judge(3, "cause_first")
        self.check_value(COMMAND)
        self.capture("revoke", 4, "Больше не ставь команду первой.",
                     report_order_context="failed_test_report")
        self.check_value(None)
        self.capture("replace", 5, "Теперь начинай с причины.",
                     report_order_context="failed_test_report")
        self.check_value(CAUSE)
        self.capture("ambiguous", 6, "Надо пересмотреть порядок.",
                     report_order_context="failed_test_report")
        self.check_value(None)
        self.capture("restore", 7, "Теперь начинай с причины.",
                     report_order_context="failed_test_report")
        self.check_value(CAUSE)
        self.capture("missing-first", 8, "For failed-test reports, now put the command.")
        self.check_value(None)

    def test_forget_claim_invalidates_sources_and_old_votes(self):
        sources = self.learn()
        item, = self.check_value(COMMAND)
        self.store.erase_claim(self.actor, item["claim_id"])
        self.check_value(None)
        self.assertEqual(self.judge(0)["status"], "completed")
        self.check_value(None)
        self.assertEqual(self.capture("task-1", 0)["status"], "forgotten")
        self.assertEqual(len(sources), 2)

    def test_v6_store_migrates_and_can_learn(self):
        with self.store._tx() as conn:
            conn.execute("DROP TRIGGER clear_redacted_report_order_votes")
            for name in ("slots", "votes", "attempts"):
                conn.execute(f"DROP TABLE memory_report_order_{name}")
            conn.execute("UPDATE memory_schema SET version=6")
        self.store = apm.PersonalMemoryStore(self.store.data_dir)
        self.learn()
        self.check_value(COMMAND)

    def test_opposing_series_in_one_batch_matches_chronological_periods(self):
        orders = ["command_first", "command_first", "cause_first", "cause_first", "command_first"]
        days = [0, 1, 2, 3, 4]
        for day, kind in zip(days, orders):
            self.capture(f"separate-{day}", day, kind)
            self.assertEqual(self.judge(day, kind)["status"], "completed")
        self.check_value(CAUSE)

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = apm.PersonalMemoryStore(directory.name)
        for minute, kind in enumerate(orders):
            self.capture(f"batched-{minute}", 0, kind, minute=minute)

        def both_directions(request):
            return {
                "report_order_observations": [{
                    "source_id": event["source_id"], "source_version": event["version"],
                    "start": 0, "end": len(event["text"]), "kind": event["text"],
                } for event in request["events"]],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        self.assertEqual(apm.consolidate_period(
            self.actor, period="day", start=self.day.isoformat(), store=self.store,
            adapter=both_directions, report_order_enabled=True,
        )["status"], "completed")
        self.check_value(CAUSE)

    def test_capture_and_explicit_change_commit_together(self):
        self.learn()
        statement = "For failed-test reports, now put the diagnosis first."
        with patch("agent_personal_memory.report_order.record_correction_on", side_effect=RuntimeError("simulated power loss")):
            with self.assertRaisesRegex(RuntimeError, "power loss"):
                self.capture("atomic", 5, statement)
        self.store = apm.PersonalMemoryStore(self.store.data_dir)
        self.check_value(COMMAND)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.capture("atomic", 5, statement), range(2)))
        self.assertEqual(sorted(r["status"] for r in results), ["recorded", "replayed"])
        self.check_value(CAUSE)

    def test_flag_off_does_not_capture_or_learn_pilot_evidence(self):
        for day in range(2):
            self.capture(str(day), day, report_order_enabled=False)
            self.judge(day, enabled=False, allowed_only=False)
        self.check_value(None)

    def test_lost_receipt_closes_at_deadline_and_later_period_can_run(self):
        self.capture("lost", 0)
        self.capture("later", 1)
        self.capture("after", 2)
        start = self.day.isoformat()
        calls = 0

        def lose_receipt(request):
            nonlocal calls
            calls += 1
            raise TimeoutError

        def lost():
            return apm.consolidate_period(
                self.actor, period="day", start=start, store=self.store, adapter=lose_receipt,
                report_order_enabled=True, report_order_provider=("codex", "test-fixture"),
            )

        initial = lost()
        self.assertEqual(initial["model_attempt"]["status"], "uncertain")
        self.store = apm.PersonalMemoryStore(self.store.data_dir)
        pending = lost()
        self.assertEqual(pending["reason"], "uncertain_provider_outcome")
        self.assertEqual(initial["attempt_id"], pending["attempt_id"])
        periods = apm.pending_periods(self.actor, store=self.store, closed_only=True, limit=1,
                                     report_order_enabled=True)
        self.assertEqual(periods[0]["start"], (self.day + timedelta(days=1)).isoformat())
        self.judge(1)
        future = consolidation.time.time() + 86401
        with patch.object(consolidation.time, "time", return_value=future):
            outcome = lost()
            self.assertEqual(outcome["status"], "uncredited")
            self.assertEqual(outcome["reason"], "provider_receipt_deadline")
            self.assertEqual(outcome["eligible_source_count"], 1)
            self.assertEqual(outcome["provider_cost"], "unknown")
            self.assertEqual(outcome["attempt_id"], pending["attempt_id"])
            self.assertEqual(outcome["judge"], {"cli": "codex", "model": "test-fixture"})
            self.capture("late-closed-day", 0)
            self.assertEqual(lost()["status"], "uncredited")
        self.assertEqual(calls, 1)
        self.store = apm.PersonalMemoryStore(self.store.data_dir)
        self.assertEqual(apm.report_order_uncredited_attempts(self.actor, store=self.store), [outcome])
        other = apm.Actor("bb", "other")
        self.assertEqual(apm.report_order_uncredited_attempts(other, store=self.store), [])
        self.judge(2)
        self.check_value(COMMAND)

"""Public scheduling and fail-safe consolidation contracts."""
import tempfile
import unittest
from unittest.mock import patch

import agent_personal_memory as apm
from agent_personal_memory.consolidation import valid_projections


class ConsolidationRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = apm.PersonalMemoryStore(directory.name)
        self.actor = apm.Actor("test", "recovery")
        self.budget = apm.ConsolidationBudget(max_input_tokens=65536)
        self.calls = 0

    def event(self, stamp):
        return apm.record_turn(
            self.actor, event_id=stamp, text="The owner prefers green tea.",
            occurred_at=stamp, store=self.store,
            policy=apm.CapturePolicy(True, retention_days=3650),
        )

    def consolidate(self, **kwargs):
        return apm.consolidate_period(
            self.actor, period="day", start="2025-12-31", store=self.store,
            budget=self.budget, **kwargs,
        )

    def judge(self, _):
        self.calls += 1
        return {
            "selected_claim_ids": [], "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    def test_large_day_finishes_in_bounded_batches_across_restarts(self):
        expected = set()
        for i in range(100):
            event = apm.record_turn(self.actor, event_id=f"large-{i}",
                text=f"Synthetic statement {i:03d}: " + "x" * 290 + ".",
                occurred_at="2025-12-31T10:00:00Z", store=self.store,
                policy=apm.CapturePolicy(True, retention_days=3650))
            expected.add((event["source_id"], event["version"]))
        seen = []
        def judge(request):
            seen.extend((event["source_id"], event["version"]) for event in request["events"])
            return {"selected_claim_ids": [], "proposals": [], "reviews": [],
                    "usage": {"input_tokens": 1, "output_tokens": 1}}
        budget = apm.ConsolidationBudget(max_calls=2, max_input_tokens=262144,
                                         max_output_tokens=32768, max_seconds=90)
        outcomes = []
        for _ in range(10):
            before = len(seen)
            result = apm.consolidate_period(self.actor, period="day", start="2025-12-31",
                                            store=self.store, adapter=judge, budget=budget)
            outcomes.append(result["status"])
            self.assertLessEqual(result.get("usage", {}).get("calls", 0), 2)
            if result["status"] == "completed": break
            self.assertGreater(len(seen), before)
            self.store = apm.PersonalMemoryStore(self.store.data_dir)
        self.assertEqual(outcomes[-1], "completed")
        self.assertEqual(len(set(seen) & expected), len(expected))
        self.assertEqual(len(seen), len(expected))
        self.assertEqual(result["projection"]["coverage"]["event_count"], 100)
        self.assertFalse(result["projection"]["coverage"]["review_input_truncated"])

    def test_unpassable_single_original_never_invokes_judge_on_repeated_ticks(self):
        apm.record_turn(self.actor, event_id="too-large", text="x" * 50000,
            occurred_at="2025-12-31T10:00:00Z", store=self.store,
            policy=apm.CapturePolicy(True, retention_days=3650))
        budget = apm.ConsolidationBudget(max_calls=2, max_input_tokens=8000, max_output_tokens=32768, max_seconds=90)
        for _ in range(2):
            result = apm.consolidate_period(self.actor, period="day", start="2025-12-31", store=self.store, adapter=self.judge, budget=budget)
            self.assertEqual(result["status"], "budget_exhausted")
            self.assertEqual(result["reason"], "event_too_large")
            self.assertEqual(self.calls, 0)

    def test_completed_batches_survive_budget_change(self):
        for i in range(30):
            apm.record_turn(self.actor, event_id=f"budget-{i}", text=f"Synthetic {i}. " + "x" * 500,
                occurred_at="2025-12-31T10:00:00Z", store=self.store,
                policy=apm.CapturePolicy(True, retention_days=3650))
        seen = []
        def judge(request):
            seen.extend(event['source_id'] for event in request['events'])
            return {'selected_claim_ids': [], 'proposals': [], 'usage': {'input_tokens': 1, 'output_tokens': 1}}
        first = apm.ConsolidationBudget(max_calls=1, max_input_tokens=8000, max_output_tokens=32768, max_seconds=90)
        second = apm.ConsolidationBudget(max_calls=20, max_input_tokens=262144, max_output_tokens=32768, max_seconds=90)
        self.assertEqual(apm.consolidate_period(self.actor, period='day', start='2025-12-31', store=self.store, adapter=judge, budget=first)['status'], 'pending')
        self.store = apm.PersonalMemoryStore(self.store.data_dir)
        result = apm.consolidate_period(self.actor, period='day', start='2025-12-31', store=self.store, adapter=judge, budget=second)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(len(seen), 30)
        self.assertEqual(len(set(seen)), 30)

    def test_preflight_progress_survives_deadlines_and_restart(self):
        from agent_personal_memory import consolidation as module
        for i in range(20):
            apm.record_turn(self.actor, event_id=f'preflight-{i}', text=f'Synthetic {i}.',
                occurred_at='2025-12-31T10:00:00Z', store=self.store,
                policy=apm.CapturePolicy(True, retention_days=3650))
        budget = apm.ConsolidationBudget(max_calls=2, max_input_tokens=65536, max_output_tokens=32768, max_seconds=2)
        statuses = []
        for _ in range(20):
            counter = [0]
            def clock():
                counter[0] += 1
                return counter[0] * .2
            with patch.object(module.time, 'monotonic', side_effect=clock):
                result = apm.consolidate_period(self.actor, period='day', start='2025-12-31',
                    store=self.store, adapter=self.judge, budget=budget)
            statuses.append(result.get('reason', result['status']))
            if result['status'] == 'completed':
                break
            self.store = apm.PersonalMemoryStore(self.store.data_dir)
        self.assertIn('preflight_pending', statuses)
        self.assertEqual(statuses[-1], 'completed', statuses)
        self.assertEqual(self.calls, 1)

    def test_second_batch_failure_keeps_first_checkpoint_without_retrying_paid_call(self):
        for i in range(40):
            apm.record_turn(self.actor, event_id=f"failure-{i}", text=f"Synthetic {i}. " + "x" * 400,
                occurred_at="2025-12-31T10:00:00Z", store=self.store,
                policy=apm.CapturePolicy(True, retention_days=3650))
        calls = []
        def judge(request):
            calls.append([event["source_id"] for event in request["events"]])
            if len(calls) == 2: raise TimeoutError("synthetic")
            return {"selected_claim_ids": [], "proposals": [], "usage": {"input_tokens": 1, "output_tokens": 1}}
        budget = apm.ConsolidationBudget(max_calls=2, max_input_tokens=262144, max_output_tokens=32768, max_seconds=90)
        result = apm.consolidate_period(self.actor, period="day", start="2025-12-31", store=self.store, adapter=judge, budget=budget)
        self.assertEqual(result["status"], "pending")
        self.assertNotIn("projection", result)
        self.assertEqual(len(calls), 2)
        self.store = apm.PersonalMemoryStore(self.store.data_dir)
        result = apm.consolidate_period(self.actor, period="day", start="2025-12-31", store=self.store, adapter=judge, budget=budget)
        self.assertEqual(result["reason"], "uncertain_provider_outcome")
        self.assertEqual(len(calls), 2)

    def test_edit_during_provider_never_commits_old_checkpoint(self):
        self.event("2025-12-31T10:00:00Z")
        def editing_provider(request):
            apm.record_turn(self.actor, event_id="2025-12-31T10:00:00Z",
                text="The owner now prefers coffee.", occurred_at="2025-12-31T10:00:00Z",
                store=self.store, policy=apm.CapturePolicy(True, retention_days=3650))
            return {"selected_claim_ids": [], "proposals": [], "usage": {"input_tokens": 1, "output_tokens": 1}}
        result = self.consolidate(adapter=editing_provider)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(valid_projections(self.actor, store=self.store), [])
        retry = self.consolidate(adapter=self.judge)
        self.assertEqual(retry["status"], "completed", retry)

    def test_checkpoint_from_old_version_cannot_resurrect_after_forget(self):
        events = []
        for i in range(25):
            events.append(apm.record_turn(self.actor, event_id=f'forget-{i}', text=f'Synthetic fact {i}.',
                occurred_at='2025-12-31T10:00:00Z', store=self.store,
                policy=apm.CapturePolicy(True, retention_days=3650)))
        seen = []
        def judge(request):
            seen.extend(event['source_id'] for event in request['events'])
            return {'selected_claim_ids': [], 'proposals': [], 'usage': {'input_tokens': 1, 'output_tokens': 1}}
        budget = apm.ConsolidationBudget(max_calls=1, max_input_tokens=65536, max_output_tokens=32768, max_seconds=90)
        result = apm.consolidate_period(self.actor, period='day', start='2025-12-31', store=self.store, adapter=judge, budget=budget)
        self.assertEqual(result['status'], 'pending')
        removed = events[0]['source_id']
        self.store.erase_source(self.actor, removed)
        self.store = apm.PersonalMemoryStore(self.store.data_dir)
        seen.clear()
        for _ in range(5):
            result = apm.consolidate_period(self.actor, period='day', start='2025-12-31', store=self.store, adapter=judge, budget=budget)
            if result['status'] == 'completed':
                break
        self.assertEqual(result['status'], 'completed', result)
        self.assertNotIn(removed, seen)
        self.assertNotIn(removed, str(result['projection']))

    def test_boundaries_filter_before_limit(self):
        for stamp in ("2025-12-31T10:00:00Z", "2026-01-01T10:00:00Z"):
            self.event(stamp)
        as_of = "2026-01-01T11:00:00Z"
        self.assertEqual(apm.pending_periods(
            self.actor, store=self.store, closed_only=True, as_of=as_of, limit=1,
        ), [{"period": "day", "start": "2025-12-31"}])
        self.consolidate()
        self.assertEqual(apm.pending_periods(
            self.actor, store=self.store, closed_only=True, as_of=as_of, limit=1,
        ), [])
        self.assertEqual(apm.pending_periods(
            self.actor, store=self.store, closed_only=True,
            as_of="2026-01-05T00:00:00Z", limit=1,
        ), [{"period": "day", "start": "2026-01-01"}])

    def test_closed_day_uses_local_timezone_across_dst(self):
        self.event("2025-03-09T20:00:00Z")
        self.assertEqual(apm.pending_periods(
            self.actor, store=self.store, timezone="America/New_York",
            closed_only=True, as_of="2025-03-10T03:59:59Z", limit=1,
        ), [])
        self.assertEqual(apm.pending_periods(
            self.actor, store=self.store, timezone="America/New_York",
            closed_only=True, as_of="2025-03-10T04:00:00Z", limit=1,
        ), [{"period": "day", "start": "2025-03-09"}])

    def test_offline_recovery_retains_uncertain_admission_across_restart(self):
        self.event("2025-12-31T10:00:00Z")
        def timeout(_):
            self.calls += 1
            raise TimeoutError("synthetic failure")
        attempted = self.consolidate(adapter=timeout)
        self.assertEqual(attempted["usage"]["calls"], 1)
        self.store = apm.PersonalMemoryStore(self.store.data_dir)
        self.assertEqual(self.consolidate()["reason"], "uncertain_provider_outcome")
        recovered = self.consolidate(recover_extractive=True)
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["projection"]["method"], "extractive")
        self.assertEqual(recovered["usage"]["calls"], 0)
        self.assertEqual(recovered["model_attempt"]["status"], "uncertain")
        self.assertEqual(self.consolidate(adapter=self.judge, upgrade_extractive=True)["status"], "cached")
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(valid_projections(self.actor, store=self.store)), 1)

    def test_extractive_projection_is_explicitly_eligible_for_first_judgment(self):
        self.event("2025-12-31T10:00:00Z")
        self.assertEqual(self.consolidate()["projection"]["method"], "extractive")
        self.assertEqual(self.consolidate(adapter=self.judge)["status"], "cached")
        self.assertEqual(self.calls, 0)
        self.assertEqual(apm.pending_periods(
            self.actor, store=self.store, include_extractive=True, limit=1,
        ), [{"period": "day", "start": "2025-12-31"}])
        upgraded = self.consolidate(adapter=self.judge, upgrade_extractive=True)
        self.assertEqual(upgraded["status"], "completed")
        self.assertEqual(upgraded["projection"]["method"], "judged")
        self.assertEqual(self.consolidate(adapter=self.judge, upgrade_extractive=True)["status"], "cached")
        self.assertEqual(self.calls, 1)

    def test_changed_input_is_not_consumed_by_recovery(self):
        self.event("2025-12-31T10:00:00Z")
        def timeout(_):
            self.calls += 1
            raise TimeoutError()
        self.assertEqual(self.consolidate(adapter=timeout)["status"], "pending")
        self.event("2025-12-31T11:00:00Z")
        self.assertEqual(self.consolidate(recover_extractive=True)["reason"], "recovery_input_changed")
        self.assertEqual(self.consolidate(adapter=self.judge)["status"], "completed")
        self.assertEqual(self.calls, 2)

    def test_provable_prelaunch_failure_releases_only_its_own_admission(self):
        self.event("2025-12-31T10:00:00Z")
        def not_started(_):
            raise apm.ConsolidationNotStarted()
        result = self.consolidate(adapter=not_started)
        self.assertEqual(result["reason"], "provider_not_started")
        self.assertEqual(result["usage"]["calls"], 0)
        self.assertEqual(self.consolidate(adapter=self.judge)["status"], "completed")
        self.assertEqual(self.calls, 1)

    def test_validation_failure_has_a_safe_stage_and_does_not_authorize_retry(self):
        self.event("2025-12-31T10:00:00Z")
        def invalid(_):
            self.calls += 1
            return {"selected_claim_ids": [["invalid type"]], "usage": {"input_tokens": 1, "output_tokens": 1}}
        result = self.consolidate(adapter=invalid)
        self.assertEqual(result["model_attempt"], {"status": "rejected", "validation_stage": "selected_claim_ids"})
        self.assertEqual(self.consolidate(adapter=self.judge)["reason"], "uncertain_provider_outcome")
        self.assertEqual(self.calls, 1)

    def test_recovering_a_failed_upgrade_refreshes_the_projection_receipt(self):
        self.event("2025-12-31T10:00:00Z")
        self.assertEqual(self.consolidate()["projection"]["model_attempt"]["status"], "not_started")
        def timeout(_):
            self.calls += 1
            raise TimeoutError()
        self.consolidate(adapter=timeout, upgrade_extractive=True)
        self.assertIn({"period": "day", "start": "2025-12-31"}, apm.pending_periods(
            self.actor, store=self.store, limit=1, include_extractive=True))
        self.assertEqual(self.consolidate(adapter=self.judge, upgrade_extractive=True)["reason"],
                         "uncertain_provider_outcome")
        refreshed = self.consolidate(recover_extractive=True)
        self.assertEqual(refreshed["projection"]["model_attempt"]["status"], "uncertain")
        self.assertNotIn({"period": "day", "start": "2025-12-31"}, apm.pending_periods(
            self.actor, store=self.store, include_extractive=True))
        again = self.consolidate(recover_extractive=True)
        self.assertEqual(again["status"], "cached")
        self.assertEqual(self.calls, 1)

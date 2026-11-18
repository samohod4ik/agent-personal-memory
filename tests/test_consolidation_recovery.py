"""Public scheduling and fail-safe consolidation contracts."""
import tempfile
import unittest

import agent_personal_memory as apm
from agent_personal_memory.consolidation import valid_projections


class ConsolidationRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = apm.PersonalMemoryStore(directory)
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

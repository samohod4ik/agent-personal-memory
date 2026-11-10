"""Host evidence and bounded review tests; no claims of real-model quality."""
import tempfile
import unittest
from unittest.mock import patch

import agent_personal_memory as apm


ACTOR = apm.Actor("test", "dreamer")
ENV = {"project": "example", "host": "fixture", "revision": "r1"}
POLICY = apm.CapturePolicy(True, 3650)
NOW = "2026-10-01T10:00:00Z"


class DreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = apm.PersonalMemoryStore(self.temp.name)
        self.parent = self.turn("Deploy the fix.", "request")

    def tearDown(self):
        self.temp.cleanup()

    def turn(self, text, event_id, **kwargs):
        return apm.record_turn(ACTOR, event_id=event_id, text=text,
            occurred_at=NOW, policy=POLICY, store=self.store, **kwargs)

    def receipt(self, event_id="result", phase="result", outcome="failed", **kwargs):
        params = dict(event_id=event_id, operation_id="deploy-1", tool_name="deploy",
            environment=ENV, phase=phase, outcome=outcome, summary="Controller result.",
            occurred_at=NOW, parent_source_id=self.parent["source_id"],
            parent_source_version=self.parent["version"], policy=POLICY, store=self.store)
        params.update(kwargs)
        return apm.record_action_receipt(ACTOR, **params)

    def run_period(self, adapter=None, **kwargs):
        return apm.consolidate_period(ACTOR, period="day", start="2026-10-01",
            store=self.store, adapter=adapter, **kwargs)

    def ref(self, event):
        return {"source_id": event["source_id"], "source_version": event["version"]}

    def lesson(self, failure, success):
        return dict(kind="lesson", environment=ENV,
            condition="When the controller rejects installation.",
            failed_approach="Repeating the same rejected call.",
            successful_action="Inspect the actual process context before retrying.",
            evidence=[self.ref(failure), self.ref(success)])

    def provider(self, review, calls=None):
        def adapter(request):
            if calls is not None:
                calls.append(request)
            return {"selected_claim_ids": [], "reviews": [review],
                "usage": {"input_tokens": 1, "output_tokens": 1}}
        return adapter

    def test_false_done_does_not_override_failed_tool(self):
        failure = self.receipt()
        reply = self.turn("Done, production is updated.", "done", role="assistant", origin="derived",
            parent_source_id=self.parent["source_id"], parent_source_version=1)
        review = dict(kind="assessment", environment=ENV, verdict="contradicted",
            summary="The assistant reports completion; the controller failed. Delivery is unconfirmed.",
            evidence=[self.ref(failure), self.ref(reply)])
        calls = []
        result = self.run_period(self.provider(review, calls))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["projection"]["reviews"][0]["state"], "candidate")
        self.assertEqual(result["projection"]["reviews"][1]["verdict"], "failure_observed")
        self.assertEqual(apm.build_core(ACTOR, store=self.store)["text"], "")
        self.assertEqual({e["role"] for e in calls[0]["events"]}, {"user", "assistant", "tool"})
        self.assertEqual(self.run_period(self.provider(review, calls))["status"], "cached")
        self.assertEqual(len(calls), 1)

    def test_successful_call_does_not_prove_effect_and_absence_is_not_failure(self):
        self.receipt("request-only", "requested", "unknown", operation_id="other")
        result = self.receipt(outcome="succeeded")
        output = self.run_period()["projection"]["reviews"]
        self.assertEqual({r["verdict"] for r in output}, {"insufficient_evidence", "call_succeeded_effect_unconfirmed"})
        # Changing the input invalidates the cached projection before a review.
        self.turn("Check the actual installed version.", "followup")
        review = dict(kind="assessment", environment=ENV, verdict="supported",
            summary="Deployment completed.", evidence=[self.ref(result)])
        self.assertEqual(self.run_period(self.provider(review))["status"], "pending")

    def test_conflicting_observations_stay_explicit(self):
        self.receipt("observed-yes", "observed", "succeeded")
        self.receipt("observed-no", "observed", "failed")
        result = self.run_period()
        self.assertEqual(result["projection"]["reviews"][0]["verdict"], "conflicting_observations")

    def test_operation_ids_are_scoped_to_execution_and_receipt_identity_is_immutable(self):
        self.receipt()
        other = self.turn("A different request.", "other-request")
        self.receipt("other-result", outcome="succeeded", parent_source_id=other["source_id"], parent_source_version=1)
        self.assertEqual(len(self.run_period()["projection"]["reviews"]), 2)
        with self.assertRaises(apm.ConflictError):
            self.receipt(environment={**ENV, "revision": "r2"})

    def test_conflicting_results_are_not_silently_resolved(self):
        self.receipt()
        self.receipt("contrary-result", outcome="succeeded")
        self.assertEqual(self.run_period()["projection"]["reviews"][0]["verdict"], "conflicting_observations")

    def test_adapter_prose_secret_is_rejected(self):
        failure = self.receipt()
        review = dict(kind="assessment", environment=ENV, verdict="contradicted",
            summary="password is very-private", evidence=[self.ref(failure)])
        self.assertEqual(self.run_period(self.provider(review))["status"], "pending")

    def test_numeric_source_uuid_is_not_misclassified_as_a_secret(self):
        from agent_personal_memory.dreaming import validate_reviews
        identity = "12345678-1234-4234-8234-123456789012"
        source = dict(source_id=identity, version=1, action_receipt=dict(
            environment=ENV, phase="result", outcome="failed"))
        review = dict(kind="assessment", environment=ENV, verdict="contradicted",
            summary="Controller failed; delivery remains unconfirmed.",
            evidence=[dict(source_id=identity, source_version=1)])
        validated = validate_reviews([review], [source])
        self.assertEqual(validated[0]["state"], "candidate")
        self.assertEqual(validated[0]["evidence"], review["evidence"])

    def test_lesson_survives_new_session_but_not_new_environment(self):
        failure = self.receipt()
        success = self.receipt("effect", "observed", "succeeded", operation_id="deploy-2")
        result = self.run_period(self.provider(self.lesson(failure, success)))
        self.assertEqual(result["status"], "completed")
        reopened = apm.PersonalMemoryStore(self.temp.name)
        items = apm.recall_reviews(ACTOR, environment=ENV, query="process context", store=reopened)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["state"], "candidate")
        self.assertEqual(apm.recall_reviews(ACTOR, environment={**ENV, "revision": "r2"}, store=reopened)["items"], [])
        self.assertEqual(apm.recall_reviews(apm.Actor("test", "other"), environment=ENV, store=reopened)["items"], [])

    def test_lesson_survives_many_operations_and_default_output_and_recall_limits(self):
        failure = self.receipt()
        success = self.receipt("effect", "observed", "succeeded", operation_id="repaired")
        for i in range(7):
            self.receipt("extra-" + str(i), outcome="succeeded", operation_id="other-" + str(i))
        result = self.run_period(self.provider(self.lesson(failure, success)),
            budget=apm.ConsolidationBudget(max_input_tokens=32000))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["projection"]["coverage"]["reviews_truncated"])
        self.assertEqual(result["projection"]["reviews"][0]["kind"], "lesson")
        reopened = apm.PersonalMemoryStore(self.temp.name)
        recalled = apm.recall_reviews(ACTOR, environment=ENV, store=reopened)["items"]
        self.assertEqual(recalled[0]["kind"], "lesson")
        self.assertEqual(recalled[0]["state"], "candidate")

    def test_free_receipt_review_does_not_open_ordinary_originals(self):
        huge = self.turn("A" * 60000, "large-original")
        receipt = self.receipt()
        opened = []
        decrypt = self.store._decrypt_version
        def tracked(conn, tenant, source_id, row):
            opened.append(source_id)
            return decrypt(conn, tenant, source_id, row)
        with patch.object(self.store, "_decrypt_version", side_effect=tracked):
            result = self.run_period()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(opened, [receipt["source_id"]])
        self.assertNotIn(huge["source_id"], opened)
        self.assertEqual(result["projection"]["coverage"]["review_scope"], "host_receipts_only")

    def test_provider_input_is_admitted_before_decryption_and_partial_is_explicit(self):
        huge = self.turn("A" * 60000, "large-original")
        self.receipt()
        opened, requests = [], []
        decrypt = self.store._decrypt_version
        def tracked(conn, tenant, source_id, row):
            opened.append(source_id)
            return decrypt(conn, tenant, source_id, row)
        def provider(request):
            requests.append(request)
            return {"selected_claim_ids": [], "usage": {"input_tokens": 1, "output_tokens": 1}}
        with patch.object(self.store, "_decrypt_version", side_effect=tracked):
            result = self.run_period(provider)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn(huge["source_id"], opened)
        self.assertEqual(requests[0]["coverage"]["review_events_omitted"], 1)
        self.assertTrue(result["projection"]["coverage"]["review_input_truncated"])
        self.assertEqual(result["projection"]["reviews"][0]["verdict"], "insufficient_evidence")
        self.assertEqual(result["projection"]["reviews"][0]["coverage"], "partial_batch")

    def test_lesson_requires_observed_success_and_matching_scope(self):
        failure = self.receipt()
        success = self.receipt("result-ok", "result", "succeeded")
        self.assertEqual(self.run_period(self.provider(self.lesson(failure, success)))["status"], "pending")
        self.receipt("new", "observed", "succeeded", environment={**ENV, "host": "other"})
        foreign = apm.query_journal(ACTOR, store=self.store, include_text=True)["items"]
        foreign = next(e for e in foreign if e.get("action_receipt") and e["action_receipt"]["environment"]["host"] == "other")
        self.assertEqual(self.run_period(self.provider(self.lesson(failure, foreign)))["status"], "pending")

    def test_forget_during_review_cannot_publish_or_replay(self):
        failure = self.receipt()
        success = self.receipt("success", "observed", "succeeded")
        def adapter(request):
            self.store.erase_source(ACTOR, self.parent["source_id"])
            return self.provider(self.lesson(failure, success))(request)
        self.assertEqual(self.run_period(adapter)["status"], "stale")
        self.assertEqual(apm.recall_reviews(ACTOR, environment=ENV, store=self.store)["items"], [])
        self.assertEqual(self.receipt()["status"], "forgotten")
        with self.assertRaises(apm.NotFoundError):
            self.receipt("late-output")

    def test_unrelated_forget_purges_tool_recall_and_invalidates_inflight(self):
        old = self.turn("A private old fact.", "old")
        self.receipt(summary="May include recalled content.")
        self.store.erase_source(ACTOR, old["source_id"])
        self.assertFalse(any(e.get("action_receipt") for e in apm.query_journal(ACTOR, store=self.store)["items"]))
        with self.assertRaises(apm.NotFoundError):
            self.receipt("late")

    def test_adapter_cannot_mutate_evidence_authority(self):
        result = self.receipt(outcome="succeeded")
        def adapter(request):
            tool = next(e for e in request["events"] if e.get("action_receipt"))
            tool["action_receipt"]["phase"] = "observed"
            return self.provider(dict(kind="assessment", environment=ENV, verdict="supported",
                summary="Done.", evidence=[self.ref(result)]))(request)
        self.assertEqual(self.run_period(adapter)["status"], "pending")

    def test_parent_edit_invalidates_existing_reviews(self):
        self.receipt()
        self.run_period()
        self.assertTrue(apm.recall_reviews(ACTOR, environment=ENV, store=self.store)["items"])
        self.turn("Do not deploy; inspect first.", "request")
        self.assertEqual(apm.recall_reviews(ACTOR, environment=ENV, store=self.store)["items"], [])

    def test_receipt_validation_privacy_and_no_paid_admission_over_budget(self):
        with self.assertRaises(apm.ValidationError):
            self.receipt(phase="started", outcome="succeeded")
        with self.assertRaises(apm.ValidationError):
            self.receipt(environment={"project": "example"})
        self.assertEqual(self.receipt(summary="password is very-private")["status"], "skipped")
        self.receipt()
        calls = []
        self.assertEqual(self.run_period(lambda request: calls.append(request),
            budget=apm.ConsolidationBudget(max_input_tokens=1))["status"], "budget_exhausted")
        self.assertFalse(calls)


if __name__ == "__main__":
    unittest.main()

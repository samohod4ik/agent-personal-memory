import tempfile
import unittest

import agent_personal_memory as apm
from agent_personal_memory.confirmation import bind_targets
from agent_personal_memory.store import ConflictError

ALICE = apm.Actor('test', 'alice')
BOB = apm.Actor('test', 'bob')
POLICY = apm.CapturePolicy(True, 3650)


class ErasureImpactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = apm.PersonalMemoryStore(self.tmp.name)
        event = self.event(ALICE, 'fact', 'I like tea.')
        claim = apm.remember_from_evidence(ALICE, source_id=event['source_id'], source_version=1, start=0, end=11, store=self.store)
        self.targets, _ = bind_targets(ALICE, [claim['claim_id']], store=self.store, language='en')

    def event(self, actor, identity, text, **kwargs):
        return apm.record_turn(actor, event_id=identity, text=text, occurred_at='2026-10-01T10:00:00Z', policy=POLICY, store=self.store, **kwargs)

    def erase(self, **kwargs):
        return self.store.erase_bound_memory(ALICE, claim_targets=[t.as_dict() for t in self.targets], **kwargs)

    def test_impact_is_actor_scoped_and_changed_scope_denies_erasure(self):
        self.event(ALICE, 'answer', 'Tea.', role='assistant')
        self.event(BOB, 'answer', 'Coffee.', role='assistant')
        impact = self.store.preview_erasure_impact(ALICE, claim_ids=[self.targets[0].claim_id])
        self.assertEqual(impact.generated_count, 1)
        self.event(ALICE, 'answer2', 'More tea.', role='assistant')
        with self.assertRaises(ConflictError):
            self.erase(require_impact_ack=True, impact_fingerprint=impact.fingerprint)
        self.assertIsNotNone(apm.get_journal_event(ALICE, event_id='answer', store=self.store))
        self.assertIsNotNone(apm.get_journal_event(ALICE, event_id='answer2', store=self.store))
        self.erase(require_impact_ack=True, impact_fingerprint=self.store.preview_erasure_impact(ALICE, claim_ids=[self.targets[0].claim_id]).fingerprint)
        self.assertIsNone(apm.get_journal_event(ALICE, event_id='answer', store=self.store))
        self.assertIsNotNone(apm.get_journal_event(BOB, event_id='answer', store=self.store))

    def test_missing_ack_denies_before_erasure(self):
        self.event(ALICE, 'answer', 'Tea.', role='assistant')
        with self.assertRaises(ConflictError):
            self.erase(require_impact_ack=True)
        self.assertIsNotNone(apm.get_journal_event(ALICE, event_id='answer', store=self.store))

    def test_impact_is_bound_to_legacy_claims_without_evidence(self):
        source = self.event(ALICE, 'legacy-source', 'I prefer coffee.')
        claims = [self.store.create_candidate(
            ALICE, claim_kind='preference', content=f'Legacy candidate {i}'.encode(),
            confidence=0.5, explanation='', sensitivity='normal',
            source_ids=[source['source_id']], valid_from=None, valid_to=None,
            expires_at='2099-01-01T00:00:00Z') for i in (1, 2)]
        first, second = [claim['claim_id'] for claim in claims]
        reviewed = self.store.preview_erasure_impact(ALICE, claim_ids=[first])
        targets, _ = bind_targets(ALICE, [second], store=self.store, language='en')
        with self.assertRaises(ConflictError):
            self.store.erase_bound_claims(ALICE, [target.as_dict() for target in targets],
                                          require_impact_ack=True, impact_fingerprint=reviewed.fingerprint)

    def test_trusted_preview_discloses_and_binds_generated_scope(self):
        from agent_personal_memory.confirmation import (REGISTRY, SessionBinding,
            consume_delete_request, create_delete_preview, reset_registry)
        reset_registry()
        self.addCleanup(reset_registry)
        self.event(ALICE, 'answer', 'Tea.', role='assistant')
        binding = SessionBinding('test', 'alice', 'key', 'session', 'chat')
        preview = create_delete_preview(ALICE, action='forget_personal_memory',
            claim_ids=[self.targets[0].claim_id], store=self.store, session_binding=binding,
            empty_kind='forget_empty', preview_kind='forget_preview')
        sent = []
        REGISTRY.open_preview(preview['request_id'], binding, send=lambda text: sent.append(text) or True)
        self.assertIn('1 generated', sent[0])
        nonce = next(line.split()[-1] for line in sent[0].splitlines() if '/memory_confirm' in line)
        self.event(ALICE, 'new-answer', 'More tea.', role='assistant')
        with self.assertRaises(ConflictError):
            consume_delete_request(REGISTRY.consume_confirm(nonce, binding), store=self.store)
        self.assertIsNotNone(apm.get_journal_event(ALICE, event_id='answer', store=self.store))

    def test_lineage_scope_changes_invalidate_impact(self):
        parent = self.event(ALICE, 'question', 'Question.')
        impact = self.store.preview_erasure_impact(ALICE, claim_ids=[self.targets[0].claim_id])
        self.event(ALICE, 'derived', 'Derived.', origin='derived',
                   parent_source_id=parent['source_id'], parent_source_version=1)
        current = self.store.preview_erasure_impact(ALICE, claim_ids=[self.targets[0].claim_id])
        self.assertGreater(current.lineage_count, impact.lineage_count)
        with self.assertRaises(ConflictError):
            self.erase(require_impact_ack=True, impact_fingerprint=impact.fingerprint)

    def test_editing_a_derived_lineage_source_invalidates_impact(self):
        parent = self.event(ALICE, 'parent', 'Question.')
        options = dict(origin='derived', parent_source_id=parent['source_id'], parent_source_version=1)
        self.event(ALICE, 'derived', 'First.', **options)
        impact = self.store.preview_erasure_impact(ALICE, claim_ids=[self.targets[0].claim_id])
        self.event(ALICE, 'derived', 'Second.', **options)
        with self.assertRaises(ConflictError):
            self.erase(require_impact_ack=True, impact_fingerprint=impact.fingerprint)

    def test_new_claim_supported_by_generated_source_invalidates_impact(self):
        reply = self.event(ALICE, 'reply-dependent', 'Generated answer.', role='assistant')
        impact = self.store.preview_erasure_impact(ALICE, claim_ids=[self.targets[0].claim_id])
        dependent = self.store.create_candidate(
            ALICE, claim_kind='preference', content=b'A supported candidate',
            confidence=0.5, explanation='', sensitivity='normal',
            source_ids=[reply['source_id']], valid_from=None, valid_to=None,
            expires_at='2099-01-01T00:00:00Z')
        with self.assertRaises(ConflictError):
            self.erase(require_impact_ack=True, impact_fingerprint=impact.fingerprint)
        self.assertIsNotNone(apm.get_journal_event(ALICE, event_id='reply-dependent', store=self.store))
        self.assertTrue(apm.query_claims(ALICE, states=['candidate'], store=self.store,
                                         include_content=True))
        self.assertIn(dependent['claim_id'], [r['claim_id'] for r in apm.query_claims(
            ALICE, states=['candidate'], store=self.store)])

    def test_new_claim_supported_by_target_source_invalidates_impact(self):
        impact = self.store.preview_erasure_impact(ALICE, claim_ids=[self.targets[0].claim_id])
        fact = apm.get_journal_event(ALICE, event_id='fact', store=self.store)
        dependent = self.store.create_candidate(
            ALICE, claim_kind='preference', content=b'Another candidate', confidence=0.5,
            explanation='', sensitivity='normal', source_ids=[fact['source_id']],
            valid_from=None, valid_to=None, expires_at='2099-01-01T00:00:00Z')
        with self.assertRaises(ConflictError):
            self.erase(require_impact_ack=True, impact_fingerprint=impact.fingerprint)
        self.assertIn(dependent['claim_id'], [r['claim_id'] for r in apm.query_claims(
            ALICE, states=['candidate'], store=self.store)])
        self.assertIsNotNone(apm.get_journal_event(ALICE, event_id='fact', store=self.store))

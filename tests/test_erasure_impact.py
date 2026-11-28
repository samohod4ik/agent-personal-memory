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

    def test_edit_keeps_unknown_generated_history_but_hides_it_from_recall_and_export(self):
        answer = self.event(ALICE, 'independent-context-answer', 'Synthetic private generated text.', role='assistant')
        derived = self.store.create_candidate(ALICE, claim_kind='preference', content=b'Private synthetic derived statement',
            confidence=.5, explanation='', sensitivity='normal', source_ids=[answer['source_id']],
            valid_from=None, valid_to=None, expires_at='2099-01-01T00:00:00Z')
        self.event(BOB, 'other-answer', 'Other tenant output.', role='assistant')
        self.event(ALICE, 'fact', 'I now prefer coffee.')
        with self.store._tx() as conn:
            tenant = self.store._ensure_principal_on(conn, ALICE)
            row = conn.execute('SELECT deleted_at FROM memory_sources WHERE tenant_id=? AND id=?', (tenant, answer['source_id'])).fetchone()
            self.assertIsNone(row['deleted_at'])
            self.assertIsNotNone(self.store._newest_active_version(conn, tenant, answer['source_id']))
        self.assertNotIn(answer['source_id'], [e['source_id'] for e in apm.query_journal(ALICE, store=self.store, include_text=True)['items']])
        self.store = apm.PersonalMemoryStore(self.tmp.name)
        history = apm.query_journal(ALICE, store=self.store, include_text=True, include_history=True)['items']
        historical = next(e for e in history if e['source_id'] == answer['source_id'])
        self.assertTrue(historical['history_only'])
        self.assertEqual(historical['text'], 'Synthetic private generated text.')
        with self.assertRaises(apm.NotFoundError):
            self.store.get_source(ALICE, answer['source_id'])
        self.assertNotIn(derived['claim_id'], [claim['claim_id'] for claim in apm.query_claims(ALICE, states=['candidate', 'confirmed'], store=self.store)])
        self.assertNotIn('Private synthetic derived statement', str(apm.recall(ALICE, query='Private synthetic derived statement', store=self.store)))
        self.assertNotIn('Synthetic private generated text.', str(self.store.export_owner_data(ALICE)))
        self.assertIsNotNone(apm.get_journal_event(BOB, event_id='other-answer', store=self.store))

    def test_ttl_preserves_history_until_forget_or_own_expiry(self):
        expired = self.event(ALICE, 'expires', 'An old message.')
        other = self.event(ALICE, 'other', 'A fresh message.')
        answer = self.event(ALICE, 'answer-history', 'Fresh generated history.', role='assistant')
        with self.store._tx() as conn:
            conn.execute("UPDATE memory_journal SET expires_at='2000-01-01T00:00:00Z' WHERE source_id=?", (expired['source_id'],))
        self.assertEqual(apm.prune_journal(ALICE, store=self.store)['expired'], 1)
        self.store = apm.PersonalMemoryStore(self.tmp.name)
        history = apm.query_journal(ALICE, store=self.store, include_text=True, include_history=True)['items']
        self.assertTrue(next(e for e in history if e['source_id'] == answer['source_id'])['history_only'])
        self.assertNotIn(answer['source_id'], [e['source_id'] for e in apm.query_journal(ALICE, store=self.store, include_text=True)['items']])
        self.store.erase_source(ALICE, other['source_id'])
        history = apm.query_journal(ALICE, store=self.store, include_text=True, include_history=True)['items']
        self.assertNotIn(answer['source_id'], [e['source_id'] for e in history])

    def test_exact_live_alternative_preserves_claim_after_edit_and_ttl(self):
        first = apm.get_journal_event(ALICE, event_id='fact', store=self.store)
        alternate = self.event(ALICE, 'alternate', 'I like tea.')
        claim_id = self.targets[0].claim_id
        with self.store._tx() as conn:
            tenant = self.store._ensure_principal_on(conn, ALICE)
            latest = self.store._newest_claim_version(conn, tenant, claim_id)
            self.store._insert_support(conn, tenant_id=tenant, claim_id=claim_id, source_ids=[alternate['source_id']], now='2026-10-01T10:00:00Z')
            conn.execute('INSERT INTO memory_evidence VALUES(?,?,?,?,?,?,?)', (tenant, claim_id, latest['version'], alternate['source_id'], 1, 0, 11))
        self.event(ALICE, 'fact', 'I prefer coffee now.')
        self.assertIn(claim_id, [c['claim_id'] for c in apm.query_claims(ALICE, states=['candidate','confirmed'], store=self.store)])
        with self.store._tx() as conn:
            conn.execute("UPDATE memory_journal SET expires_at='2000-01-01T00:00:00Z' WHERE source_id=?", (first['source_id'],))
        apm.prune_journal(ALICE, store=self.store)
        self.store = apm.PersonalMemoryStore(self.tmp.name)
        self.assertIn(claim_id, [c['claim_id'] for c in apm.query_claims(ALICE, states=['candidate','confirmed'], store=self.store)])

    def test_ttl_keeps_claim_only_with_sufficient_live_alternative(self):
        first = apm.get_journal_event(ALICE, event_id='fact', store=self.store)
        alternate = self.event(ALICE, 'ttl-alternative', 'I like tea.')
        claim_id = self.targets[0].claim_id
        with self.store._tx() as conn:
            tenant = self.store._ensure_principal_on(conn, ALICE)
            latest = self.store._newest_claim_version(conn, tenant, claim_id)
            self.store._insert_support(conn, tenant_id=tenant, claim_id=claim_id, source_ids=[alternate['source_id']], now='2026-10-01T10:00:00Z')
            conn.execute('INSERT INTO memory_evidence VALUES(?,?,?,?,?,?,?)', (tenant, claim_id, latest['version'], alternate['source_id'], 1, 0, 11))
            conn.execute("UPDATE memory_journal SET expires_at='2000-01-01T00:00:00Z' WHERE source_id=?", (first['source_id'],))
        apm.prune_journal(ALICE, store=self.store)
        self.store = apm.PersonalMemoryStore(self.tmp.name)
        self.assertIn(claim_id, [c['claim_id'] for c in apm.query_claims(ALICE, states=['candidate','confirmed'], store=self.store)])

    def test_quarantined_history_expires_on_its_own_ttl(self):
        expired = self.event(ALICE, 'old-turn', 'Old question.')
        answer = self.event(ALICE, 'fresh-answer', 'Fresh generated answer.', role='assistant')
        with self.store._tx() as conn:
            conn.execute("UPDATE memory_journal SET expires_at='2000-01-01T00:00:00Z' WHERE source_id=?", (expired['source_id'],))
        apm.prune_journal(ALICE, store=self.store)
        self.assertIn(answer['source_id'], [e['source_id'] for e in apm.query_journal(ALICE, store=self.store, include_history=True)['items']])
        with self.store._tx() as conn:
            conn.execute("UPDATE memory_journal SET expires_at='2000-01-01T00:00:00Z' WHERE source_id=?", (answer['source_id'],))
        apm.prune_journal(ALICE, store=self.store)
        self.store = apm.PersonalMemoryStore(self.tmp.name)
        self.assertNotIn(answer['source_id'], [e['source_id'] for e in apm.query_journal(ALICE, store=self.store, include_history=True)['items']])

    def test_explicit_source_forget_preserves_independent_exact_support(self):
        first = apm.get_journal_event(ALICE, event_id='fact', store=self.store)
        alternate = self.event(ALICE, 'forget-alternative', 'I like tea.')
        generated = self.event(ALICE, 'unknown-answer', 'Unknown output.', role='assistant')
        claim_id = self.targets[0].claim_id
        with self.store._tx() as conn:
            tenant = self.store._ensure_principal_on(conn, ALICE)
            latest = self.store._newest_claim_version(conn, tenant, claim_id)
            self.store._insert_support(conn, tenant_id=tenant, claim_id=claim_id, source_ids=[alternate['source_id']], now='2026-10-01T10:00:00Z')
            conn.execute('INSERT INTO memory_evidence VALUES(?,?,?,?,?,?,?)', (tenant, claim_id, latest['version'], alternate['source_id'], 1, 0, 11))
        self.store.erase_source(ALICE, first['source_id'])
        self.store = apm.PersonalMemoryStore(self.tmp.name)
        self.assertIsNotNone(apm.get_journal_event(ALICE, event_id='forget-alternative', store=self.store))
        self.assertIn(claim_id, [c['claim_id'] for c in apm.query_claims(ALICE, states=['candidate','confirmed'], store=self.store)])
        self.assertIsNone(apm.get_journal_event(ALICE, event_id='unknown-answer', store=self.store))
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

    def test_existing_v6_store_migrates_history_quarantine(self):
        with self.store._tx() as conn:
            conn.execute('DROP TABLE memory_quarantined_sources')
            conn.execute('UPDATE memory_schema SET version=6 WHERE id=1')
        self.store = apm.PersonalMemoryStore(self.tmp.name)
        self.assertIsNotNone(apm.get_journal_event(ALICE, event_id='fact', store=self.store))
        self.event(ALICE, 'answer-after-migration', 'Generated.', role='assistant')
        self.event(ALICE, 'fact', 'Edited original.')
        items = apm.query_journal(ALICE, store=self.store, include_history=True)['items']
        self.assertTrue(any(item.get('history_only') for item in items))

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

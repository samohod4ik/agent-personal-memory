import tempfile
import unittest

import agent_personal_memory as apm

ACTOR = apm.Actor('test', 'reconcile')
POLICY = apm.CapturePolicy(True, 3650)


class JournalReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = apm.PersonalMemoryStore(self.tmp.name)

    def record(self, event_id='event', text='I prefer tea.', **kwargs):
        return apm.record_turn(ACTOR, event_id=event_id, text=text,
            occurred_at=kwargs.pop('occurred_at', '2026-10-01T10:00:00Z'),
            policy=POLICY, store=self.store, **kwargs)

    def test_replay_preserves_original_version_date_and_lineage(self):
        parent = self.record('parent')
        first = self.record('reply', 'A reply.', role='assistant', origin='derived',
                            parent_source_id=parent['source_id'], parent_source_version=1)
        replay = self.record('reply', 'A reply.', role='assistant', origin='derived',
                             occurred_at='2026-10-02T10:00:00Z', reconcile=True)
        self.assertEqual(replay, {**first, 'status': 'replayed'})
        event = next(x for x in apm.query_journal(ACTOR, store=self.store, include_text=True)['items'] if x['source_id'] == first['source_id'])
        self.assertEqual(event['occurred_at'], '2026-10-01T10:00:00Z')
        self.assertEqual(event['parent_source_id'], parent['source_id'])
        self.assertEqual(event['parent_source_version'], 1)

    def test_changed_replay_skips_without_mutation_but_normal_edit_works(self):
        first = self.record()
        for changes in ({'text': 'I prefer coffee.'}, {'role': 'assistant'}, {'origin': 'imported'}, {'session_id': 'other'}):
            self.assertEqual(self.record(reconcile=True, **changes), {'status': 'skipped', 'reason': 'reconcile_existing_event'})
        self.assertEqual(apm.get_journal_event(ACTOR, event_id='event', store=self.store)['version'], first['version'])
        self.assertEqual(self.record(text='I prefer coffee.')['version'], 2)

    def test_reconciliation_inserts_new_event_and_cannot_restore_forgotten(self):
        first = self.record(reconcile=True)
        self.assertEqual(first['status'], 'recorded')
        apm.prune_journal(ACTOR, store=self.store, now='2040-01-01T00:00:00Z')
        self.assertEqual(self.record(reconcile=True)['status'], 'forgotten')

    def test_expired_existing_event_cannot_be_revived(self):
        first = self.record()
        with self.store._tx() as conn:
            conn.execute("UPDATE memory_journal SET expires_at='2020-01-01T00:00:00Z' WHERE source_id=?", (first['source_id'],))
        result = self.record(reconcile=True)
        self.assertNotEqual(result['status'], 'recorded')
        self.assertIsNone(apm.get_journal_event(ACTOR, event_id='event', store=self.store))

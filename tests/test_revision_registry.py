"""Independent registry rejects missing/truncated history before serving data."""
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import agent_personal_memory as apm
from agent_personal_memory.revision_registry import RegistryError
from agent_personal_memory.revision_registry import restore_snapshot


class RevisionRegistryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / 'current'
        self.registry = self.root / 'registry'
        self.actor = apm.Actor('test', 'registry')
        self.store = apm.PersonalMemoryStore(self.data, registry_dir=self.registry, registry_initialize=True)

    def record(self, identity, text):
        return apm.record_turn(self.actor, event_id=identity, text=text, occurred_at='2026-10-01T00:00:00Z',
            policy=apm.CapturePolicy(True, 3650), store=self.store)

    def test_missing_registry_blocks_existing_database_before_read(self):
        self.record('old', 'Synthetic source.')
        shutil.rmtree(self.registry)
        with self.assertRaises(RegistryError):
            apm.PersonalMemoryStore(self.data, registry_dir=self.registry)
        with self.assertRaises(RegistryError):
            apm.PersonalMemoryStore(self.data)

    def test_valid_prefix_truncation_is_detected_by_external_head(self):
        self.record('old', 'Synthetic original.')
        log = self.registry / 'ledger.jsonl'
        prefix = log.read_bytes()
        self.record('old', 'Synthetic edited original.')
        log.write_bytes(prefix)
        with self.assertRaises(RegistryError):
            apm.build_core(self.actor, store=self.store)
        with self.assertRaises(RegistryError):
            apm.PersonalMemoryStore(self.data, registry_dir=self.registry)

    def test_direct_old_image_replacement_is_rejected_before_open(self):
        self.record('old', 'Synthetic old source.')
        candidate = self.root / 'snapshot'
        shutil.copytree(self.data, candidate)
        self.record('old', 'Synthetic newer source.')
        shutil.rmtree(self.data)
        candidate.rename(self.data)
        with self.assertRaises(RegistryError):
            apm.PersonalMemoryStore(self.data, registry_dir=self.registry)

    def test_registry_contains_no_source_text(self):
        self.record('old', 'A synthetic secretless distinctive original.')
        self.assertNotIn(b'A synthetic secretless distinctive original.', (self.registry / 'ledger.jsonl').read_bytes())

    def test_lost_head_or_key_blocks_running_store(self):
        self.record('old', 'Synthetic source.')
        for name in ('head.json', 'key'):
            path = self.registry / name
            saved = path.read_bytes()
            path.unlink()
            with self.assertRaises(RegistryError):
                apm.query_journal(self.actor, store=self.store, include_text=True)
            path.write_bytes(saved)
            path.chmod(0o600)

    def test_pre_enrollment_generic_forget_with_unknown_identity_fails_closed(self):
        from agent_personal_memory.validation import content_hash_for
        data, registry = self.root / 'legacy', self.root / 'legacy-registry'
        legacy = apm.PersonalMemoryStore(data)
        payload = b'Synthetic document.'
        args = dict(source_kind='document', external_ref='legacy-document', content_hash=content_hash_for(payload),
                    snapshot=payload, observed_at='2026-10-01T00:00:00Z', retention_class='standard')
        source = legacy.register_source(self.actor, **args)
        legacy.erase_source(self.actor, source['source_id'])
        protected = apm.PersonalMemoryStore(data, registry_dir=registry, registry_initialize=True)
        with self.assertRaises(apm.NotFoundError):
            protected.register_source(self.actor, **args)

    def test_crash_after_registry_before_db_commit_fails_closed(self):
        original = self.store._registry.append
        def crash(state):
            original(state)
            raise OSError('synthetic crash')
        with patch.object(self.store._registry, 'append', side_effect=crash):
            with self.assertRaises(OSError):
                self.record('unfinished', 'Synthetic value.')
        with self.assertRaises(RegistryError):
            apm.PersonalMemoryStore(self.data, registry_dir=self.registry)

    def test_interrupted_ledger_head_update_fails_closed(self):
        with patch.object(self.store._registry, '_write_head', side_effect=OSError('synthetic failure')):
            with self.assertRaises(RegistryError):
                self.record('unfinished', 'Synthetic value.')
        with self.assertRaises(RegistryError):
            apm.PersonalMemoryStore(self.data, registry_dir=self.registry)

    def test_concurrent_store_instances_serialize_registry_and_db(self):
        second = apm.PersonalMemoryStore(self.data, registry_dir=self.registry)
        def capture(pair):
            store, identity = pair
            return apm.record_turn(self.actor, event_id=identity, text='Synthetic concurrent source.',
                occurred_at='2026-10-01T00:00:00Z', policy=apm.CapturePolicy(True, 3650), store=store)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(capture, [(self.store, 'one'), (second, 'two')]))
        self.assertEqual([result['status'] for result in outcomes], ['recorded', 'recorded'])
        reopened = apm.PersonalMemoryStore(self.data, registry_dir=self.registry)
        self.assertEqual(len(apm.query_journal(self.actor, store=reopened)['items']), 2)

    def test_lost_main_restore_suppresses_stale_and_missing_new_versions(self):
        old = self.record('old', 'Old synthetic value.')
        with self.store._tx() as conn:
            old_key = bytes(conn.execute("SELECT wrapped_dek FROM memory_keys WHERE entity_kind='source' AND entity_id=?", (old['source_id'],)).fetchone()[0])
        candidate = self.root / 'snapshot'
        shutil.copytree(self.data, candidate)
        self.record('old', 'Revised synthetic value.')
        new = self.record('new', 'New synthetic value.')
        self.store.erase_source(self.actor, old['source_id'])
        shutil.rmtree(self.data)
        result = restore_snapshot(candidate, self.registry, self.data)
        self.assertGreaterEqual(result['unavailable_sources'], 1)
        self.assertGreaterEqual(result['unavailable_deletion_receipts'], 1)
        restored = apm.PersonalMemoryStore(self.data, registry_dir=self.registry)
        self.assertNotIn(old_key, (self.data / 'personal_memory.db').read_bytes())
        self.assertIsNone(apm.get_journal_event(self.actor, event_id='old', store=restored))
        self.assertIsNone(apm.get_journal_event(self.actor, event_id='new', store=restored))
        self.assertEqual(apm.record_turn(self.actor, event_id='new', text='New synthetic value.',
            occurred_at='2026-10-01T00:00:00Z', policy=apm.CapturePolicy(True, 3650), store=restored)['status'], 'forgotten')

    def test_restore_refuses_missing_registry_without_creating_output(self):
        self.record('old', 'Synthetic value.')
        candidate = self.root / 'snapshot'
        shutil.copytree(self.data, candidate)
        shutil.rmtree(self.data)
        shutil.rmtree(self.registry)
        with self.assertRaises(RegistryError):
            restore_snapshot(candidate, self.registry, self.data)
        self.assertFalse(self.data.exists())

    def test_restore_preserves_independent_current_source_and_claim(self):
        stable = self.record('stable', 'I prefer green tea.')
        captured_generation = apm.get_journal_event(self.actor, event_id='stable', store=self.store)['generation']
        remembered = apm.remember_from_evidence(self.actor, source_id=stable['source_id'],
            source_version=stable['version'], start=0, end=len('I prefer green tea.'), store=self.store)
        changed = self.record('changed', 'An older separate message.')
        candidate = self.root / 'snapshot'
        shutil.copytree(self.data, candidate)
        self.record('changed', 'A newer separate message.')
        shutil.rmtree(self.data)
        result = restore_snapshot(candidate, self.registry, self.data)
        restored = apm.PersonalMemoryStore(self.data, registry_dir=self.registry)
        self.assertIn('I prefer green tea.', apm.build_core(self.actor, store=restored)['text'])
        self.assertIsNone(apm.get_journal_event(self.actor, event_id='changed', store=restored))
        self.assertGreaterEqual(result['unavailable_sources'], 1)
        with self.assertRaises(apm.ConflictError):
            apm.record_turn(self.actor, event_id='late-assistant', text='Synthetic late answer.',
                role='assistant', origin='derived', expected_capture_generation=captured_generation,
                occurred_at='2026-10-01T00:00:00Z', policy=apm.CapturePolicy(True, 3650), store=restored)

    def test_large_unicode_edit_is_not_recovered_from_old_image(self):
        original = 'ёж🙂' * 160000 + 'synthetic-old-tail'
        self.assertGreater(len(original.encode('utf-8')), 1_000_000)
        self.record('large', original)
        candidate = self.root / 'snapshot'
        shutil.copytree(self.data, candidate)
        self.record('large', original + 'synthetic-new-tail')
        shutil.rmtree(self.data)
        result = restore_snapshot(candidate, self.registry, self.data)
        self.assertEqual(result['unavailable_sources'], 1)
        restored = apm.PersonalMemoryStore(self.data, registry_dir=self.registry)
        self.assertIsNone(apm.get_journal_event(self.actor, event_id='large', store=restored))
        self.assertNotIn('synthetic-old-tail', str(apm.query_journal(self.actor, store=restored, include_text=True)))

    def test_recovery_preserves_untouched_other_tenant_projection(self):
        from agent_personal_memory.consolidation import valid_projections
        other = apm.Actor('test', 'other-tenant')
        apm.record_turn(other, event_id='other', text='Other synthetic source.',
            occurred_at='2026-10-01T00:00:00Z', policy=apm.CapturePolicy(True, 3650), store=self.store)
        apm.consolidate_period(other, period='day', start='2026-10-01', store=self.store)
        self.record('old', 'Synthetic original.')
        before = valid_projections(other, store=self.store)
        candidate = self.root / 'snapshot'
        shutil.copytree(self.data, candidate)
        self.record('old', 'Synthetic revision.')
        shutil.rmtree(self.data)
        restore_snapshot(candidate, self.registry, self.data)
        restored = apm.PersonalMemoryStore(self.data, registry_dir=self.registry)
        self.assertEqual(valid_projections(other, store=restored), before)

    def test_restore_reports_claim_created_after_backup_as_unavailable(self):
        source = self.record('old', 'I prefer green tea.')
        candidate = self.root / 'snapshot'
        shutil.copytree(self.data, candidate)
        apm.remember_from_evidence(self.actor, source_id=source['source_id'], source_version=1,
                                   start=0, end=len('I prefer green tea.'), store=self.store)
        shutil.rmtree(self.data)
        result = restore_snapshot(candidate, self.registry, self.data)
        self.assertEqual(result['unavailable_claims'], 1)
        restored = apm.PersonalMemoryStore(self.data, registry_dir=self.registry)
        self.assertEqual(apm.build_core(self.actor, store=restored)['text'], '')

    def test_restore_never_publishes_evidence_expired_at_restore_time(self):
        from datetime import datetime, timezone
        source = self.record('old', 'I prefer green tea.')
        apm.remember_from_evidence(self.actor, source_id=source['source_id'], source_version=1,
                                   start=0, end=len('I prefer green tea.'), store=self.store)
        candidate = self.root / 'snapshot'
        shutil.copytree(self.data, candidate)
        shutil.rmtree(self.data)
        with patch('agent_personal_memory.revision_registry.datetime') as clock:
            clock.now.return_value = datetime(2040, 1, 1, tzinfo=timezone.utc)
            restore_snapshot(candidate, self.registry, self.data)
        restored = apm.PersonalMemoryStore(self.data, registry_dir=self.registry)
        self.assertIsNone(apm.get_journal_event(self.actor, event_id='old', store=restored))
        self.assertEqual(apm.build_core(self.actor, store=restored)['text'], '')

    def test_restore_never_repeats_paid_uncertain_job(self):
        self.record('old', 'Synthetic original.')
        candidate = self.root / 'snapshot'
        shutil.copytree(self.data, candidate)
        calls = []
        def timed_out(_):
            calls.append(True)
            raise TimeoutError('synthetic')
        first = apm.consolidate_period(self.actor, period='day', start='2026-10-01', store=self.store, adapter=timed_out)
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(len(calls), 1)
        shutil.rmtree(self.data)
        restore_snapshot(candidate, self.registry, self.data)
        restored = apm.PersonalMemoryStore(self.data, registry_dir=self.registry)
        retry = apm.consolidate_period(self.actor, period='day', start='2026-10-01', store=restored, adapter=timed_out)
        self.assertEqual(retry['reason'], 'uncertain_provider_outcome')
        self.assertEqual(len(calls), 1)

    def test_restore_refuses_valid_truncated_prefix_without_output(self):
        self.record('old', 'Synthetic value.')
        candidate = self.root / 'snapshot'
        shutil.copytree(self.data, candidate)
        log = self.registry / 'ledger.jsonl'
        prefix = log.read_bytes()
        self.record('new', 'Synthetic newer value.')
        log.write_bytes(prefix)
        shutil.rmtree(self.data)
        with self.assertRaises(RegistryError):
            restore_snapshot(candidate, self.registry, self.data)
        self.assertFalse(self.data.exists())


if __name__ == '__main__':
    unittest.main()

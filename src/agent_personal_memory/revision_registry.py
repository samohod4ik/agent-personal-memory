"""External authenticated revision ledger; contains no plaintext sources."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class RegistryError(RuntimeError):
    """Registry is missing, incomplete, or inconsistent with the database."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _cell(value, key):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'bytes_hmac': hmac.new(key, value, hashlib.sha256).hexdigest()}
    return value


class RevisionRegistry:
    def __init__(self, directory: Path, *, initialize: bool):
        self.directory = Path(directory)
        self.key_path = self.directory / 'key'
        self.log_path = self.directory / 'ledger.jsonl'
        self.head_path = self.directory / 'head.json'
        self.lock_path = self.directory / 'lock'
        if initialize:
            try:
                self.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
                self._create(self.key_path, os.urandom(32))
                self._create(self.log_path, b'')
                self._create(self.lock_path, b'\0')
                self.key = self.key_path.read_bytes()
                self._write_head(0, '')
            except OSError as exc:
                raise RegistryError('cannot initialize independent registry') from exc
        else:
            self._check_protected(self.directory, directory=True)
            for path in (self.key_path, self.log_path, self.head_path, self.lock_path):
                self._check_protected(path)
            self.key = self.key_path.read_bytes()
        if len(self.key) != 32:
            raise RegistryError('invalid registry key')
        with self.locked():
            self.entries = self._verify()

    def _check_protected(self, path: Path, *, directory=False):
        try:
            mode = path.lstat().st_mode
            valid = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
            if not valid or (os.name == 'posix' and (stat.S_IMODE(mode) & 0o077 or path.stat().st_uid != os.getuid())):
                raise RegistryError('registry permissions or file type invalid')
        except OSError as exc:
            raise RegistryError('independent registry unavailable') from exc

    @staticmethod
    def _create(path, data):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def _mac(self, value):
        return hmac.new(self.key, _json(value), hashlib.sha256).hexdigest()

    @contextmanager
    def locked(self):
        """Serialize protected DB transactions and offline recovery."""
        self._check_protected(self.lock_path)
        try:
            fd = os.open(self.lock_path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        except OSError as exc:
            raise RegistryError('cannot lock independent registry') from exc
        try:
            try:
                if os.name == 'posix':
                    import fcntl
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise RegistryError('independent registry lock timed out')
                            time.sleep(.01)
                elif os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                else:
                    raise RegistryError('independent registry locking unsupported')
            except OSError as exc:
                raise RegistryError('cannot lock independent registry') from exc
            try:
                yield
            finally:
                if os.name == 'posix':
                    fcntl.flock(fd, fcntl.LOCK_UN)
                elif os.name == 'nt':
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)

    def _write_head(self, sequence, last):
        head = {'sequence': sequence, 'last': last}
        head['mac'] = self._mac(head)
        temporary = self.directory / 'head.tmp'
        self._create(temporary, _json(head) + b'\n')
        os.replace(temporary, self.head_path)
        if os.name == 'posix':
            fd = os.open(self.directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _verify(self):
        try:
            self._check_protected(self.directory, directory=True)
            for path in (self.key_path, self.log_path, self.head_path, self.lock_path):
                self._check_protected(path)
            if not hmac.compare_digest(self.key_path.read_bytes(), self.key):
                raise RegistryError('registry key changed')
            head = json.loads(self.head_path.read_bytes())
            signature = head.pop('mac')
            if not hmac.compare_digest(signature, self._mac(head)):
                raise RegistryError('registry head authentication failed')
            count, previous = 0, ''
            state = {'store': None, 'location': None, 'derived': {}, 'principals': {}, 'sources': {}, 'claims': {}, 'generations': {}, 'jobs': {}, 'deletions': {}}
            with self.log_path.open('rb') as stream:
                for line in stream:
                    item = json.loads(line)
                    mac = item.pop('mac')
                    if item['sequence'] != count + 1 or item['previous'] != previous or not hmac.compare_digest(mac, self._mac(item)):
                        raise RegistryError('registry chain invalid')
                    changes = item['changes']
                    for name in ('store', 'location'):
                        if name in changes:
                            state[name] = changes[name]
                    for name in ('principals', 'sources', 'claims', 'generations', 'derived', 'jobs', 'deletions'):
                        for identity, value in changes.get(name, {}).items():
                            if value is None:
                                state[name].pop(identity, None)
                            else:
                                state[name][identity] = value
                    count += 1
                    previous = mac
            if head != {'sequence': count, 'last': previous}:
                raise RegistryError('registry incomplete or truncated')
            return [{'sequence': count, 'state': state}] if count else []
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RegistryError('independent registry unavailable or invalid') from exc

    def snapshot(self, conn):
        def grouped(sql, identity):
            result = {}
            for row in conn.execute(sql):
                result.setdefault(row[identity], []).append([_cell(value, self.key) for value in row])
            return result
        principals = {row[0]: self._mac([_cell(value, self.key) for value in row])
                      for row in conn.execute('SELECT * FROM memory_principals ORDER BY id').fetchall()}
        generations = {row[0]: row[1] for row in conn.execute('SELECT tenant_id,generation FROM memory_actor_generation ORDER BY tenant_id')}
        deletions = {row[0]: {'tenant': row[1], 'digest': self._mac([_cell(value, self.key) for value in row])}
                     for row in conn.execute('SELECT * FROM memory_deletion_manifest ORDER BY receipt_id')}
        if self.entries:
            for identity, entry in self.entries[-1]['state']['deletions'].items():
                if entry.get('unavailable') and identity not in deletions:
                    deletions[identity] = entry
        source_versions = grouped('SELECT * FROM memory_source_versions ORDER BY source_id,version', 1)
        journal_rows = grouped('SELECT * FROM memory_journal ORDER BY source_id', 0)
        source_keys = grouped("SELECT * FROM memory_keys WHERE entity_kind='source' ORDER BY entity_id,version", 3)
        capture_generations = grouped('SELECT * FROM memory_capture_generation ORDER BY source_id', 0)
        lineages = grouped('SELECT * FROM memory_source_lineage ORDER BY source_id', 1)
        quarantined = grouped('SELECT * FROM memory_quarantined_sources ORDER BY source_id', 1)
        previous_sources = self.entries[-1]['state']['sources'] if self.entries else {}
        sources = {}
        for row in conn.execute('SELECT * FROM memory_sources ORDER BY id').fetchall():
            sid, tenant, deleted = row[0], row[1], row[5]
            versions, journal = source_versions.get(sid, []), journal_rows.get(sid, [])
            event_hash = journal[0][2] if journal else None
            lookup = row[3] or previous_sources.get(sid, {}).get('lookup', '')
            kind = (previous_sources.get(sid, {}).get('kind') if deleted is not None else None) or self._mac(['kind', row[2]])
            sources[sid] = {'tenant': tenant, 'revision': max((row[3] for row in versions), default=0),
                            'event': event_hash, 'lookup': lookup, 'kind': kind, 'deleted': deleted is not None,
                            'digest': self._mac([[_cell(v, self.key) for v in row], versions, journal, source_keys.get(sid, []), lineages.get(sid, []), quarantined.get(sid, []), capture_generations.get(sid, [])])}
        claim_versions = grouped('SELECT * FROM memory_claim_versions ORDER BY claim_id,version', 1)
        claim_keys = grouped("SELECT * FROM memory_keys WHERE entity_kind='claim' ORDER BY entity_id,version", 3)
        supports = grouped('SELECT * FROM memory_support ORDER BY claim_id,id', 2)
        evidences = grouped('SELECT * FROM memory_evidence ORDER BY claim_id,claim_version,source_id,source_version,span_start', 1)
        claims = {}
        for row in conn.execute('SELECT * FROM memory_claims ORDER BY id').fetchall():
            cid, tenant, deleted = row[0], row[1], row[4]
            versions = claim_versions.get(cid, [])
            claims[cid] = {'tenant': tenant, 'revision': max((row[3] for row in versions), default=0),
                           'deleted': deleted is not None, 'digest': self._mac([[_cell(v, self.key) for v in row], versions, claim_keys.get(cid, []), supports.get(cid, []), evidences.get(cid, [])])}
        derived = {}
        jobs = {f'{row[0]}:{row[1]}': {'tenant': row[0], 'key': row[1], 'fingerprint': row[2], 'admitted': bool(row[6])}
                for row in conn.execute('SELECT * FROM memory_jobs ORDER BY tenant_id,job_key')}
        for tenant in principals:
            derived_tables = {}
            for table in ('memory_projections', 'memory_jobs', 'memory_relations', 'memory_claim_links', 'memory_capture_days'):
                derived_tables[table] = sorted(([_cell(value, self.key) for value in row] for row in conn.execute(f'SELECT * FROM {table} WHERE tenant_id=?', (tenant,))), key=_json)
            derived_tables['projection_keys'] = sorted(([_cell(value, self.key) for value in row] for row in conn.execute("SELECT * FROM memory_keys WHERE tenant_id=? AND entity_kind='projection'", (tenant,))), key=_json)
            derived[tenant] = self._mac(derived_tables)
        return {'store': self.store_marker, 'location': self.location_marker, 'derived': derived,
                'principals': principals, 'sources': sources, 'claims': claims, 'generations': generations, 'jobs': jobs, 'deletions': deletions}

    def forgotten_lookup(self, tenant, kind, lookup):
        kind_hash = self._mac(['kind', kind])
        return any(source['tenant'] == tenant and source['deleted'] and source.get('kind') == kind_hash
                   and (source.get('lookup') == lookup or (not source.get('lookup') and not source.get('event')))
                   for source in self.entries[-1]['state']['sources'].values())

    def check(self, conn):
        self.entries = self._verify()
        if not self.entries or self.entries[-1]['state'] != self.snapshot(conn):
            raise RegistryError('database and independent registry disagree')

    def append(self, state):
        entries = self._verify()
        if entries and entries[-1]['state'] == state:
            return
        head = json.loads(self.head_path.read_bytes())
        previous = entries[-1]['state'] if entries else {'store': None, 'location': None, 'derived': {}, 'principals': {}, 'sources': {}, 'claims': {}, 'generations': {}, 'jobs': {}, 'deletions': {}}
        changes = {name: state[name] for name in ('store', 'location') if state[name] != previous[name]}
        for name in ('principals', 'sources', 'claims', 'generations', 'derived', 'jobs', 'deletions'):
            delta = {identity: state[name].get(identity) for identity in state[name].keys() | previous[name].keys()
                     if state[name].get(identity) != previous[name].get(identity)}
            if delta:
                changes[name] = delta
        item = {'sequence': (entries[-1]['sequence'] if entries else 0) + 1,
                'previous': head['last'], 'changes': changes}
        mac = self._mac(item)
        item['mac'] = mac
        try:
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'ab') as stream:
                stream.write(_json(item) + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            self._write_head(item['sequence'], mac)
        except OSError as exc:
            raise RegistryError('cannot durably advance independent registry') from exc
        self.entries = self._verify()


def restore_snapshot(candidate: Path, registry_dir: Path, output: Path) -> dict:
    """Reconcile an old encrypted image offline; never expose stale bytes."""
    candidate, registry_dir, output = map(lambda p: Path(p).resolve(), (candidate, registry_dir, output))
    if output.exists() or not output.parent.is_dir() or output in (candidate, registry_dir):
        raise RegistryError('restore output must be a new isolated directory')
    if candidate in output.parents or registry_dir in output.parents:
        raise RegistryError('restore output must be outside inputs')
    registry = RevisionRegistry(registry_dir, initialize=False)
    with registry.locked():
        registry.entries = registry._verify()
        return _restore_locked(candidate, registry, output)


def _restore_locked(candidate: Path, registry: RevisionRegistry, output: Path) -> dict:
    if output.exists():
        raise RegistryError('original store location is no longer absent')
    if not registry.entries:
        raise RegistryError('registry has no complete state')
    desired = registry.entries[-1]['state']
    key_path, db_path = candidate / 'master.key', candidate / 'personal_memory.db'
    if not key_path.is_file() or not db_path.is_file():
        raise RegistryError('candidate database or key unavailable')
    key = key_path.read_bytes()
    registry.store_marker = registry._mac(['store-key', key.hex()])
    registry.location_marker = registry._mac(['store-location', str(output)])
    if len(key) != 32 or desired['store'] != registry.store_marker or desired['location'] != registry.location_marker:
        raise RegistryError('candidate key or original location does not match registry')
    try:
        with tempfile.TemporaryDirectory(prefix='.memory-restore-', dir=output.parent) as temporary:
            staging = Path(temporary) / 'state'
            staging.mkdir(mode=0o700)
            shutil.copy2(key_path, staging / 'master.key')
            source = sqlite3.connect(f'{db_path.as_uri()}?mode=ro', uri=True)
            destination = sqlite3.connect(staging / 'personal_memory.db', isolation_level=None)
            try:
                source.backup(destination)
                destination.execute('PRAGMA foreign_keys=ON')
                destination.execute('PRAGMA secure_delete=ON')
                if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RegistryError('candidate database failed integrity check')
                version = destination.execute('SELECT version FROM memory_schema WHERE id=1').fetchone()
                if version is None or version[0] not in (6, 7):
                    raise RegistryError('candidate schema requires separate migration')
                destination.execute('BEGIN IMMEDIATE')
                if version[0] == 6:
                    destination.execute('CREATE TABLE IF NOT EXISTS memory_quarantined_sources (tenant_id TEXT NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY(tenant_id,source_id), FOREIGN KEY(source_id) REFERENCES memory_sources(id))')
                    destination.execute('UPDATE memory_schema SET version=7 WHERE id=1')
                actual = registry.snapshot(destination)
                if actual['store'] != desired['store'] or actual['principals'] != desired['principals']:
                    raise RegistryError('actor metadata changed or is unavailable')
                if actual['sources'].keys() - desired['sources'].keys() or actual['claims'].keys() - desired['claims'].keys():
                    raise RegistryError('candidate contains identities not present in registry')
                if actual['deletions'].keys() - desired['deletions'].keys():
                    raise RegistryError('candidate contains deletion receipts not present in registry')
                unavailable_receipts = {receipt for receipt in desired['deletions']
                                        if receipt not in actual['deletions'] or actual['deletions'][receipt].get('unavailable')}
                for receipt, value in actual['deletions'].items():
                    expected = {k: v for k, v in desired['deletions'][receipt].items() if k != 'unavailable'}
                    if value != expected:
                        unavailable_receipts.add(receipt)
                        destination.execute('DELETE FROM memory_deletion_manifest WHERE receipt_id=?', (receipt,))
                now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                affected = {sid for sid, entry in actual['sources'].items() if entry != desired['sources'][sid]}
                affected.update(row[0] for row in destination.execute(
                    'SELECT j.source_id FROM memory_journal j JOIN memory_sources s ON s.id=j.source_id '
                    'WHERE j.expires_at<=? AND s.deleted_at IS NULL', (now,)))
                missing = set(desired['sources']) - set(actual['sources'])
                affected_claims = {cid for cid, entry in actual['claims'].items() if entry != desired['claims'][cid]}
                missing_claims = set(desired['claims']) - set(actual['claims'])
                affected_tenants = {desired['sources'][sid]['tenant'] for sid in affected | missing}
                affected_tenants.update(desired['claims'][cid]['tenant'] for cid in affected_claims | missing_claims)
                affected_tenants.update(tenant for tenant in desired['principals']
                    if actual['derived'].get(tenant) != desired['derived'].get(tenant)
                    or actual['generations'].get(tenant, 0) != desired['generations'].get(tenant, 0))
                for sid in affected:
                    affected_claims.update(row[0] for row in destination.execute(
                        'SELECT claim_id FROM memory_support WHERE source_id=? UNION SELECT claim_id FROM memory_evidence WHERE source_id=?', (sid, sid)))
                for cid in affected_claims:
                    destination.execute("UPDATE memory_claim_versions SET state='deleted',content_enc=NULL,explanation_enc=NULL WHERE claim_id=?", (cid,))
                    destination.execute("DELETE FROM memory_keys WHERE entity_kind='claim' AND entity_id=?", (cid,))
                    destination.execute('UPDATE memory_claims SET deleted_at=? WHERE id=?', (now, cid))
                    destination.execute('DELETE FROM memory_support WHERE claim_id=?', (cid,))
                    destination.execute('DELETE FROM memory_evidence WHERE claim_id=?', (cid,))
                for sid in affected:
                    destination.execute("UPDATE memory_source_versions SET snapshot_enc=NULL,locator_enc=NULL,metadata_enc=NULL,content_hash='',deletion_state='deleted' WHERE source_id=?", (sid,))
                    destination.execute("DELETE FROM memory_keys WHERE entity_kind='source' AND entity_id=?", (sid,))
                    destination.execute("UPDATE memory_sources SET deleted_at=?,keyed_lookup_hash='' WHERE id=?", (now, sid))
                    destination.execute('UPDATE memory_journal SET redacted=1 WHERE source_id=?', (sid,))
                    destination.execute('DELETE FROM memory_source_lineage WHERE source_id=? OR parent_source_id=?', (sid, sid))
                for sid in missing:
                    entry = desired['sources'][sid]
                    if entry['tenant'] not in desired['principals']:
                        raise RegistryError('source actor unavailable')
                    destination.execute("INSERT INTO memory_sources(id,tenant_id,source_kind,keyed_lookup_hash,created_at,deleted_at) VALUES(?,?,'document','',?,?)", (sid, entry['tenant'], now, now))
                    if entry['event']:
                        destination.execute('INSERT INTO memory_journal VALUES(?,?,?,?,?,?,1)', (sid, entry['tenant'], entry['event'], '', now, now))
                # No pre-restore in-flight assistant capture may cross the
                # recovery boundary, even if its parent still exists.
                for tenant in affected_tenants:
                    generation = desired['generations'].get(tenant, 0) + 1
                    destination.execute('INSERT INTO memory_actor_generation VALUES(?,?) ON CONFLICT(tenant_id) DO UPDATE SET generation=excluded.generation', (tenant, generation))
                    destination.execute("DELETE FROM memory_keys WHERE tenant_id=? AND entity_kind='projection'", (tenant,))
                    for table in ('memory_projections', 'memory_jobs', 'memory_relations', 'memory_claim_links', 'memory_capture_days'):
                        destination.execute(f'DELETE FROM {table} WHERE tenant_id=?', (tenant,))
                    for job in desired['jobs'].values():
                        if job['tenant'] == tenant and job['admitted']:
                            destination.execute("INSERT OR REPLACE INTO memory_jobs VALUES(?,?,?,'pending',0,'restored',1,0,0)",
                                                (tenant, job['key'], job['fingerprint']))
                    if destination.execute("SELECT 1 FROM sqlite_master WHERE name='memory_claim_fts_index'").fetchone():
                        destination.execute('DELETE FROM memory_claim_fts_index WHERE tenant_id=?', (tenant,))
                destination.execute('CREATE TABLE IF NOT EXISTS memory_registry_binding (store_marker TEXT NOT NULL)')
                destination.execute('DELETE FROM memory_registry_binding')
                destination.execute('INSERT INTO memory_registry_binding VALUES (?)', (registry.store_marker,))
                destination.execute('COMMIT')
                destination.execute('VACUUM')
                checkpoint = destination.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint and checkpoint[0] != 0:
                    raise RegistryError('cannot compact restored write-ahead log')
                recovered_state = registry.snapshot(destination)
                for receipt in unavailable_receipts:
                    recovered_state['deletions'][receipt] = {**desired['deletions'][receipt], 'unavailable': True}
                registry.append(recovered_state)
            finally:
                source.close()
                destination.close()
            os.rename(staging, output)
        unavailable = sum(not desired['sources'][sid]['deleted'] for sid in affected | missing)
        unavailable_claims = sum(not desired['claims'][cid]['deleted'] for cid in affected_claims | missing_claims)
        return {'status': 'restored', 'unavailable_sources': unavailable,
                'unavailable_claims': unavailable_claims, 'suppressed_sources': len(affected | missing),
                'suppressed_claims': len(affected_claims), 'unavailable_deletion_receipts': len(unavailable_receipts)}
    except RegistryError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise RegistryError('restore could not be safely reconciled') from exc

"""Schema and transaction helpers for encrypted layered projections."""
from __future__ import annotations

import json
import uuid

from .crypto import decrypt_record, encrypt_record

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS memory_actor_generation (
        tenant_id TEXT PRIMARY KEY, generation INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memory_capture_generation (
        source_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, generation INTEGER NOT NULL,
        FOREIGN KEY(source_id) REFERENCES memory_sources(id))""",
    """CREATE TABLE IF NOT EXISTS memory_capture_days (
        tenant_id TEXT NOT NULL, day TEXT NOT NULL, timezone TEXT NOT NULL,
        empty_at_completion INTEGER NOT NULL, completed_at TEXT NOT NULL,
        PRIMARY KEY(tenant_id,day,timezone))""",
    """CREATE TABLE IF NOT EXISTS memory_journal (
        source_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, event_hash TEXT NOT NULL,
        envelope_hash TEXT NOT NULL, occurred_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        redacted INTEGER NOT NULL DEFAULT 0,
        UNIQUE(tenant_id,event_hash),
        FOREIGN KEY(source_id) REFERENCES memory_sources(id))""",
    "CREATE INDEX IF NOT EXISTS idx_journal_time ON memory_journal(tenant_id,occurred_at,source_id)",
    """CREATE TABLE IF NOT EXISTS memory_source_lineage (
        tenant_id TEXT NOT NULL, source_id TEXT PRIMARY KEY,
        parent_source_id TEXT NOT NULL, parent_source_version INTEGER NOT NULL,
        FOREIGN KEY(source_id) REFERENCES memory_sources(id),
        FOREIGN KEY(parent_source_id) REFERENCES memory_sources(id))""",
    "CREATE INDEX IF NOT EXISTS idx_source_lineage_parent ON memory_source_lineage(tenant_id,parent_source_id)",
    """CREATE TABLE IF NOT EXISTS memory_evidence (
        tenant_id TEXT NOT NULL, claim_id TEXT NOT NULL, claim_version INTEGER NOT NULL,
        source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
        span_start INTEGER NOT NULL, span_end INTEGER NOT NULL,
        PRIMARY KEY(tenant_id,claim_id,claim_version,source_id,source_version,span_start),
        FOREIGN KEY(claim_id) REFERENCES memory_claims(id),
        FOREIGN KEY(source_id) REFERENCES memory_sources(id))""",
    """CREATE TABLE IF NOT EXISTS memory_projections (
        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, period TEXT NOT NULL,
        start TEXT NOT NULL, timezone TEXT NOT NULL, fingerprint TEXT NOT NULL,
        payload_enc BLOB NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(tenant_id,period,start,timezone))""",
    """CREATE TABLE IF NOT EXISTS memory_jobs (
        tenant_id TEXT NOT NULL, job_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
        state TEXT NOT NULL, lease_until REAL NOT NULL, owner TEXT NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(tenant_id,job_key))""",
    """CREATE TABLE IF NOT EXISTS memory_relations (
        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
        from_claim_id TEXT NOT NULL, from_version INTEGER NOT NULL,
        to_claim_id TEXT NOT NULL, to_version INTEGER NOT NULL,
        relation TEXT NOT NULL, source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
        span_start INTEGER NOT NULL, span_end INTEGER NOT NULL,
        FOREIGN KEY(from_claim_id) REFERENCES memory_claims(id),
        FOREIGN KEY(to_claim_id) REFERENCES memory_claims(id))""",
)

REPORT_ORDER_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS memory_report_order_slots (
        tenant_id TEXT PRIMARY KEY, claim_id TEXT, direction TEXT, cutoff_at TEXT NOT NULL,
        action_source TEXT, direct_at TEXT, FOREIGN KEY(claim_id) REFERENCES memory_claims(id))""",
    """CREATE TABLE IF NOT EXISTS memory_report_order_votes (
        tenant_id TEXT NOT NULL, source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
        span_start INTEGER NOT NULL, span_end INTEGER NOT NULL, direction TEXT NOT NULL,
        PRIMARY KEY(tenant_id,source_id,source_version),
        FOREIGN KEY(source_id) REFERENCES memory_sources(id))""",
    """CREATE TABLE IF NOT EXISTS memory_report_order_attempts (
        tenant_id TEXT NOT NULL, job_key TEXT NOT NULL, owner TEXT NOT NULL,
        start TEXT NOT NULL, timezone TEXT NOT NULL, deadline REAL NOT NULL,
        eligible_sources INTEGER NOT NULL, state TEXT NOT NULL, cli TEXT, model TEXT,
        PRIMARY KEY(tenant_id,job_key))""",
    """CREATE TRIGGER IF NOT EXISTS clear_redacted_report_order_votes
        AFTER UPDATE OF redacted ON memory_journal WHEN new.redacted=1
        BEGIN
            DELETE FROM memory_report_order_votes WHERE tenant_id=new.tenant_id AND source_id=new.source_id;
        END""",
)

REPORT_ORDER_ADMISSION_SCHEMA = (
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_report_order_attempt_owner
        ON memory_report_order_attempts(tenant_id,owner)""",
    """CREATE INDEX IF NOT EXISTS idx_report_order_attempt_day
        ON memory_report_order_attempts(tenant_id,timezone,start)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_report_order_active_day
        ON memory_report_order_attempts(tenant_id,timezone,start) WHERE state='admitted'""",
    """CREATE TABLE IF NOT EXISTS memory_report_order_admitted_sources (
        tenant_id TEXT NOT NULL, owner TEXT NOT NULL, source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
        PRIMARY KEY(tenant_id,owner,source_id,source_version),
        FOREIGN KEY(tenant_id,owner) REFERENCES memory_report_order_attempts(tenant_id,owner) ON DELETE CASCADE)""",
    """CREATE INDEX IF NOT EXISTS idx_report_order_admitted_source
        ON memory_report_order_admitted_sources(tenant_id,source_id)""",
)

REPORT_ORDER_VISIBILITY_SCHEMA = """CREATE TABLE IF NOT EXISTS memory_report_order_unavailable_sources (
    tenant_id TEXT NOT NULL, source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
    PRIMARY KEY(tenant_id,source_id,source_version),
    FOREIGN KEY(source_id,source_version) REFERENCES memory_source_versions(source_id,version))"""


def migrate_report_order_admissions_on(conn):
    if not any(row["name"] == "admitted_input_bytes"
               for row in conn.execute("PRAGMA table_info(memory_report_order_attempts)")):
        conn.execute("ALTER TABLE memory_report_order_attempts ADD COLUMN admitted_input_bytes INTEGER")
    for statement in REPORT_ORDER_ADMISSION_SCHEMA:
        conn.execute(statement)
    # v7 kept receipts without their exact admitted input. Fence every
    # possible earlier input from the recorded window, including edited
    # originals whose prior version was in that window.
    from .consolidation import bounds
    for attempt in conn.execute("SELECT tenant_id,owner,start,timezone FROM memory_report_order_attempts"):
        left, right, _, _ = bounds("day", attempt["start"], attempt["timezone"])
        conn.execute("""INSERT OR IGNORE INTO memory_report_order_admitted_sources
            SELECT v.tenant_id,?,v.source_id,v.version FROM memory_source_versions v
            JOIN memory_journal j ON j.tenant_id=v.tenant_id AND j.source_id=v.source_id
            WHERE v.tenant_id=? AND ((v.observed_at>=? AND v.observed_at<?)
                OR (j.occurred_at>=? AND j.occurred_at<?))""",
            (attempt["owner"], attempt["tenant_id"], left, right, left, right),
        )


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def generation_on(conn, tenant):
    row = conn.execute("SELECT generation FROM memory_actor_generation WHERE tenant_id=?", (tenant,)).fetchone()
    return row[0] if row else 0


def invalidate_generation_on(conn, tenant):
    conn.execute("INSERT INTO memory_actor_generation VALUES(?,1) ON CONFLICT(tenant_id) DO UPDATE SET generation=generation+1", (tenant,))


def put_projection(store, conn, tenant, *, period, start, timezone, fingerprint, payload, now):
    old = conn.execute("SELECT id FROM memory_projections WHERE tenant_id=? AND period=? AND start=? AND timezone=?",
                       (tenant, period, start, timezone)).fetchone()
    if old:
        store._destroy_keys(conn, tenant_id=tenant, entity_kind="projection", entity_id=old["id"], now=now)
        conn.execute("DELETE FROM memory_projections WHERE id=?", (old["id"],))
    identity = str(uuid.uuid4())
    wrapped, blob = encrypt_record(store._kek, encode(payload), tenant_id=tenant,
                                  entity_kind="projection", entity_id=identity, version=1)
    store._put_record_dek(conn, tenant_id=tenant, entity_kind="projection", entity_id=identity,
                          version=1, wrapped_dek=wrapped, now=now)
    conn.execute("INSERT INTO memory_projections VALUES(?,?,?,?,?,?,?,?)",
                 (identity, tenant, period, start, timezone, fingerprint, blob, now))
    return identity


def read_projection(store, conn, tenant, row):
    wrapped = store._get_record_dek(conn, tenant_id=tenant, entity_kind="projection", entity_id=row["id"], version=1)
    if wrapped is None:
        return None
    return json.loads(decrypt_record(store._kek, wrapped, row["payload_enc"], tenant_id=tenant,
                                     entity_kind="projection", entity_id=row["id"], version=1))


def purge_projections_on(store, conn, tenant, now):
    # Conservative privacy invalidation: these are rebuildable, never canonical facts.
    for row in conn.execute("SELECT id FROM memory_projections WHERE tenant_id=?", (tenant,)).fetchall():
        store._destroy_keys(conn, tenant_id=tenant, entity_kind="projection", entity_id=row["id"], now=now)
    conn.execute("DELETE FROM memory_projections WHERE tenant_id=?", (tenant,))
    conn.execute("DELETE FROM memory_jobs WHERE tenant_id=?", (tenant,))

"""SQLite schema foundation for the agent-personal-memory store."""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .crypto import (
    FIELD_CONTENT,
    FIELD_EXPLANATION,
    FIELD_LOCATOR,
    FIELD_METADATA,
    FIELD_SNAPSHOT,
    CryptoError,
    decrypt_field,
    decrypt_record,
    derive_lookup_key,
    encrypt_field,
    encrypt_record,
    ensure_master_key,
    field_aad,
    keyed_lookup_hash,
    new_dek,
    record_aad,
    unwrap_dek,
    wrap_dek,
)
from .erasure import ErasureReceipt, compact_sqlite_store, new_erasure_receipt
from .lifecycle import ACTIVE_READ_STATES, MemoryState
from .types import Actor
from .validation import (
    DEFAULT_MEMORY_MODE,
    MEMORY_MODES,
    ValidationError,
    canonicalize_metadata,
    confirmation_source_is_reliable,
    validate_correct_fields,
    validate_create_candidate_fields,
    validate_memory_mode,
    validate_valid_time_order,
    validate_list_limit,
    validate_optional_claim_kind,
    validate_optional_source_kind,
    validate_register_fields,
    content_hash_for,
)

logger = logging.getLogger("plugins.personal-memory")

SCHEMA_VERSION = 4

_CREATE_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS memory_schema (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_principals (
        id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        user_id TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (platform, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_sources (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        keyed_lookup_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        deleted_at TEXT,
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_source_versions (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        snapshot_enc BLOB,
        locator_enc BLOB,
        metadata_enc BLOB,
        content_hash TEXT NOT NULL DEFAULT '',
        observed_at TEXT NOT NULL,
        retention_class TEXT NOT NULL DEFAULT '',
        deletion_state TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        UNIQUE (source_id, version),
        FOREIGN KEY (source_id) REFERENCES memory_sources(id),
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_claims (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        claim_kind TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted_at TEXT,
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_claim_versions (
        id TEXT PRIMARY KEY,
        claim_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'candidate',
        sensitivity TEXT NOT NULL DEFAULT '',
        content_enc BLOB,
        confidence REAL,
        explanation_enc BLOB,
        valid_from TEXT,
        valid_to TEXT,
        expires_at TEXT,
        supersedes_id TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (claim_id, version),
        FOREIGN KEY (claim_id) REFERENCES memory_claims(id),
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_support (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        claim_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id),
        FOREIGN KEY (claim_id) REFERENCES memory_claims(id),
        FOREIGN KEY (source_id) REFERENCES memory_sources(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id TEXT,
        actor_id TEXT,
        action TEXT NOT NULL,
        target_kind TEXT NOT NULL DEFAULT '',
        target_id TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL,
        policy_reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_keys (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        entity_kind TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        wrapped_dek BLOB NOT NULL,
        created_at TEXT NOT NULL,
        destroyed_at TEXT,
        UNIQUE (tenant_id, entity_kind, entity_id, version),
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
)

_V2_SCHEMA_SQL = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_sources_lookup
      ON memory_sources(tenant_id, source_kind, keyed_lookup_hash)
      WHERE keyed_lookup_hash <> ''
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_source_versions_tenant_insert
    BEFORE INSERT ON memory_source_versions
    FOR EACH ROW
    WHEN NEW.tenant_id <> (
        SELECT tenant_id FROM memory_sources WHERE id = NEW.source_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'source version tenant mismatch');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_source_versions_tenant_update
    BEFORE UPDATE ON memory_source_versions
    FOR EACH ROW
    WHEN NEW.tenant_id <> (
        SELECT tenant_id FROM memory_sources WHERE id = NEW.source_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'source version tenant mismatch');
    END
    """,
)

_V3_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS memory_deletion_manifest (
        receipt_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        target_kind TEXT NOT NULL,
        outcome TEXT NOT NULL,
        erased_at TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES memory_principals(id)
    )
    """,
)

_V4_SCHEMA_SQL = (
    """
    ALTER TABLE memory_principals
    ADD COLUMN memory_mode TEXT NOT NULL DEFAULT 'auto_insight'
    """,
)

AUDIT_ACTIONS = frozenset(
    {
        "source.register",
        "source.get",
        "claim.create_candidate",
        "claim.confirm",
        "claim.correct",
        "claim.list",
        "claim.query",
        "claim.erase",
        "source.query",
        "source.erase",
        "owner.export",
        "claim.explain",
        "actor.set_memory_mode",
    }
)
AUDIT_TARGET_KINDS = frozenset(
    {"source", "source_version", "claim", "claim_version", "export", "principal"}
)
AUDIT_OUTCOMES = frozenset(
    {
        "created",
        "replayed",
        "version_appended",
        "denied",
        "rejected",
        "confirmed",
        "superseded",
        "erased",
        "exported",
        "explained",
        "updated",
    }
)
AUDIT_REASONS = frozenset(
    {
        "",
        "not_found",
        "validation_failed",
        "idempotent_replay",
        "content_changed",
        "confirmation_required",
        "invalid_transition",
    }
)


class SchemaVersionError(RuntimeError):
    """Raised when the on-disk schema is newer than this package build."""


class NotFoundError(LookupError):
    """Raised when a record is missing or not visible to the actor."""


class ConflictError(RuntimeError):
    """Raised when source registration cannot be reconciled."""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _export_bytes(value: bytes) -> str | dict[str, str]:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
        }


def _source_label(metadata: object) -> str:
    if isinstance(metadata, dict):
        title = metadata.get("title")
        if isinstance(title, str):
            return title
    return ""


def default_data_dir() -> Path:
    override = (os.environ.get("PERSONAL_MEMORY_DATA_DIR") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".agent-personal-memory"


class PersonalMemoryStore:
    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.data_dir = Path(data_dir or default_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "personal_memory.db"
        self.key_path = self.data_dir / "master.key"
        self._kek = ensure_master_key(self.key_path)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA secure_delete = ON")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = self._read_schema_version(conn)
                if current > self.SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"Unknown future schema version: {current}"
                    )
                if current < 1:
                    for statement in _CREATE_SCHEMA_SQL:
                        conn.execute(statement)
                if current < 2:
                    for statement in _V2_SCHEMA_SQL:
                        conn.execute(statement)
                if current < 3:
                    for statement in _V3_SCHEMA_SQL:
                        conn.execute(statement)
                if current < 4:
                    for statement in _V4_SCHEMA_SQL:
                        conn.execute(statement)
                if current < self.SCHEMA_VERSION:
                    conn.execute(
                        "INSERT OR REPLACE INTO memory_schema(id, version) VALUES (1, ?)",
                        (self.SCHEMA_VERSION,),
                    )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def _read_schema_version(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memory_schema'"
        ).fetchone()
        if row is None:
            return 0
        version_row = conn.execute(
            "SELECT version FROM memory_schema WHERE id = 1"
        ).fetchone()
        if version_row is None:
            return 0
        return int(version_row[0])

    def _ensure_principal_on(self, conn: sqlite3.Connection, actor: Actor) -> str:
        now = utcnow_iso()
        row = conn.execute(
            "SELECT id, display_name FROM memory_principals "
            "WHERE platform = ? AND user_id = ?",
            (actor.platform, actor.user_id),
        ).fetchone()
        if row:
            if actor.display_name and actor.display_name != row["display_name"]:
                conn.execute(
                    "UPDATE memory_principals SET display_name = ?, updated_at = ? "
                    "WHERE id = ?",
                    (actor.display_name, now, row["id"]),
                )
            return str(row["id"])
        principal_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO memory_principals("
            "id, platform, user_id, display_name, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                principal_id,
                actor.platform,
                actor.user_id,
                actor.display_name,
                now,
                now,
            ),
        )
        return principal_id

    def ensure_principal(self, actor: Actor) -> str:
        with self._tx() as conn:
            return self._ensure_principal_on(conn, actor)

    def _memory_mode_on(self, conn: sqlite3.Connection, tenant_id: str) -> str:
        row = conn.execute(
            "SELECT memory_mode FROM memory_principals WHERE id = ?",
            (tenant_id,),
        ).fetchone()
        if row is None:
            return DEFAULT_MEMORY_MODE
        value = str(row["memory_mode"] or "").strip()
        if value in MEMORY_MODES:
            return value
        return DEFAULT_MEMORY_MODE

    def get_memory_mode(self, actor: Actor) -> str:
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            return self._memory_mode_on(conn, tenant_id)

    def set_memory_mode(self, actor: Actor, mode: str) -> str:
        cleaned = validate_memory_mode(mode)
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            now = utcnow_iso()
            conn.execute(
                "UPDATE memory_principals SET memory_mode = ?, updated_at = ? "
                "WHERE id = ?",
                (cleaned, now, tenant_id),
            )
            self._append_audit(
                conn,
                tenant_id=tenant_id,
                actor_id=tenant_id,
                action="actor.set_memory_mode",
                target_kind="principal",
                target_id=tenant_id,
                outcome="updated",
                policy_reason="",
            )
            return cleaned

    def _put_record_dek(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        entity_kind: str,
        entity_id: str,
        version: int,
        wrapped_dek: bytes,
        now: str,
    ) -> None:
        conn.execute(
            "INSERT INTO memory_keys("
            "id, tenant_id, entity_kind, entity_id, version, wrapped_dek, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                tenant_id,
                entity_kind,
                entity_id,
                version,
                wrapped_dek,
                now,
            ),
        )

    def _get_record_dek(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        entity_kind: str,
        entity_id: str,
        version: int,
    ) -> bytes | None:
        row = conn.execute(
            "SELECT wrapped_dek FROM memory_keys "
            "WHERE tenant_id = ? AND entity_kind = ? AND entity_id = ? "
            "AND version = ? AND destroyed_at IS NULL",
            (tenant_id, entity_kind, entity_id, version),
        ).fetchone()
        if row is None:
            return None
        return bytes(row["wrapped_dek"])

    def encrypt_and_persist_key(
        self,
        plaintext: bytes,
        *,
        tenant_id: str,
        entity_kind: str,
        entity_id: str,
        version: int,
    ) -> tuple[bytes, bytes]:
        wrapped, blob = encrypt_record(
            self._kek,
            plaintext,
            tenant_id=tenant_id,
            entity_kind=entity_kind,
            entity_id=entity_id,
            version=version,
        )
        now = utcnow_iso()
        with self._tx() as conn:
            self._put_record_dek(
                conn,
                tenant_id=tenant_id,
                entity_kind=entity_kind,
                entity_id=entity_id,
                version=version,
                wrapped_dek=wrapped,
                now=now,
            )
        return wrapped, blob

    def decrypt_with_persisted_key(
        self,
        blob: bytes,
        *,
        tenant_id: str,
        entity_kind: str,
        entity_id: str,
        version: int,
    ) -> bytes:
        with self._tx() as conn:
            wrapped = self._get_record_dek(
                conn,
                tenant_id=tenant_id,
                entity_kind=entity_kind,
                entity_id=entity_id,
                version=version,
            )
        if wrapped is None:
            raise LookupError("Wrapped DEK not found")
        return decrypt_record(
            self._kek,
            wrapped,
            blob,
            tenant_id=tenant_id,
            entity_kind=entity_kind,
            entity_id=entity_id,
            version=version,
        )

    def _append_audit(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        actor_id: str,
        action: str,
        target_kind: str,
        target_id: str,
        outcome: str,
        policy_reason: str,
    ) -> None:
        if action not in AUDIT_ACTIONS:
            raise ValueError("audit action is not allowlisted")
        if target_kind not in AUDIT_TARGET_KINDS:
            raise ValueError("audit target_kind is not allowlisted")
        if outcome not in AUDIT_OUTCOMES:
            raise ValueError("audit outcome is not allowlisted")
        if policy_reason not in AUDIT_REASONS:
            raise ValueError("audit policy_reason is not allowlisted")
        conn.execute(
            "INSERT INTO memory_audit("
            "tenant_id, actor_id, action, target_kind, target_id, outcome, "
            "policy_reason, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                actor_id,
                action,
                target_kind,
                target_id,
                outcome,
                policy_reason,
                utcnow_iso(),
            ),
        )

    def _lookup_source(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        source_kind: str,
        lookup_hash: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT id, tenant_id, source_kind, created_at, deleted_at "
            "FROM memory_sources "
            "WHERE tenant_id = ? AND source_kind = ? AND keyed_lookup_hash = ?",
            (tenant_id, source_kind, lookup_hash),
        ).fetchone()

    def _newest_active_version(
        self, conn: sqlite3.Connection, tenant_id: str, source_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT id, source_id, tenant_id, version, snapshot_enc, locator_enc, "
            "metadata_enc, content_hash, observed_at, retention_class, "
            "deletion_state, created_at "
            "FROM memory_source_versions "
            "WHERE source_id = ? AND tenant_id = ? AND deletion_state = 'active' "
            "ORDER BY version DESC LIMIT 1",
            (source_id, tenant_id),
        ).fetchone()

    def _encrypt_source_fields(
        self,
        *,
        tenant_id: str,
        source_id: str,
        version: int,
        snapshot: bytes,
        external_ref: str,
        metadata: dict[str, Any],
    ) -> tuple[bytes, bytes, bytes, bytes]:
        dek = new_dek()
        wrap_aad = record_aad(tenant_id, "source", source_id, version)
        wrapped = wrap_dek(self._kek, dek, wrap_aad)
        snapshot_enc = encrypt_field(
            dek,
            snapshot,
            field_aad(tenant_id, "source", source_id, version, FIELD_SNAPSHOT),
        )
        locator_enc = encrypt_field(
            dek,
            external_ref.encode("utf-8"),
            field_aad(tenant_id, "source", source_id, version, FIELD_LOCATOR),
        )
        metadata_enc = encrypt_field(
            dek,
            canonicalize_metadata(metadata),
            field_aad(tenant_id, "source", source_id, version, FIELD_METADATA),
        )
        return wrapped, snapshot_enc, locator_enc, metadata_enc

    def _insert_source_version(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        source_id: str,
        version: int,
        fields: dict[str, Any],
        now: str,
    ) -> str:
        version_id = str(uuid.uuid4())
        wrapped, snapshot_enc, locator_enc, metadata_enc = self._encrypt_source_fields(
            tenant_id=tenant_id,
            source_id=source_id,
            version=version,
            snapshot=fields["snapshot"],
            external_ref=fields["external_ref"],
            metadata=fields["metadata"],
        )
        conn.execute(
            "INSERT INTO memory_source_versions("
            "id, source_id, tenant_id, version, snapshot_enc, locator_enc, "
            "metadata_enc, content_hash, observed_at, retention_class, "
            "deletion_state, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (
                version_id,
                source_id,
                tenant_id,
                version,
                snapshot_enc,
                locator_enc,
                metadata_enc,
                fields["content_hash"],
                fields["observed_at"],
                fields["retention_class"],
                now,
            ),
        )
        self._put_record_dek(
            conn,
            tenant_id=tenant_id,
            entity_kind="source",
            entity_id=source_id,
            version=version,
            wrapped_dek=wrapped,
            now=now,
        )
        return version_id

    def _register_result(
        self,
        *,
        source_id: str,
        source_version_id: str,
        version: int,
        fields: dict[str, Any],
        created: bool,
        version_created: bool,
    ) -> dict[str, Any]:
        return {
            "source_id": source_id,
            "source_version_id": source_version_id,
            "version": version,
            "source_kind": fields["source_kind"],
            "content_hash": fields["content_hash"],
            "observed_at": fields["observed_at"],
            "retention_class": fields["retention_class"],
            "created": created,
            "version_created": version_created,
        }

    def _replay_or_append(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        source_row: sqlite3.Row,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        source_id = str(source_row["id"])
        newest = self._newest_active_version(conn, tenant_id, source_id)
        if newest is not None and newest["content_hash"] == fields["content_hash"]:
            self._append_audit(
                conn,
                tenant_id=tenant_id,
                actor_id=tenant_id,
                action="source.register",
                target_kind="source",
                target_id=source_id,
                outcome="replayed",
                policy_reason="idempotent_replay",
            )
            return self._register_result(
                source_id=source_id,
                source_version_id=str(newest["id"]),
                version=int(newest["version"]),
                fields={
                    **fields,
                    "observed_at": str(newest["observed_at"]),
                    "retention_class": str(newest["retention_class"]),
                },
                created=False,
                version_created=False,
            )
        next_version = 1 if newest is None else int(newest["version"]) + 1
        now = utcnow_iso()
        version_id = self._insert_source_version(
            conn,
            tenant_id=tenant_id,
            source_id=source_id,
            version=next_version,
            fields=fields,
            now=now,
        )
        self._append_audit(
            conn,
            tenant_id=tenant_id,
            actor_id=tenant_id,
            action="source.register",
            target_kind="source_version",
            target_id=version_id,
            outcome="version_appended",
            policy_reason="content_changed",
        )
        return self._register_result(
            source_id=source_id,
            source_version_id=version_id,
            version=next_version,
            fields=fields,
            created=False,
            version_created=True,
        )

    def register_source(
        self,
        actor: Actor,
        *,
        source_kind: str,
        external_ref: str,
        content_hash: str,
        snapshot: bytes,
        observed_at: str,
        retention_class: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        pending_error: Exception | None = None
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            try:
                fields = validate_register_fields(
                    source_kind=source_kind,
                    external_ref=external_ref,
                    content_hash=content_hash,
                    snapshot=snapshot,
                    observed_at=observed_at,
                    retention_class=retention_class,
                    metadata=metadata,
                )
            except ValidationError as exc:
                self._append_audit(
                    conn,
                    tenant_id=tenant_id,
                    actor_id=tenant_id,
                    action="source.register",
                    target_kind="source",
                    target_id="",
                    outcome="rejected",
                    policy_reason="validation_failed",
                )
                pending_error = exc
            if pending_error is None:
                lookup_hash = keyed_lookup_hash(
                    derive_lookup_key(self._kek),
                    tenant_id,
                    fields["source_kind"],
                    fields["external_ref"],
                )
                existing = self._lookup_source(
                    conn, tenant_id, fields["source_kind"], lookup_hash
                )
                if existing is not None:
                    return self._replay_or_append(
                        conn, tenant_id=tenant_id, source_row=existing, fields=fields
                    )
                source_id = str(uuid.uuid4())
                now = utcnow_iso()
                try:
                    conn.execute(
                        "INSERT INTO memory_sources("
                        "id, tenant_id, source_kind, keyed_lookup_hash, created_at"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (source_id, tenant_id, fields["source_kind"], lookup_hash, now),
                    )
                    version_id = self._insert_source_version(
                        conn,
                        tenant_id=tenant_id,
                        source_id=source_id,
                        version=1,
                        fields=fields,
                        now=now,
                    )
                except sqlite3.IntegrityError:
                    raced = self._lookup_source(
                        conn, tenant_id, fields["source_kind"], lookup_hash
                    )
                    if raced is None:
                        raise ConflictError("Source registration conflict")
                    return self._replay_or_append(
                        conn, tenant_id=tenant_id, source_row=raced, fields=fields
                    )
                self._append_audit(
                    conn,
                    tenant_id=tenant_id,
                    actor_id=tenant_id,
                    action="source.register",
                    target_kind="source",
                    target_id=source_id,
                    outcome="created",
                    policy_reason="",
                )
                return self._register_result(
                    source_id=source_id,
                    source_version_id=version_id,
                    version=1,
                    fields=fields,
                    created=True,
                    version_created=True,
                )
        raise pending_error

    def _decrypt_version(
        self, conn: sqlite3.Connection, tenant_id: str, source_id: str, version_row: sqlite3.Row
    ) -> tuple[bytes, str, dict[str, Any]]:
        wrapped = self._get_record_dek(
            conn,
            tenant_id=tenant_id,
            entity_kind="source",
            entity_id=source_id,
            version=int(version_row["version"]),
        )
        if wrapped is None:
            raise LookupError("Wrapped DEK not found")
        version = int(version_row["version"])
        dek = unwrap_dek(
            self._kek, wrapped, record_aad(tenant_id, "source", source_id, version)
        )
        snapshot = decrypt_field(
            dek,
            bytes(version_row["snapshot_enc"]),
            field_aad(tenant_id, "source", source_id, version, FIELD_SNAPSHOT),
        )
        locator = decrypt_field(
            dek,
            bytes(version_row["locator_enc"]),
            field_aad(tenant_id, "source", source_id, version, FIELD_LOCATOR),
        ).decode("utf-8")
        metadata = json.loads(
            decrypt_field(
                dek,
                bytes(version_row["metadata_enc"]),
                field_aad(tenant_id, "source", source_id, version, FIELD_METADATA),
            ).decode("utf-8")
        )
        return snapshot, locator, metadata

    def get_source(self, actor: Actor, source_id: str) -> dict[str, object]:
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            row = conn.execute(
                "SELECT id, source_kind, created_at, deleted_at FROM memory_sources "
                "WHERE id = ? AND tenant_id = ?",
                (source_id, tenant_id),
            ).fetchone()
            version_row = None
            if row is not None:
                version_row = self._newest_active_version(conn, tenant_id, str(row["id"]))
            if row is None or version_row is None:
                self._append_audit(
                    conn,
                    tenant_id=tenant_id,
                    actor_id=tenant_id,
                    action="source.get",
                    target_kind="source",
                    target_id="",
                    outcome="denied",
                    policy_reason="not_found",
                )
            else:
                snapshot, locator, metadata = self._decrypt_version(
                    conn, tenant_id, str(row["id"]), version_row
                )
                return {
                    "source_id": str(row["id"]),
                    "source_version_id": str(version_row["id"]),
                    "version": int(version_row["version"]),
                    "source_kind": str(row["source_kind"]),
                    "content_hash": str(version_row["content_hash"]),
                    "observed_at": str(version_row["observed_at"]),
                    "retention_class": str(version_row["retention_class"]),
                    "created_at": str(row["created_at"]),
                    "deleted_at": row["deleted_at"],
                    "deletion_state": str(version_row["deletion_state"]),
                    "external_ref": locator,
                    "metadata": metadata,
                    "snapshot": snapshot,
                }
        raise NotFoundError("Source not found")

    def list_sources(
        self,
        actor: Actor,
        *,
        source_kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        kind = validate_optional_source_kind(source_kind)
        lim = validate_list_limit(limit)
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            sql = (
                "SELECT s.id AS source_id, s.source_kind, v.version, "
                "v.id AS source_version_id, v.content_hash, v.observed_at, "
                "v.retention_class, s.created_at "
                "FROM memory_sources s "
                "JOIN memory_source_versions v "
                "ON v.source_id = s.id AND v.tenant_id = s.tenant_id "
                "WHERE s.tenant_id = ? AND s.deleted_at IS NULL "
                "AND v.deletion_state = 'active' AND v.version = ("
                "SELECT MAX(v2.version) FROM memory_source_versions v2 "
                "WHERE v2.source_id = s.id AND v2.deletion_state = 'active'"
                ")"
            )
            params: list[object] = [tenant_id]
            if kind is not None:
                sql += " AND s.source_kind = ?"
                params.append(kind)
            sql += " ORDER BY v.observed_at DESC, s.id DESC LIMIT ?"
            params.append(lim)
            rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "source_id": str(row["source_id"]),
                    "source_kind": str(row["source_kind"]),
                    "version": int(row["version"]),
                    "source_version_id": str(row["source_version_id"]),
                    "content_hash": str(row["content_hash"]),
                    "observed_at": str(row["observed_at"]),
                    "retention_class": str(row["retention_class"]),
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]

    def _visible_source(
        self, conn: sqlite3.Connection, tenant_id: str, source_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT id, source_kind, tenant_id, created_at FROM memory_sources "
            "WHERE id = ? AND tenant_id = ? AND deleted_at IS NULL",
            (source_id, tenant_id),
        ).fetchone()

    def _require_visible_sources(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        source_ids: list[str],
    ) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        for source_id in source_ids:
            row = self._visible_source(conn, tenant_id, source_id)
            if row is None:
                raise ValidationError("source_ids: not visible")
            rows.append(row)
        return rows

    def _newest_claim_version(
        self, conn: sqlite3.Connection, tenant_id: str, claim_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT id, claim_id, tenant_id, version, state, sensitivity, "
            "content_enc, confidence, explanation_enc, valid_from, valid_to, "
            "expires_at, supersedes_id, created_at "
            "FROM memory_claim_versions "
            "WHERE claim_id = ? AND tenant_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (claim_id, tenant_id),
        ).fetchone()

    def _lookup_claim(
        self, conn: sqlite3.Connection, tenant_id: str, claim_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT id, tenant_id, claim_kind, created_at, deleted_at "
            "FROM memory_claims WHERE id = ? AND tenant_id = ?",
            (claim_id, tenant_id),
        ).fetchone()

    def _encrypt_claim_fields(
        self,
        *,
        tenant_id: str,
        claim_id: str,
        version: int,
        content: bytes,
        explanation: str,
    ) -> tuple[bytes, bytes, bytes]:
        dek = new_dek()
        wrap_aad = record_aad(tenant_id, "claim", claim_id, version)
        wrapped = wrap_dek(self._kek, dek, wrap_aad)
        content_enc = encrypt_field(
            dek,
            content,
            field_aad(tenant_id, "claim", claim_id, version, FIELD_CONTENT),
        )
        explanation_enc = encrypt_field(
            dek,
            explanation.encode("utf-8"),
            field_aad(tenant_id, "claim", claim_id, version, FIELD_EXPLANATION),
        )
        return wrapped, content_enc, explanation_enc

    def _decrypt_claim_fields(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        claim_id: str,
        version_row: sqlite3.Row,
    ) -> tuple[bytes, str]:
        version = int(version_row["version"])
        wrapped = self._get_record_dek(
            conn,
            tenant_id=tenant_id,
            entity_kind="claim",
            entity_id=claim_id,
            version=version,
        )
        if wrapped is None:
            raise LookupError("Wrapped DEK not found")
        dek = unwrap_dek(
            self._kek, wrapped, record_aad(tenant_id, "claim", claim_id, version)
        )
        content = decrypt_field(
            dek,
            bytes(version_row["content_enc"]),
            field_aad(tenant_id, "claim", claim_id, version, FIELD_CONTENT),
        )
        explanation = decrypt_field(
            dek,
            bytes(version_row["explanation_enc"]),
            field_aad(tenant_id, "claim", claim_id, version, FIELD_EXPLANATION),
        ).decode("utf-8")
        return content, explanation

    def _insert_support(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        claim_id: str,
        source_ids: list[str],
        now: str,
    ) -> None:
        existing = {
            str(row["source_id"])
            for row in conn.execute(
                "SELECT source_id FROM memory_support WHERE claim_id = ? AND tenant_id = ?",
                (claim_id, tenant_id),
            )
        }
        for source_id in source_ids:
            if source_id in existing:
                continue
            conn.execute(
                "INSERT INTO memory_support("
                "id, tenant_id, claim_id, source_id, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), tenant_id, claim_id, source_id, now),
            )
            existing.add(source_id)

    def _insert_claim_version(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        claim_id: str,
        version: int,
        state: MemoryState,
        sensitivity: str,
        content: bytes,
        confidence: float,
        explanation: str,
        valid_from: str | None,
        valid_to: str | None,
        expires_at: str | None,
        supersedes_id: str | None,
        now: str,
    ) -> str:
        version_id = str(uuid.uuid4())
        wrapped, content_enc, explanation_enc = self._encrypt_claim_fields(
            tenant_id=tenant_id,
            claim_id=claim_id,
            version=version,
            content=content,
            explanation=explanation,
        )
        conn.execute(
            "INSERT INTO memory_claim_versions("
            "id, claim_id, tenant_id, version, state, sensitivity, content_enc, "
            "confidence, explanation_enc, valid_from, valid_to, expires_at, "
            "supersedes_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                claim_id,
                tenant_id,
                version,
                state.value,
                sensitivity,
                content_enc,
                confidence,
                explanation_enc,
                valid_from,
                valid_to,
                expires_at,
                supersedes_id,
                now,
            ),
        )
        self._put_record_dek(
            conn,
            tenant_id=tenant_id,
            entity_kind="claim",
            entity_id=claim_id,
            version=version,
            wrapped_dek=wrapped,
            now=now,
        )
        return version_id

    def _claim_result(
        self,
        *,
        claim_id: str,
        claim_version_id: str,
        version: int,
        claim_kind: str,
        state: MemoryState,
        sensitivity: str,
        confidence: float,
        expires_at: str | None,
        created_at: str,
        supersedes_id: str | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "claim_id": claim_id,
            "claim_version_id": claim_version_id,
            "version": version,
            "claim_kind": claim_kind,
            "state": state.value,
            "sensitivity": sensitivity,
            "confidence": confidence,
            "expires_at": expires_at,
            "created_at": created_at,
        }
        if supersedes_id is not None:
            result["supersedes_id"] = supersedes_id
        return result

    def _reject_claim(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        action: str,
        policy_reason: str,
    ) -> None:
        self._append_audit(
            conn,
            tenant_id=tenant_id,
            actor_id=tenant_id,
            action=action,
            target_kind="claim",
            target_id="",
            outcome="rejected",
            policy_reason=policy_reason,
        )

    def _deny_claim(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        action: str,
    ) -> None:
        self._append_audit(
            conn,
            tenant_id=tenant_id,
            actor_id=tenant_id,
            action=action,
            target_kind="claim",
            target_id="",
            outcome="denied",
            policy_reason="not_found",
        )

    def create_candidate(
        self,
        actor: Actor,
        *,
        claim_kind: str,
        content: bytes,
        confidence: float,
        explanation: str | None,
        sensitivity: str,
        source_ids: list[str],
        valid_from: str | None = None,
        valid_to: str | None = None,
        expires_at: str,
    ) -> dict[str, object]:
        pending_error: Exception | None = None
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            try:
                fields = validate_create_candidate_fields(
                    claim_kind=claim_kind,
                    content=content,
                    confidence=confidence,
                    explanation=explanation,
                    sensitivity=sensitivity,
                    source_ids=source_ids,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    expires_at=expires_at,
                )
                self._require_visible_sources(conn, tenant_id, fields["source_ids"])
            except ValidationError as exc:
                self._reject_claim(
                    conn,
                    tenant_id=tenant_id,
                    action="claim.create_candidate",
                    policy_reason="validation_failed",
                )
                pending_error = exc
            if pending_error is None:
                claim_id = str(uuid.uuid4())
                now = utcnow_iso()
                conn.execute(
                    "INSERT INTO memory_claims("
                    "id, tenant_id, claim_kind, created_at"
                    ") VALUES (?, ?, ?, ?)",
                    (claim_id, tenant_id, fields["claim_kind"], now),
                )
                version_id = self._insert_claim_version(
                    conn,
                    tenant_id=tenant_id,
                    claim_id=claim_id,
                    version=1,
                    state=MemoryState.CANDIDATE,
                    sensitivity=fields["sensitivity"],
                    content=fields["content"],
                    confidence=fields["confidence"],
                    explanation=fields["explanation"],
                    valid_from=fields["valid_from"],
                    valid_to=fields["valid_to"],
                    expires_at=fields["expires_at"],
                    supersedes_id=None,
                    now=now,
                )
                self._insert_support(
                    conn,
                    tenant_id=tenant_id,
                    claim_id=claim_id,
                    source_ids=fields["source_ids"],
                    now=now,
                )
                self._append_audit(
                    conn,
                    tenant_id=tenant_id,
                    actor_id=tenant_id,
                    action="claim.create_candidate",
                    target_kind="claim",
                    target_id=claim_id,
                    outcome="created",
                    policy_reason="",
                )
                return self._claim_result(
                    claim_id=claim_id,
                    claim_version_id=version_id,
                    version=1,
                    claim_kind=fields["claim_kind"],
                    state=MemoryState.CANDIDATE,
                    sensitivity=fields["sensitivity"],
                    confidence=fields["confidence"],
                    expires_at=fields["expires_at"],
                    created_at=now,
                )
        raise pending_error

    def _resolve_confirmation_sources(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        *,
        confirmation_source_id: str | None,
        reliable_source_id: str | None,
        memory_mode: str,
    ) -> list[str]:
        if confirmation_source_id is None and reliable_source_id is None:
            raise ValidationError("confirmation: required")
        source_ids: list[str] = []
        if confirmation_source_id is not None:
            if not isinstance(confirmation_source_id, str) or not confirmation_source_id:
                raise ValidationError("confirmation: required")
            row = self._visible_source(conn, tenant_id, confirmation_source_id)
            if row is None:
                raise ValidationError("confirmation: not visible")
            if str(row["source_kind"]) != "user_confirmation":
                raise ValidationError("confirmation: required")
            source_ids.append(str(row["id"]))
        if reliable_source_id is not None:
            if not isinstance(reliable_source_id, str) or not reliable_source_id:
                raise ValidationError("confirmation: required")
            row = self._visible_source(conn, tenant_id, reliable_source_id)
            if row is None:
                raise ValidationError("confirmation: not visible")
            if not confirmation_source_is_reliable(str(row["source_kind"]), memory_mode):
                raise ValidationError("confirmation: required")
            source_ids.append(str(row["id"]))
        return source_ids

    def confirm_claim(
        self,
        actor: Actor,
        claim_id: str,
        *,
        confirmation_source_id: str | None = None,
        reliable_source_id: str | None = None,
    ) -> dict[str, object]:
        pending_error: Exception | None = None
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            claim_row = self._lookup_claim(conn, tenant_id, claim_id)
            newest = None
            if claim_row is not None and claim_row["deleted_at"] is None:
                newest = self._newest_claim_version(conn, tenant_id, str(claim_row["id"]))
            if claim_row is None or newest is None:
                self._deny_claim(conn, tenant_id=tenant_id, action="claim.confirm")
                pending_error = NotFoundError("Claim not found")
            elif str(newest["state"]) != MemoryState.CANDIDATE:
                self._reject_claim(
                    conn,
                    tenant_id=tenant_id,
                    action="claim.confirm",
                    policy_reason="invalid_transition",
                )
                pending_error = ValidationError("claim: invalid transition")
            else:
                try:
                    support_ids = self._resolve_confirmation_sources(
                        conn,
                        tenant_id,
                        confirmation_source_id=confirmation_source_id,
                        reliable_source_id=reliable_source_id,
                        memory_mode=self._memory_mode_on(conn, tenant_id),
                    )
                except ValidationError as exc:
                    reason = (
                        "confirmation_required"
                        if "required" in str(exc)
                        else "validation_failed"
                    )
                    self._reject_claim(
                        conn,
                        tenant_id=tenant_id,
                        action="claim.confirm",
                        policy_reason=reason,
                    )
                    pending_error = exc
                if pending_error is None:
                    content, explanation = self._decrypt_claim_fields(
                        conn,
                        tenant_id=tenant_id,
                        claim_id=str(claim_row["id"]),
                        version_row=newest,
                    )
                    now = utcnow_iso()
                    next_version = int(newest["version"]) + 1
                    previous_id = str(newest["id"])
                    conn.execute(
                        "UPDATE memory_claim_versions SET state = ? "
                        "WHERE id = ? AND tenant_id = ?",
                        (MemoryState.SUPERSEDED.value, previous_id, tenant_id),
                    )
                    version_id = self._insert_claim_version(
                        conn,
                        tenant_id=tenant_id,
                        claim_id=str(claim_row["id"]),
                        version=next_version,
                        state=MemoryState.CONFIRMED,
                        sensitivity=str(newest["sensitivity"]),
                        content=content,
                        confidence=float(newest["confidence"]),
                        explanation=explanation,
                        valid_from=newest["valid_from"],
                        valid_to=newest["valid_to"],
                        expires_at=None,
                        supersedes_id=previous_id,
                        now=now,
                    )
                    self._insert_support(
                        conn,
                        tenant_id=tenant_id,
                        claim_id=str(claim_row["id"]),
                        source_ids=support_ids,
                        now=now,
                    )
                    self._append_audit(
                        conn,
                        tenant_id=tenant_id,
                        actor_id=tenant_id,
                        action="claim.confirm",
                        target_kind="claim_version",
                        target_id=version_id,
                        outcome="confirmed",
                        policy_reason="",
                    )
                    return self._claim_result(
                        claim_id=str(claim_row["id"]),
                        claim_version_id=version_id,
                        version=next_version,
                        claim_kind=str(claim_row["claim_kind"]),
                        state=MemoryState.CONFIRMED,
                        sensitivity=str(newest["sensitivity"]),
                        confidence=float(newest["confidence"]),
                        expires_at=None,
                        created_at=now,
                        supersedes_id=previous_id,
                    )
        raise pending_error

    def correct_claim(
        self,
        actor: Actor,
        claim_id: str,
        *,
        content: bytes,
        explanation: str | None,
        source_ids: list[str],
        confidence: float | None = None,
        sensitivity: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, object]:
        pending_error: Exception | None = None
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            claim_row = self._lookup_claim(conn, tenant_id, claim_id)
            newest = None
            if claim_row is not None and claim_row["deleted_at"] is None:
                newest = self._newest_claim_version(conn, tenant_id, str(claim_row["id"]))
            if claim_row is None or newest is None:
                self._deny_claim(conn, tenant_id=tenant_id, action="claim.correct")
                pending_error = NotFoundError("Claim not found")
            elif str(newest["state"]) not in {item.value for item in ACTIVE_READ_STATES}:
                self._reject_claim(
                    conn,
                    tenant_id=tenant_id,
                    action="claim.correct",
                    policy_reason="invalid_transition",
                )
                pending_error = ValidationError("claim: invalid transition")
            else:
                try:
                    fields = validate_correct_fields(
                        content=content,
                        explanation=explanation,
                        source_ids=source_ids,
                        confidence=confidence,
                        sensitivity=sensitivity,
                        valid_from=valid_from,
                        valid_to=valid_to,
                        expires_at=expires_at,
                    )
                    next_valid_from = (
                        newest["valid_from"]
                        if fields["valid_from"] is None
                        else fields["valid_from"]
                    )
                    next_valid_to = (
                        newest["valid_to"]
                        if fields["valid_to"] is None
                        else fields["valid_to"]
                    )
                    validate_valid_time_order(next_valid_from, next_valid_to)
                    self._require_visible_sources(conn, tenant_id, fields["source_ids"])
                except ValidationError as exc:
                    self._reject_claim(
                        conn,
                        tenant_id=tenant_id,
                        action="claim.correct",
                        policy_reason="validation_failed",
                    )
                    pending_error = exc
                if pending_error is None:
                    now = utcnow_iso()
                    next_version = int(newest["version"]) + 1
                    previous_id = str(newest["id"])
                    previous_state = MemoryState(str(newest["state"]))
                    next_expires = fields["expires_at"]
                    if next_expires is None:
                        next_expires = newest["expires_at"]
                    if previous_state is MemoryState.CONFIRMED and fields["expires_at"] is None:
                        next_expires = None
                    conn.execute(
                        "UPDATE memory_claim_versions SET state = ? "
                        "WHERE id = ? AND tenant_id = ?",
                        (MemoryState.SUPERSEDED.value, previous_id, tenant_id),
                    )
                    version_id = self._insert_claim_version(
                        conn,
                        tenant_id=tenant_id,
                        claim_id=str(claim_row["id"]),
                        version=next_version,
                        state=previous_state,
                        sensitivity=fields["sensitivity"] or str(newest["sensitivity"]),
                        content=fields["content"],
                        confidence=(
                            newest["confidence"]
                            if fields["confidence"] is None
                            else fields["confidence"]
                        ),
                        explanation=fields["explanation"],
                        valid_from=next_valid_from,
                        valid_to=next_valid_to,
                        expires_at=next_expires,
                        supersedes_id=previous_id,
                        now=now,
                    )
                    self._insert_support(
                        conn,
                        tenant_id=tenant_id,
                        claim_id=str(claim_row["id"]),
                        source_ids=fields["source_ids"],
                        now=now,
                    )
                    self._append_audit(
                        conn,
                        tenant_id=tenant_id,
                        actor_id=tenant_id,
                        action="claim.correct",
                        target_kind="claim_version",
                        target_id=version_id,
                        outcome="superseded",
                        policy_reason="",
                    )
                    return self._claim_result(
                        claim_id=str(claim_row["id"]),
                        claim_version_id=version_id,
                        version=next_version,
                        claim_kind=str(claim_row["claim_kind"]),
                        state=previous_state,
                        sensitivity=fields["sensitivity"] or str(newest["sensitivity"]),
                        confidence=(
                            float(newest["confidence"])
                            if fields["confidence"] is None
                            else fields["confidence"]
                        ),
                        expires_at=next_expires,
                        created_at=now,
                        supersedes_id=previous_id,
                    )
        raise pending_error

    def list_active_claims(
        self,
        actor: Actor,
        *,
        claim_kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        kind = validate_optional_claim_kind(claim_kind)
        lim = validate_list_limit(limit)
        now = utcnow_iso()
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            sql = (
                "SELECT c.id AS claim_id, v.version, v.id AS claim_version_id, "
                "c.claim_kind, v.state, v.sensitivity, v.confidence, "
                "v.expires_at, v.created_at "
                "FROM memory_claims c "
                "JOIN memory_claim_versions v "
                "ON v.claim_id = c.id AND v.tenant_id = c.tenant_id "
                "WHERE c.tenant_id = ? AND c.deleted_at IS NULL "
                "AND v.version = ("
                "SELECT MAX(v2.version) FROM memory_claim_versions v2 "
                "WHERE v2.claim_id = c.id"
                ") "
                "AND v.state NOT IN ('superseded', 'deleted') "
                "AND v.sensitivity <> 'restricted' "
                "AND (v.expires_at IS NULL OR v.expires_at > ?)"
            )
            params: list[object] = [tenant_id, now]
            if kind is not None:
                sql += " AND c.claim_kind = ?"
                params.append(kind)
            sql += " ORDER BY v.created_at DESC, v.rowid DESC LIMIT ?"
            params.append(lim)
            rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "claim_id": str(row["claim_id"]),
                    "version": int(row["version"]),
                    "claim_version_id": str(row["claim_version_id"]),
                    "claim_kind": str(row["claim_kind"]),
                    "state": str(row["state"]),
                    "sensitivity": str(row["sensitivity"]),
                    "confidence": float(row["confidence"]),
                    "expires_at": row["expires_at"],
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]

    def _try_decrypt_source_version(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        source_id: str,
        version_row: sqlite3.Row,
    ) -> tuple[bytes, str, dict[str, Any]] | None:
        if version_row["snapshot_enc"] is None:
            return None
        try:
            return self._decrypt_version(conn, tenant_id, source_id, version_row)
        except (LookupError, TypeError, ValueError, CryptoError):
            return None

    def _try_decrypt_claim_version(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        claim_id: str,
        version_row: sqlite3.Row,
    ) -> tuple[bytes, str] | None:
        if version_row["content_enc"] is None:
            return None
        try:
            return self._decrypt_claim_fields(
                conn,
                tenant_id=tenant_id,
                claim_id=claim_id,
                version_row=version_row,
            )
        except (LookupError, TypeError, ValueError, CryptoError):
            return None

    def _owner_source_label(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        source_id: str,
        version_row: sqlite3.Row | None,
    ) -> str:
        if version_row is None:
            return ""
        decrypted = self._try_decrypt_source_version(
            conn, tenant_id, source_id, version_row
        )
        if decrypted is None:
            return ""
        return _source_label(decrypted[2])

    def _owner_claim_provenance(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        claim_id: str,
    ) -> list[dict[str, object]]:
        rows = conn.execute(
            "SELECT s.id AS source_id, s.source_kind, v.snapshot_enc, v.locator_enc, "
            "v.metadata_enc, v.version "
            "FROM memory_support m "
            "JOIN memory_sources s ON s.id = m.source_id AND s.tenant_id = m.tenant_id "
            "LEFT JOIN memory_source_versions v "
            "ON v.source_id = s.id AND v.tenant_id = s.tenant_id AND v.version = ("
            "SELECT MAX(v2.version) FROM memory_source_versions v2 "
            "WHERE v2.source_id = s.id"
            ") "
            "WHERE m.tenant_id = ? AND m.claim_id = ? "
            "ORDER BY s.created_at, s.id",
            (tenant_id, claim_id),
        ).fetchall()
        provenance: list[dict[str, object]] = []
        for row in rows:
            provenance.append(
                {
                    "source_kind": str(row["source_kind"]),
                    "label": self._owner_source_label(
                        conn, tenant_id, str(row["source_id"]), row
                    ),
                }
            )
        return provenance

    def export_owner_data(self, actor: Actor) -> dict[str, object]:
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            source_rows = conn.execute(
                "SELECT s.id AS source_id, s.source_kind, s.created_at, "
                "v.version, v.observed_at, v.retention_class, v.deletion_state, "
                "v.snapshot_enc, v.locator_enc, v.metadata_enc "
                "FROM memory_sources s "
                "JOIN memory_source_versions v "
                "ON v.source_id = s.id AND v.tenant_id = s.tenant_id "
                "WHERE s.tenant_id = ? AND s.deleted_at IS NULL "
                "AND v.version = ("
                "SELECT MAX(v2.version) FROM memory_source_versions v2 "
                "WHERE v2.source_id = s.id"
                ") "
                "ORDER BY v.observed_at DESC, s.id DESC",
                (tenant_id,),
            ).fetchall()
            sources: list[dict[str, object]] = []
            for row in source_rows:
                source_id = str(row["source_id"])
                decrypted = self._try_decrypt_source_version(
                    conn, tenant_id, source_id, row
                )
                snapshot = ""
                locator = ""
                label = ""
                if decrypted is not None:
                    snapshot = _export_bytes(decrypted[0])
                    locator = decrypted[1]
                    label = _source_label(decrypted[2])
                sources.append(
                    {
                        "source_id": source_id,
                        "source_kind": str(row["source_kind"]),
                        "label": label,
                        "version": int(row["version"]),
                        "observed_at": str(row["observed_at"]),
                        "created_at": str(row["created_at"]),
                        "retention_class": str(row["retention_class"]),
                        "deletion_state": str(row["deletion_state"]),
                        "snapshot": snapshot,
                        "external_ref": locator,
                    }
                )
            claim_rows = conn.execute(
                "SELECT id, claim_kind, created_at FROM memory_claims "
                "WHERE tenant_id = ? AND deleted_at IS NULL "
                "ORDER BY created_at, id",
                (tenant_id,),
            ).fetchall()
            claims: list[dict[str, object]] = []
            for claim in claim_rows:
                claim_id = str(claim["id"])
                versions = conn.execute(
                    "SELECT id, version, state, sensitivity, confidence, "
                    "content_enc, explanation_enc, valid_from, valid_to, "
                    "expires_at, created_at FROM memory_claim_versions "
                    "WHERE claim_id = ? AND tenant_id = ? ORDER BY version",
                    (claim_id, tenant_id),
                ).fetchall()
                lifecycle: list[dict[str, object]] = []
                for version in versions:
                    decrypted = self._try_decrypt_claim_version(
                        conn, tenant_id, claim_id, version
                    )
                    content = ""
                    explanation = ""
                    if decrypted is not None:
                        content = _export_bytes(decrypted[0])
                        explanation = decrypted[1]
                    lifecycle.append(
                        {
                            "version": int(version["version"]),
                            "state": str(version["state"]),
                            "confidence": float(version["confidence"]),
                            "sensitivity": str(version["sensitivity"]),
                            "created_at": str(version["created_at"]),
                            "expires_at": version["expires_at"],
                            "valid_from": version["valid_from"],
                            "valid_to": version["valid_to"],
                            "content": content,
                            "explanation": explanation,
                        }
                    )
                claims.append(
                    {
                        "claim_id": claim_id,
                        "claim_kind": str(claim["claim_kind"]),
                        "created_at": str(claim["created_at"]),
                        "lifecycle": lifecycle,
                        "provenance": self._owner_claim_provenance(
                            conn, tenant_id, claim_id
                        ),
                    }
                )
            receipts = [
                {
                    "receipt_id": str(row["receipt_id"]),
                    "operation": str(row["operation"]),
                    "target_kind": str(row["target_kind"]),
                    "outcome": str(row["outcome"]),
                    "erased_at": str(row["erased_at"]),
                }
                for row in conn.execute(
                    "SELECT receipt_id, operation, target_kind, outcome, erased_at "
                    "FROM memory_deletion_manifest WHERE tenant_id = ? "
                    "ORDER BY erased_at, receipt_id",
                    (tenant_id,),
                )
            ]
            self._append_audit(
                conn,
                tenant_id=tenant_id,
                actor_id=tenant_id,
                action="owner.export",
                target_kind="export",
                target_id="",
                outcome="exported",
                policy_reason="",
            )
            return {
                "exported_at": utcnow_iso(),
                "sources": sources,
                "claims": claims,
                "deletion_receipts": receipts,
            }

    def explain_claim(self, actor: Actor, claim_id: str) -> dict[str, object]:
        pending_error: Exception | None = None
        explanation: dict[str, object] | None = None
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            row = None
            newest = None
            if isinstance(claim_id, str) and claim_id:
                row = self._lookup_claim(conn, tenant_id, claim_id)
                if row is not None and row["deleted_at"] is None:
                    newest = self._newest_claim_version(
                        conn, tenant_id, str(row["id"])
                    )
            if (
                row is None
                or newest is None
                or str(newest["state"]) == MemoryState.DELETED
            ):
                self._deny_claim(conn, tenant_id=tenant_id, action="claim.explain")
                pending_error = NotFoundError("Claim not found")
            else:
                explanation = {
                    "claim_id": str(row["id"]),
                    "claim_kind": str(row["claim_kind"]),
                    "state": str(newest["state"]),
                    "confidence": float(newest["confidence"]),
                    "created_at": str(newest["created_at"]),
                    "expires_at": newest["expires_at"],
                    "valid_from": newest["valid_from"],
                    "valid_to": newest["valid_to"],
                    "sources": self._owner_claim_provenance(
                        conn, tenant_id, str(row["id"])
                    ),
                }
                self._append_audit(
                    conn,
                    tenant_id=tenant_id,
                    actor_id=tenant_id,
                    action="claim.explain",
                    target_kind="claim",
                    target_id=str(row["id"]),
                    outcome="explained",
                    policy_reason="",
                )
        if pending_error is not None:
            raise pending_error
        if explanation is None:
            raise NotFoundError("Claim not found")
        return explanation

    def _compact_after_erasure(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                compact_sqlite_store(conn)
            except sqlite3.OperationalError:
                logger.exception(
                    "personal-memory compaction failed after committed erasure"
                )
            finally:
                conn.close()

    def _record_erasure(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        operation: str,
        target_kind: str,
        now: str,
    ) -> ErasureReceipt:
        receipt = new_erasure_receipt(
            operation=operation, target_kind=target_kind, erased_at=now
        )
        conn.execute(
            "INSERT INTO memory_deletion_manifest("
            "receipt_id, tenant_id, operation, target_kind, outcome, erased_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                receipt.receipt_id,
                tenant_id,
                receipt.operation,
                receipt.target_kind,
                receipt.outcome,
                receipt.erased_at,
            ),
        )
        self._append_audit(
            conn,
            tenant_id=tenant_id,
            actor_id=tenant_id,
            action=operation,
            target_kind=target_kind,
            target_id=receipt.receipt_id,
            outcome="erased",
            policy_reason="",
        )
        return receipt

    def _destroy_keys(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        entity_kind: str,
        entity_id: str,
        now: str,
    ) -> None:
        rows = conn.execute(
            "SELECT id, length(wrapped_dek) AS n FROM memory_keys "
            "WHERE tenant_id = ? AND entity_kind = ? AND entity_id = ?",
            (tenant_id, entity_kind, entity_id),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE memory_keys SET wrapped_dek = zeroblob(?), destroyed_at = ? "
                "WHERE id = ?",
                (int(row["n"] or 0), now, row["id"]),
            )
        conn.execute(
            "DELETE FROM memory_keys WHERE tenant_id = ? AND entity_kind = ? "
            "AND entity_id = ?",
            (tenant_id, entity_kind, entity_id),
        )

    def _destroy_claim_blobs(
        self, conn: sqlite3.Connection, *, tenant_id: str, claim_id: str
    ) -> None:
        conn.execute(
            "UPDATE memory_claim_versions SET "
            "content_enc = CASE WHEN content_enc IS NULL THEN NULL "
            "ELSE zeroblob(length(content_enc)) END, "
            "explanation_enc = CASE WHEN explanation_enc IS NULL THEN NULL "
            "ELSE zeroblob(length(explanation_enc)) END "
            "WHERE claim_id = ? AND tenant_id = ?",
            (claim_id, tenant_id),
        )
        conn.execute(
            "UPDATE memory_claim_versions SET content_enc = NULL, explanation_enc = NULL "
            "WHERE claim_id = ? AND tenant_id = ?",
            (claim_id, tenant_id),
        )

    def _destroy_source_blobs(
        self, conn: sqlite3.Connection, *, tenant_id: str, source_id: str
    ) -> None:
        conn.execute(
            "UPDATE memory_source_versions SET "
            "snapshot_enc = CASE WHEN snapshot_enc IS NULL THEN NULL "
            "ELSE zeroblob(length(snapshot_enc)) END, "
            "locator_enc = CASE WHEN locator_enc IS NULL THEN NULL "
            "ELSE zeroblob(length(locator_enc)) END, "
            "metadata_enc = CASE WHEN metadata_enc IS NULL THEN NULL "
            "ELSE zeroblob(length(metadata_enc)) END "
            "WHERE source_id = ? AND tenant_id = ?",
            (source_id, tenant_id),
        )
        conn.execute(
            "UPDATE memory_source_versions SET snapshot_enc = NULL, locator_enc = NULL, "
            "metadata_enc = NULL WHERE source_id = ? AND tenant_id = ?",
            (source_id, tenant_id),
        )

    def _erasable_claim(
        self, conn: sqlite3.Connection, tenant_id: str, claim_id: str
    ) -> sqlite3.Row | None:
        if not isinstance(claim_id, str) or not claim_id:
            return None
        row = self._lookup_claim(conn, tenant_id, claim_id)
        if row is None or row["deleted_at"] is not None:
            return None
        newest = self._newest_claim_version(conn, tenant_id, str(row["id"]))
        if newest is None or str(newest["state"]) == MemoryState.DELETED:
            return None
        return row

    def _erasable_source(
        self, conn: sqlite3.Connection, tenant_id: str, source_id: str
    ) -> sqlite3.Row | None:
        if not isinstance(source_id, str) or not source_id:
            return None
        return conn.execute(
            "SELECT id, tenant_id FROM memory_sources "
            "WHERE id = ? AND tenant_id = ? AND deleted_at IS NULL",
            (source_id, tenant_id),
        ).fetchone()

    def _deny_source(
        self, conn: sqlite3.Connection, *, tenant_id: str, action: str
    ) -> None:
        self._append_audit(
            conn,
            tenant_id=tenant_id,
            actor_id=tenant_id,
            action=action,
            target_kind="source",
            target_id="",
            outcome="denied",
            policy_reason="not_found",
        )

    def _erase_claim_on(
        self, conn: sqlite3.Connection, *, tenant_id: str, claim_id: str
    ) -> ErasureReceipt:
        from .fts_index import delete_claim_fts_rows

        now = utcnow_iso()
        delete_claim_fts_rows(conn, tenant_id=tenant_id, claim_id=claim_id)
        self._destroy_claim_blobs(conn, tenant_id=tenant_id, claim_id=claim_id)
        conn.execute(
            "UPDATE memory_claim_versions SET state = ? "
            "WHERE claim_id = ? AND tenant_id = ?",
            (MemoryState.DELETED.value, claim_id, tenant_id),
        )
        self._destroy_keys(
            conn,
            tenant_id=tenant_id,
            entity_kind="claim",
            entity_id=claim_id,
            now=now,
        )
        conn.execute(
            "DELETE FROM memory_support WHERE tenant_id = ? AND claim_id = ?",
            (tenant_id, claim_id),
        )
        conn.execute(
            "UPDATE memory_claims SET deleted_at = ? WHERE id = ? AND tenant_id = ?",
            (now, claim_id, tenant_id),
        )
        return self._record_erasure(
            conn,
            tenant_id=tenant_id,
            operation="claim.erase",
            target_kind="claim",
            now=now,
        )

    def _erase_source_on(
        self, conn: sqlite3.Connection, *, tenant_id: str, source_id: str
    ) -> ErasureReceipt:
        dependents = [
            str(row["claim_id"])
            for row in conn.execute(
                "SELECT DISTINCT claim_id FROM memory_support "
                "WHERE tenant_id = ? AND source_id = ?",
                (tenant_id, source_id),
            )
        ]
        for claim_id in dependents:
            if self._erasable_claim(conn, tenant_id, claim_id) is not None:
                self._erase_claim_on(conn, tenant_id=tenant_id, claim_id=claim_id)
        now = utcnow_iso()
        self._destroy_source_blobs(conn, tenant_id=tenant_id, source_id=source_id)
        conn.execute(
            "UPDATE memory_source_versions SET content_hash = '', deletion_state = 'deleted' "
            "WHERE source_id = ? AND tenant_id = ?",
            (source_id, tenant_id),
        )
        self._destroy_keys(
            conn,
            tenant_id=tenant_id,
            entity_kind="source",
            entity_id=source_id,
            now=now,
        )
        conn.execute(
            "DELETE FROM memory_support WHERE tenant_id = ? AND source_id = ?",
            (tenant_id, source_id),
        )
        conn.execute(
            "UPDATE memory_sources SET deleted_at = ?, keyed_lookup_hash = '' "
            "WHERE id = ? AND tenant_id = ?",
            (now, source_id, tenant_id),
        )
        return self._record_erasure(
            conn,
            tenant_id=tenant_id,
            operation="source.erase",
            target_kind="source",
            now=now,
        )

    def erase_bound_claims(
        self,
        actor: Actor,
        targets: list[dict[str, object]],
        *,
        require_state: str | None = None,
    ) -> list[ErasureReceipt]:
        """Erase an exact reviewed target set, or deny the entire batch.

        Rechecks claim id, version, version id, and content hash inside one
        BEGIN IMMEDIATE transaction. Compaction runs only after commit.
        """
        if not targets:
            raise ConflictError("Delete target set is empty")
        pending_error: Exception | None = None
        receipts: list[ErasureReceipt] = []
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            checked_ids: list[str] = []
            for raw in targets:
                claim_id = str(raw.get("claim_id") or "")
                expected_version_id = str(raw.get("claim_version_id") or "")
                expected_hash = str(raw.get("content_hash") or "")
                try:
                    expected_version = int(raw.get("version"))
                except (TypeError, ValueError):
                    self._deny_claim(conn, tenant_id=tenant_id, action="claim.erase")
                    pending_error = ConflictError("Delete target set changed")
                    break
                row = self._erasable_claim(conn, tenant_id, claim_id)
                newest = (
                    self._newest_claim_version(conn, tenant_id, str(row["id"]))
                    if row is not None
                    else None
                )
                if row is None or newest is None:
                    self._deny_claim(conn, tenant_id=tenant_id, action="claim.erase")
                    pending_error = ConflictError("Delete target set changed")
                    break
                if require_state is not None and str(newest["state"]) != require_state:
                    self._deny_claim(conn, tenant_id=tenant_id, action="claim.erase")
                    pending_error = ConflictError("Delete target set changed")
                    break
                try:
                    content, _explanation = self._decrypt_claim_fields(
                        conn,
                        tenant_id=tenant_id,
                        claim_id=str(row["id"]),
                        version_row=newest,
                    )
                    actual_hash = content_hash_for(content)
                except (LookupError, CryptoError, TypeError, ValueError):
                    self._deny_claim(conn, tenant_id=tenant_id, action="claim.erase")
                    pending_error = ConflictError("Delete target set changed")
                    break
                if (
                    int(newest["version"]) != int(expected_version)
                    or str(newest["id"]) != expected_version_id
                    or actual_hash != expected_hash
                ):
                    self._deny_claim(conn, tenant_id=tenant_id, action="claim.erase")
                    pending_error = ConflictError("Delete target set changed")
                    break
                checked_ids.append(str(row["id"]))
            if pending_error is None:
                for claim_id in checked_ids:
                    receipts.append(
                        self._erase_claim_on(
                            conn, tenant_id=tenant_id, claim_id=claim_id
                        )
                    )
        if pending_error is not None:
            raise pending_error
        if receipts:
            self._compact_after_erasure()
        return receipts

    def erase_claim(self, actor: Actor, claim_id: str) -> ErasureReceipt:
        pending_error: Exception | None = None
        receipt: ErasureReceipt | None = None
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            row = self._erasable_claim(conn, tenant_id, claim_id)
            if row is None:
                self._deny_claim(conn, tenant_id=tenant_id, action="claim.erase")
                pending_error = NotFoundError("Claim not found")
            else:
                receipt = self._erase_claim_on(
                    conn, tenant_id=tenant_id, claim_id=str(row["id"])
                )
        if pending_error is not None:
            raise pending_error
        self._compact_after_erasure()
        if receipt is None:
            raise NotFoundError("Claim not found")
        return receipt

    def erase_source(self, actor: Actor, source_id: str) -> ErasureReceipt:
        pending_error: Exception | None = None
        receipt: ErasureReceipt | None = None
        with self._tx() as conn:
            tenant_id = self._ensure_principal_on(conn, actor)
            row = self._erasable_source(conn, tenant_id, source_id)
            if row is None:
                self._deny_source(conn, tenant_id=tenant_id, action="source.erase")
                pending_error = NotFoundError("Source not found")
            else:
                receipt = self._erase_source_on(
                    conn, tenant_id=tenant_id, source_id=str(row["id"])
                )
        if pending_error is not None:
            raise pending_error
        self._compact_after_erasure()
        if receipt is None:
            raise NotFoundError("Source not found")
        return receipt

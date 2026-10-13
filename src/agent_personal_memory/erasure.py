"""Physical erasure receipts and SQLite compaction helpers."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

BACKUP_RETENTION_NOTE = (
    "Encrypted backups may retain ciphertext until the recorded retention "
    "policy expires."
)


@dataclass(frozen=True)
class ErasureReceipt:
    operation: str
    target_kind: str
    receipt_id: str
    outcome: str
    erased_at: str
    backup_retention_note: str


def new_erasure_receipt(*, operation: str, target_kind: str, erased_at: str) -> ErasureReceipt:
    return ErasureReceipt(
        operation=operation,
        target_kind=target_kind,
        receipt_id=str(uuid.uuid4()),
        outcome="erased",
        erased_at=erased_at,
        backup_retention_note=BACKUP_RETENTION_NOTE,
    )


def compact_sqlite_store(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

"""Optional rebuildable FTS5 derived index for authorized claim search.

Partial port (T2): only the erasure-side helpers that ``store.py`` needs are
present (``delete_claim_fts_rows`` plus the shared table-name/existence
helpers). The index build and FTS ranking functions depend on the
authorized-read layer (query/ranking) and land with that later task.
"""

from __future__ import annotations

_FTS_TABLE = "memory_claim_fts_index"


def _fts_table_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_FTS_TABLE,),
    ).fetchone()
    return row is not None


def delete_claim_fts_rows(
    conn,
    *,
    tenant_id: str,
    claim_id: str,
) -> None:
    """Remove one claim's plaintext from the derived FTS table immediately."""
    if not _fts_table_exists(conn):
        return
    conn.execute(
        f"DELETE FROM {_FTS_TABLE} WHERE tenant_id = ? AND claim_id = ?",
        (tenant_id, claim_id),
    )


def _ensure_fts_table(conn) -> None:
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5(
            claim_id UNINDEXED,
            tenant_id UNINDEXED,
            body,
            tokenize='unicode61 remove_diacritics 0'
        )
        """
    )

"""Optional rebuildable FTS5 derived index for authorized claim search."""

from __future__ import annotations

from .query import get_authorized_claim, query_claims
from .ranking import extract_entities, rank_claims, score_claim_row, tokenize_query
from .store import NotFoundError, PersonalMemoryStore
from .text_norm import NORMALIZER_VERSION, query_terms, stem_text
from .types import Actor
from .validation import validate_list_limit, validate_rank_query_text

_FTS_TABLE = "memory_claim_fts_index"
_FTS_META_TABLE = "memory_fts_meta"


def _memory(store: PersonalMemoryStore | None) -> PersonalMemoryStore:
    return store if store is not None else PersonalMemoryStore()


def _decode_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return ""


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _fts_table_exists(conn) -> bool:
    return _table_exists(conn, _FTS_TABLE)


def _fts_v2_ready(conn) -> bool:
    if not _fts_table_exists(conn):
        return False
    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({_FTS_TABLE})")
    }
    if "body_stem" not in columns:
        return False
    if not _table_exists(conn, _FTS_META_TABLE):
        return False
    row = conn.execute(
        f"SELECT normalizer_version FROM {_FTS_META_TABLE} WHERE id = 1"
    ).fetchone()
    return row is not None and int(row[0]) == NORMALIZER_VERSION


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


def _create_fts_v2(conn) -> None:
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE {_FTS_TABLE} USING fts5(
            claim_id UNINDEXED,
            tenant_id UNINDEXED,
            body,
            body_stem,
            tokenize='unicode61 remove_diacritics 0'
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE {_FTS_META_TABLE}(
            id INTEGER PRIMARY KEY CHECK (id = 1),
            normalizer_version INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        f"INSERT INTO {_FTS_META_TABLE}(id, normalizer_version) VALUES (1, ?)",
        (NORMALIZER_VERSION,),
    )


def sync_claim_fts(
    conn,
    *,
    tenant_id: str,
    claim_id: str,
    text: str | None,
) -> None:
    """Keep one claim's derived FTS row in step with its newest write."""
    if not _fts_v2_ready(conn):
        return
    conn.execute(
        f"DELETE FROM {_FTS_TABLE} WHERE tenant_id = ? AND claim_id = ?",
        (tenant_id, claim_id),
    )
    if text is None:
        return
    conn.execute(
        f"INSERT INTO {_FTS_TABLE}(claim_id, tenant_id, body, body_stem) "
        "VALUES (?, ?, ?, ?)",
        (claim_id, tenant_id, text, stem_text(text)),
    )


def _fts_quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _fts_match_expr(query: str) -> str | None:
    terms = query_terms(query)
    if not terms:
        return None
    stems = " AND ".join(f"body_stem:{_fts_quote(term.stem)}" for term in terms)
    raws = " AND ".join(f"body:{_fts_quote(term.raw)}" for term in terms)
    return f"({stems}) OR ({raws})"


def build_claim_fts_index(
    actor: Actor,
    *,
    store: PersonalMemoryStore | None = None,
) -> dict[str, object]:
    memory = _memory(store)
    rows = query_claims(
        actor,
        include_content=True,
        limit=500,
        store=memory,
    )
    indexed = 0
    with memory._tx() as conn:
        tenant_id = memory._ensure_principal_on(conn, actor)
        if _fts_table_exists(conn) and not _fts_v2_ready(conn):
            conn.execute(f"DROP TABLE {_FTS_TABLE}")
        if not _fts_table_exists(conn):
            conn.execute(f"DROP TABLE IF EXISTS {_FTS_META_TABLE}")
            _create_fts_v2(conn)
        conn.execute(f"DELETE FROM {_FTS_TABLE} WHERE tenant_id = ?", (tenant_id,))
        for row in rows:
            body = _decode_text(row.get("content"))
            explanation = _decode_text(row.get("explanation"))
            text = f"{body}\n{explanation}".strip()
            if not text:
                continue
            conn.execute(
                f"INSERT INTO {_FTS_TABLE}(claim_id, tenant_id, body, body_stem) "
                "VALUES (?, ?, ?, ?)",
                (str(row["claim_id"]), tenant_id, text, stem_text(text)),
            )
            indexed += 1
    return {"indexed_claims": indexed, "fts_table": _FTS_TABLE}


def fts_index_present(store: PersonalMemoryStore | None = None) -> bool:
    memory = _memory(store)
    conn = memory._connect()
    try:
        return _fts_v2_ready(conn)
    finally:
        conn.close()


def rank_claims_fts(
    actor: Actor,
    query_text: object,
    *,
    states: object = None,
    claim_kinds: object = None,
    valid_from: object = None,
    valid_to: object = None,
    limit: object = 50,
    store: PersonalMemoryStore | None = None,
) -> list[dict[str, object]]:
    memory = _memory(store)
    if not fts_index_present(memory):
        return rank_claims(
            actor,
            query_text,
            states=states,
            claim_kinds=claim_kinds,
            valid_from=valid_from,
            valid_to=valid_to,
            limit=limit,
            store=memory,
        )
    query = validate_rank_query_text(query_text)
    lim = validate_list_limit(limit)
    query_tokens = tokenize_query(query)
    entities = extract_entities(query)
    match_expr = _fts_match_expr(query)
    if match_expr is None:
        return []
    authorized = query_claims(
        actor,
        states=states,
        claim_kinds=claim_kinds,
        valid_from=valid_from,
        valid_to=valid_to,
        include_content=False,
        limit=500,
        store=memory,
    )
    allowed_ids = {str(row["claim_id"]) for row in authorized}
    if not allowed_ids:
        return []
    scored: list[dict[str, object]] = []
    with memory._tx() as conn:
        tenant_id = memory._ensure_principal_on(conn, actor)
        if not _fts_v2_ready(conn):
            return rank_claims(
                actor,
                query,
                states=states,
                claim_kinds=claim_kinds,
                valid_from=valid_from,
                valid_to=valid_to,
                limit=lim,
                store=memory,
            )
        rows = conn.execute(
            f"""
            SELECT claim_id, bm25({_FTS_TABLE}) AS fts_score
            FROM {_FTS_TABLE}
            WHERE tenant_id = ? AND {_FTS_TABLE} MATCH ?
            ORDER BY fts_score
            LIMIT ?
            """,
            (tenant_id, match_expr, max(lim * 4, lim)),
        ).fetchall()
    for row in rows:
        claim_id = str(row["claim_id"])
        if claim_id not in allowed_ids:
            continue
        try:
            item = get_authorized_claim(
                actor,
                claim_id,
                include_content=True,
                store=memory,
            )
        except NotFoundError:
            continue
        rank_score, match_kinds = score_claim_row(
            item,
            query_tokens=query_tokens,
            entities=entities,
            query_text=query,
        )
        if rank_score <= 0.0:
            continue
        item = dict(item)
        item["rank_score"] = round(rank_score, 6)
        item["match_kinds"] = match_kinds + ["fts"]
        item["fts_score"] = float(row["fts_score"])
        scored.append(item)
        if len(scored) >= lim:
            break
    if not scored:
        return rank_claims(
            actor,
            query,
            states=states,
            claim_kinds=claim_kinds,
            valid_from=valid_from,
            valid_to=valid_to,
            limit=lim,
            store=memory,
        )
    return scored[:lim]

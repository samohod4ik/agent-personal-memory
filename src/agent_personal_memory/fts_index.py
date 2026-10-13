"""Optional rebuildable FTS5 derived index for authorized claim search."""

from __future__ import annotations

import re

from .query import get_authorized_claim, query_claims
from .ranking import extract_entities, rank_claims, score_claim_row, tokenize_query
from .store import NotFoundError, PersonalMemoryStore
from .types import Actor
from .validation import validate_list_limit, validate_rank_query_text

_FTS_TABLE = "memory_claim_fts_index"
_FTS_MATCH_SAFE = re.compile(r"[^\w\u0400-\u04FF]+", re.UNICODE)


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


def _fts_query_terms(query: str) -> str:
    tokens = tokenize_query(query)
    if not tokens:
        raise ValueError("empty query")
    escaped = []
    for token in tokens:
        cleaned = _FTS_MATCH_SAFE.sub("", token)
        if cleaned:
            escaped.append(f'"{cleaned}"')
    if not escaped:
        raise ValueError("empty query")
    return " AND ".join(escaped)


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
        _ensure_fts_table(conn)
        conn.execute(f"DELETE FROM {_FTS_TABLE} WHERE tenant_id = ?", (tenant_id,))
        for row in rows:
            body = _decode_text(row.get("content"))
            explanation = _decode_text(row.get("explanation"))
            text = f"{body}\n{explanation}".strip()
            if not text:
                continue
            conn.execute(
                f"INSERT INTO {_FTS_TABLE}(claim_id, tenant_id, body) VALUES (?, ?, ?)",
                (str(row["claim_id"]), tenant_id, text),
            )
            indexed += 1
    return {"indexed_claims": indexed, "fts_table": _FTS_TABLE}


def fts_index_present(store: PersonalMemoryStore | None = None) -> bool:
    memory = _memory(store)
    conn = memory._connect()
    try:
        return _fts_table_exists(conn)
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
    try:
        match_expr = _fts_query_terms(query)
    except ValueError:
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
        if not _fts_table_exists(conn):
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

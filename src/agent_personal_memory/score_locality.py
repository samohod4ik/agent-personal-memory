"""Tenant-local scoring barriers over candidate personal-memory hits.

Current `fts_index.rank_claims_fts` uses one shared FTS5 table and
`bm25(memory_claim_fts_index)`. SQLite document frequency is table-global,
so that score is not tenant-local. This module does not add a second index.
It filters and rescores an already-retrieved candidate list.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Mapping, Sequence

from .ranking import tokenize_query
from .store import PersonalMemoryStore
from .types import Actor

_K1 = 1.2
_B = 0.75


def _decode_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return ""


def _hit_text(hit: Mapping[str, object]) -> str:
    for key in ("content", "body", "authorized_text"):
        text = _decode_text(hit.get(key)).strip()
        if text:
            return text
    return ""


def _resolve_tenant_id(
    query_context: Mapping[str, object],
    tenant_id: str | None,
) -> str:
    resolved = tenant_id if tenant_id is not None else query_context.get("tenant_id")
    if not isinstance(resolved, str) or not resolved:
        raise ValueError("tenant_id is required")
    return resolved


def _resolve_query_text(query_context: Mapping[str, object]) -> str:
    raw = query_context.get("query_text")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("query_text is required")
    return raw


def _check_actor(actor: Actor, query_context: Mapping[str, object]) -> None:
    if not isinstance(actor, Actor):
        raise TypeError("actor must be an Actor")
    other = query_context.get("actor")
    if other is None:
        return
    if not isinstance(other, Actor):
        raise TypeError("query_context actor must be an Actor")
    if (other.platform, other.user_id) != (actor.platform, actor.user_id):
        raise ValueError("query actor mismatch")


def _idf(df: int, n_docs: int) -> float:
    return math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))


def _average_length(tokenized: Sequence[list[str]]) -> float:
    if not tokenized:
        return 1.0
    total = sum(len(tokens) for tokens in tokenized)
    return max(1.0, total / len(tokenized))


def _bm25_one(
    query_tokens: Sequence[str],
    doc_tokens: Sequence[str],
    df_map: Mapping[str, int],
    n_docs: int,
    avgdl: float,
) -> float:
    if not query_tokens or n_docs <= 0:
        return 0.0
    tf_map = Counter(doc_tokens)
    dl = max(1, len(doc_tokens))
    score = 0.0
    for term in query_tokens:
        tf = tf_map.get(term, 0)
        if tf <= 0:
            continue
        df = df_map.get(term, 0)
        denom = tf + _K1 * (1.0 - _B + _B * dl / avgdl)
        score += _idf(df, n_docs) * (tf * (_K1 + 1.0)) / denom
    return score


def _corpus_stats(documents: Sequence[str]) -> tuple[dict[str, int], int, float]:
    tokenized = [tokenize_query(document) for document in documents]
    df_map: dict[str, int] = {}
    for tokens in tokenized:
        for term in set(tokens):
            df_map[term] = df_map.get(term, 0) + 1
    return df_map, len(tokenized), _average_length(tokenized)


def bm25_scores(query_text: str, documents: Sequence[str]) -> list[float]:
    """BM25 over `documents` only. The caller chooses the corpus boundary."""
    query_tokens = tokenize_query(query_text)
    df_map, n_docs, avgdl = _corpus_stats(documents)
    return [
        round(
            _bm25_one(query_tokens, tokenize_query(document), df_map, n_docs, avgdl),
            6,
        )
        for document in documents
    ]


def _tenant_local_score(
    query_text: str,
    document: str,
    tenant_corpus: Sequence[str],
) -> float:
    query_tokens = tokenize_query(query_text)
    df_map, n_docs, avgdl = _corpus_stats(tenant_corpus)
    return round(
        _bm25_one(query_tokens, tokenize_query(document), df_map, n_docs, avgdl),
        6,
    )


def apply_snippet_acl(snippet: object, authorized_text: object) -> str:
    """Keep only text that already sits on the authorized pointer."""
    authorized = _decode_text(authorized_text).strip()
    if not authorized:
        return ""
    raw = _decode_text(snippet).strip()
    if raw and raw in authorized:
        return raw
    if raw and authorized in raw:
        return authorized
    return authorized


def score_locality(
    hits: Sequence[Mapping[str, object]],
    actor: Actor,
    query_context: Mapping[str, object],
    *,
    tenant_id: str | None = None,
    store: PersonalMemoryStore | None = None,
) -> list[dict[str, object]]:
    """Apply Candidate-ACL, tenant-local BM25, and Snippet-ACL.

    Incoming `score` values are ignored for ranking. Shared-corpus DF must
    not leak through that field.

    When `store` is set, tenant identity is `store.ensure_principal(actor)`.
    Caller `tenant_id` / `query_context["tenant_id"]` cannot override it.
    Without a store, `tenant_id` is a fixture id for tests and evals only.
    """
    _check_actor(actor, query_context)
    if store is not None:
        actor_tenant = store.ensure_principal(actor)
    else:
        actor_tenant = _resolve_tenant_id(query_context, tenant_id)
    query_text = _resolve_query_text(query_context)

    authorized: list[Mapping[str, object]] = []
    for hit in hits:
        hit_tenant = hit.get("tenant_id")
        if hit_tenant == actor_tenant:
            authorized.append(hit)

    if not authorized:
        return []

    corpus_raw = query_context.get("tenant_corpus")
    if corpus_raw is None:
        tenant_corpus = [_hit_text(hit) for hit in authorized]
    else:
        if not isinstance(corpus_raw, Sequence) or isinstance(corpus_raw, (str, bytes)):
            raise TypeError("tenant_corpus must be a sequence of texts")
        tenant_corpus = [_decode_text(item) for item in corpus_raw]
    if not tenant_corpus:
        tenant_corpus = [""]

    ranked: list[dict[str, object]] = []
    for hit in authorized:
        authorized_text = _hit_text(hit)
        item = dict(hit)
        item["rank_score"] = _tenant_local_score(
            query_text,
            authorized_text,
            tenant_corpus,
        )
        item["snippet"] = apply_snippet_acl(hit.get("snippet"), authorized_text)
        kinds = [str(kind) for kind in list(hit.get("match_kinds") or [])]
        if "score_locality" not in kinds:
            kinds.append("score_locality")
        item["match_kinds"] = kinds
        ranked.append(item)

    ranked.sort(key=lambda row: str(row.get("claim_id") or ""))
    ranked.sort(key=lambda row: float(row["rank_score"]), reverse=True)
    return ranked

"""Deterministic native lexical/entity/time ranking over authorized claims."""

from __future__ import annotations

import re

from .query import query_claims
from .store import PersonalMemoryStore
from .text_norm import STOP_WORDS, query_terms, tokenize
from .types import Actor
from .validation import validate_list_limit, validate_rank_query_text

_TOKEN_RE = re.compile(r"[\w\u0400-\u04FF]+", re.UNICODE)
_QUOTED_RE = re.compile(r"""['"]([^'"]+)['"]""")
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?\b")
_CAPITALIZED_RE = re.compile(r"\b[A-Z\u0400-\u04FF][\w\u0400-\u04FF]{2,}\b", re.UNICODE)

_LEXICAL_WEIGHT = 0.7
_ENTITY_WEIGHT = 0.3


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


def tokenize_query(text: str) -> list[str]:
    tokens = [token.lower() for token in _TOKEN_RE.findall(text) if len(token) >= 2]
    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


def extract_entities(text: str) -> list[str]:
    entities: list[str] = []
    seen: set[str] = set()
    for pattern in (_QUOTED_RE, _UUID_RE, _DATE_RE, _CAPITALIZED_RE):
        for match in pattern.finditer(text):
            entity = match.group(1) if pattern is _QUOTED_RE else match.group(0)
            key = entity.casefold()
            if key in seen:
                continue
            seen.add(key)
            entities.append(entity)
    return entities


def _lexical_overlap(query_tokens: list[str], haystack: str) -> tuple[float, bool]:
    if not query_tokens:
        return 0.0, False
    lowered = haystack.casefold()
    hits = sum(1 for token in query_tokens if token in lowered)
    return hits / len(query_tokens), hits > 0


def _entity_overlap(entities: list[str], haystack: str) -> tuple[float, bool]:
    if not entities:
        return 0.0, False
    lowered = haystack.casefold()
    hits = sum(1 for entity in entities if entity.casefold() in lowered)
    return hits / len(entities), hits > 0


def _normalize_text(text: str) -> str:
    return text.casefold().replace("ё", "е")


def _haystack_index(haystack: str) -> tuple[str, frozenset[str]]:
    lowered = _normalize_text(haystack)
    stems = frozenset(token.stem for token in tokenize(haystack))
    return lowered, stems


def _lexical_overlap_stemmed(query_text: str, haystack: str) -> tuple[float, bool]:
    terms = query_terms(query_text)
    if not terms:
        return 0.0, False
    lowered, stems = _haystack_index(haystack)
    hits = sum(1 for term in terms if term.raw in lowered or term.stem in stems)
    return hits / len(terms), hits > 0


def _entity_overlap_stemmed(
    entities: list[str], haystack: str, query_text: str
) -> tuple[float, bool]:
    # Stop-word entities only survive an all-stop query (kept for parity
    # with query_terms so a stop-only query behaves exactly as before).
    if any(not token.stop for token in tokenize(query_text)):
        entities = [
            entity for entity in entities if _normalize_text(entity) not in STOP_WORDS
        ]
    if not entities:
        return 0.0, False
    lowered, stems = _haystack_index(haystack)
    hits = 0
    for entity in entities:
        if _normalize_text(entity) in lowered:
            hits += 1
            continue
        parts = tokenize(entity)
        if len(parts) == 1 and parts[0].stem in stems:
            hits += 1
    return hits / len(entities), hits > 0


def _time_sort_key(created_at: object) -> str:
    return str(created_at or "")


def score_claim_row(
    row: dict[str, object],
    *,
    query_tokens: list[str],
    entities: list[str],
    query_text: str | None = None,
) -> tuple[float, list[str]]:
    content = _decode_text(row.get("content"))
    explanation = _decode_text(row.get("explanation"))
    haystack = f"{content}\n{explanation}".strip()
    if query_text is None:
        lexical_score, lexical_hit = _lexical_overlap(query_tokens, haystack)
        entity_score, entity_hit = _entity_overlap(entities, haystack)
    else:
        lexical_score, lexical_hit = _lexical_overlap_stemmed(query_text, haystack)
        entity_score, entity_hit = _entity_overlap_stemmed(
            entities, haystack, query_text
        )
    rank_score = min(1.0, (_LEXICAL_WEIGHT * lexical_score) + (_ENTITY_WEIGHT * entity_score))
    match_kinds: list[str] = []
    if lexical_hit:
        match_kinds.append("lexical")
    if entity_hit:
        match_kinds.append("entity")
    if rank_score > 0.0:
        match_kinds.append("time")
    return rank_score, match_kinds


def rank_claims(
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
    query = validate_rank_query_text(query_text)
    lim = validate_list_limit(limit)
    query_tokens = tokenize_query(query)
    entities = extract_entities(query)
    rows = query_claims(
        actor,
        states=states,
        claim_kinds=claim_kinds,
        valid_from=valid_from,
        valid_to=valid_to,
        include_content=True,
        limit=500,
        store=memory,
    )
    scored: list[dict[str, object]] = []
    for row in rows:
        rank_score, match_kinds = score_claim_row(
            row,
            query_tokens=query_tokens,
            entities=entities,
            query_text=query,
        )
        if rank_score <= 0.0:
            continue
        item = dict(row)
        item["rank_score"] = round(rank_score, 6)
        item["match_kinds"] = match_kinds
        scored.append(item)
    scored.sort(key=lambda item: str(item.get("claim_id")))
    scored.sort(key=lambda item: _time_sort_key(item.get("created_at")), reverse=True)
    scored.sort(key=lambda item: float(item["rank_score"]), reverse=True)
    return scored[:lim]

"""Bilingual read and explain-why UX over authorized personal-memory APIs."""

from __future__ import annotations

from .export import explain_claim
from .locale import (
    format_claim_summary,
    format_explain_narrative,
    format_export_heading,
    format_export_summary,
    format_list_heading,
    normalize_language,
)
from .query import query_claims
from .store import PersonalMemoryStore
from .types import Actor
from .validation import validate_list_limit


def _memory(store: PersonalMemoryStore | None) -> PersonalMemoryStore:
    return store if store is not None else PersonalMemoryStore()


def list_personal_memories(
    actor: Actor,
    *,
    language: object = "en",
    include_content: bool = False,
    limit: object = 50,
    store: PersonalMemoryStore | None = None,
) -> dict[str, object]:
    lang = normalize_language(language)
    lim = validate_list_limit(limit)
    rows = query_claims(
        actor,
        include_content=bool(include_content),
        limit=lim,
        store=_memory(store),
    )
    items: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        item["summary"] = format_claim_summary(
            row,
            language=lang,
            include_content=bool(include_content),
        )
        items.append(item)
    return {
        "language": lang,
        "heading": format_list_heading(lang),
        "count": len(items),
        "items": items,
    }


def explain_why_remembered(
    actor: Actor,
    claim_id: object,
    *,
    language: object = "en",
    store: PersonalMemoryStore | None = None,
) -> dict[str, object]:
    lang = normalize_language(language)
    if not isinstance(claim_id, str) or not claim_id:
        from .validation import ValidationError

        raise ValidationError("claim_id: must be a non-empty string")
    payload = explain_claim(actor, claim_id, store=_memory(store))
    result = dict(payload)
    result["language"] = lang
    result["narrative"] = format_explain_narrative(payload, language=lang)
    return result


def export_personal_memory(
    actor: Actor,
    *,
    language: object = "en",
    store: PersonalMemoryStore | None = None,
) -> dict[str, object]:
    lang = normalize_language(language)
    memory = _memory(store)
    payload = memory.export_owner_data(actor)
    sources = payload.get("sources") or []
    claims = payload.get("claims") or []
    receipts = payload.get("deletion_receipts") or []
    counts = {
        "sources": len(sources),
        "claims": len(claims),
        "deletion_receipts": len(receipts),
    }
    return {
        "language": lang,
        "heading": format_export_heading(lang),
        "summary": format_export_summary(
            lang,
            sources=counts["sources"],
            claims=counts["claims"],
            deletion_receipts=counts["deletion_receipts"],
        ),
        "counts": counts,
        "export": payload,
    }

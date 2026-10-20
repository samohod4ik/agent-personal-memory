"""Persist durable personal insights from a user turn or remember tool."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from .insight_extract import (
    ExtractedInsight,
    extract_insights,
    insight_content_allowed,
    normalize_insight_text,
    turn_has_blocked_secret,
)
from .lifecycle import MemoryState
from .locale import format_mutation_confirmation, normalize_language
from .query import query_claims
from .store import PersonalMemoryStore, utcnow_iso
from .types import Actor
from .validation import (
    AUTO_INSIGHT_SOURCE_KIND,
    DEFAULT_MEMORY_MODE,
    MEMORY_MODE_AUTO_INSIGHT,
    MEMORY_MODE_CONFIRM_REQUIRED,
    ValidationError,
    content_hash_for,
    validate_claim_content,
    validate_claim_kind,
    validate_memory_mode,
)


def _memory(store: PersonalMemoryStore | None) -> PersonalMemoryStore:
    return store if store is not None else PersonalMemoryStore()


def _decode_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return ""


def insight_expires_at() -> str:
    stamp = datetime.now(timezone.utc) + timedelta(days=3650)
    return stamp.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _existing_norms(actor: Actor, store: PersonalMemoryStore) -> set[str]:
    rows = query_claims(
        actor,
        states=[MemoryState.CANDIDATE.value, MemoryState.CONFIRMED.value],
        include_content=True,
        limit=500,
        store=store,
    )
    norms: set[str] = set()
    for row in rows:
        text = normalize_insight_text(_decode_content(row.get("content")))
        if text:
            norms.add(text)
    return norms


def _register_turn_source(
    actor: Actor,
    *,
    source_kind: str,
    text: str,
    store: PersonalMemoryStore,
    label: str,
) -> str:
    payload = text.encode("utf-8")
    digest = hashlib.sha256(normalize_insight_text(text).encode("utf-8")).hexdigest()[:16]
    registered = store.register_source(
        actor,
        source_kind=source_kind,
        external_ref=f"{label}:{digest}",
        content_hash=content_hash_for(payload),
        snapshot=payload,
        observed_at=utcnow_iso(),
        retention_class="durable",
        metadata={"title": label},
    )
    return str(registered["source_id"])


def _persist_insight(
    actor: Actor,
    insight: ExtractedInsight,
    *,
    store: PersonalMemoryStore,
    mode: str,
    existing: set[str],
    judge: object = None,
) -> dict[str, object] | None:
    norm = normalize_insight_text(insight.content)
    if not norm or norm in existing:
        return None
    if not insight_content_allowed(insight.content):
        return None
    payload = insight.content.encode("utf-8")
    validate_claim_content(payload)
    source_kind = (
        AUTO_INSIGHT_SOURCE_KIND
        if mode == MEMORY_MODE_AUTO_INSIGHT
        else "user_message"
    )
    source_id = _register_turn_source(
        actor,
        source_kind=source_kind,
        text=insight.content,
        store=store,
        label="auto-insight" if source_kind == AUTO_INSIGHT_SOURCE_KIND else "remember",
    )
    created = store.create_candidate(
        actor,
        claim_kind=insight.claim_kind,
        content=payload,
        confidence=insight.confidence,
        explanation=insight.explanation,
        sensitivity="normal",
        source_ids=[source_id],
        expires_at=insight_expires_at(),
    )
    from .reconcile import DEFAULT_JUDGE, propose_reconciliations

    reconciliations = propose_reconciliations(
        actor,
        str(created["claim_id"]),
        judge=judge if judge is not None else DEFAULT_JUDGE,
        store=store,
    )
    if mode == MEMORY_MODE_AUTO_INSIGHT and not reconciliations:
        created = store.confirm_claim(
            actor,
            str(created["claim_id"]),
            reliable_source_id=source_id,
        )
    existing.add(norm)
    result = dict(created)
    result["reconciliations"] = reconciliations
    return result


def learn_from_turn(
    actor: Actor,
    turn_text: str,
    *,
    judge: object = None,
    store: PersonalMemoryStore,
) -> dict[str, Any]:
    memory = store
    mode = memory.get_memory_mode(actor)
    if mode != MEMORY_MODE_AUTO_INSIGHT:
        return {
            "mode": mode,
            "persisted": [],
            "skipped": MEMORY_MODE_CONFIRM_REQUIRED,
        }
    text = (turn_text or "").strip()
    if not text:
        return {"mode": mode, "persisted": [], "skipped": "empty"}
    insights = extract_insights(text)
    existing = _existing_norms(actor, memory)
    persisted: list[dict[str, object]] = []
    for insight in insights:
        created = _persist_insight(
            actor,
            insight,
            store=memory,
            mode=mode,
            existing=existing,
            judge=judge,
        )
        if created is not None:
            persisted.append(created)
    return {"mode": mode, "persisted": persisted, "skipped": ""}


def remember_personal_insight(
    actor: Actor,
    content: object,
    *,
    claim_kind: object = "confirmed_fact",
    judge: object = None,
    language: object = "en",
    store: PersonalMemoryStore,
) -> dict[str, object]:
    if not isinstance(content, str) or not content.strip():
        raise ValidationError("content: must be a non-empty string")
    text = " ".join(content.strip().split())
    if turn_has_blocked_secret(text) or not insight_content_allowed(text):
        raise ValidationError("content: rejected by memory filters")
    kind = validate_claim_kind(claim_kind) if claim_kind is not None else "confirmed_fact"
    lang = normalize_language(language)
    memory = store
    mode = memory.get_memory_mode(actor)
    existing = _existing_norms(actor, memory)
    created = _persist_insight(
        actor,
        ExtractedInsight(
            claim_kind=kind,
            content=text,
            confidence=0.9,
            explanation="explicit remember",
        ),
        store=memory,
        mode=mode,
        existing=existing,
        judge=judge,
    )
    if created is None:
        raise ValidationError("content: already remembered")
    created = dict(created)
    created["language"] = lang
    created["mode"] = mode
    created["confirmation"] = format_mutation_confirmation(
        "remember_confirmed" if created["state"] == MemoryState.CONFIRMED.value else "remember_candidate",
        language=lang,
    )
    return created


def get_memory_mode(
    actor: Actor,
    *,
    language: object = "en",
    store: PersonalMemoryStore | None = None,
) -> dict[str, object]:
    lang = normalize_language(language)
    memory = _memory(store)
    mode = memory.get_memory_mode(actor)
    return {
        "language": lang,
        "mode": mode,
        "default": DEFAULT_MEMORY_MODE,
        "summary": format_mutation_confirmation("memory_mode", language=lang, mode=mode),
    }


def set_memory_mode(
    actor: Actor,
    mode: object,
    *,
    language: object = "en",
    store: PersonalMemoryStore | None = None,
) -> dict[str, object]:
    lang = normalize_language(language)
    cleaned = validate_memory_mode(mode)
    memory = _memory(store)
    stored = memory.set_memory_mode(actor, cleaned)
    return {
        "language": lang,
        "mode": stored,
        "default": DEFAULT_MEMORY_MODE,
        "summary": format_mutation_confirmation("memory_mode", language=lang, mode=stored),
    }

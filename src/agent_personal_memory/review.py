"""Bilingual candidate-review flows over authorized personal-memory APIs.

Delete previews route through the standalone confirm-gate in
``confirmation.py`` (explicit ``SessionBinding`` + host-provided ``send``)."""

from __future__ import annotations

from .lifecycle import MemoryState
from .locale import (
    format_candidate_list_heading,
    format_claim_summary,
    format_mutation_confirmation,
    normalize_language,
)
from .mutations import _register_actor_source
from .confirmation import SessionBinding, create_delete_preview, reject_legacy_confirm
from .query import get_authorized_claim, query_claims
from .store import PersonalMemoryStore
from .types import Actor
from .validation import ValidationError, validate_list_limit


def _memory(store: PersonalMemoryStore | None) -> PersonalMemoryStore:
    return store if store is not None else PersonalMemoryStore()


def _require_claim_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("claim_id: must be a non-empty string")
    return value


def _require_candidate(
    actor: Actor,
    claim_id: str,
    *,
    store: PersonalMemoryStore,
) -> dict[str, object]:
    row = get_authorized_claim(actor, claim_id, store=store)
    if str(row.get("state")) != MemoryState.CANDIDATE.value:
        raise ValidationError("claim: not a candidate")
    return row


def list_candidate_memories(
    actor: Actor,
    *,
    language: object = "en",
    include_content: bool = False,
    limit: object = 50,
    store: PersonalMemoryStore | None = None,
) -> dict[str, object]:
    lang = normalize_language(language)
    lim = validate_list_limit(limit)
    memory = _memory(store)
    rows = query_claims(
        actor,
        states=[MemoryState.CANDIDATE.value],
        include_content=bool(include_content),
        limit=lim,
        store=memory,
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
        "heading": format_candidate_list_heading(lang),
        "count": len(items),
        "items": items,
    }


def confirm_candidate_memory(
    actor: Actor,
    claim_id: object,
    *,
    language: object = "en",
    store: PersonalMemoryStore,
) -> dict[str, object]:
    lang = normalize_language(language)
    memory = store
    claim = _require_claim_id(claim_id)
    _require_candidate(actor, claim, store=memory)
    confirmation = _register_actor_source(
        actor,
        source_kind="user_confirmation",
        label="candidate_confirm",
        payload=claim.encode("utf-8"),
        store=memory,
    )
    confirmed = memory.confirm_claim(
        actor,
        claim,
        confirmation_source_id=str(confirmation["source_id"]),
    )
    return {
        "language": lang,
        "claim_id": confirmed["claim_id"],
        "claim_version_id": confirmed["claim_version_id"],
        "version": confirmed["version"],
        "state": confirmed["state"],
        "confirmation": format_mutation_confirmation("confirm_candidate", language=lang),
    }


def reject_candidate_memory(
    actor: Actor,
    claim_id: object,
    *,
    confirm: object = False,
    language: object = "en",
    store: PersonalMemoryStore,
    session_binding: SessionBinding | None = None,
) -> dict[str, object]:
    reject_legacy_confirm(confirm)
    lang = normalize_language(language)
    memory = store
    claim = _require_claim_id(claim_id)
    _require_candidate(actor, claim, store=memory)
    return create_delete_preview(
        actor,
        action="reject_candidate_memory",
        claim_ids=[claim],
        language=lang,
        store=memory,
        session_binding=session_binding,
        require_state=MemoryState.CANDIDATE.value,
        empty_kind="do_not_remember_none",
        preview_kind="reject_candidate_preview",
    )

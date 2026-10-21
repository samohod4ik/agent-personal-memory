"""Bilingual owner mutation flows over authorized personal-memory APIs."""

from __future__ import annotations

import hashlib
import uuid

from .lifecycle import ACTIVE_READ_STATES
from .locale import format_claim_summary, format_mutation_confirmation, normalize_language
from .query import get_authorized_claim, query_claims
from .store import PersonalMemoryStore, utcnow_iso
from .confirmation import (
    SessionBinding,
    create_delete_preview,
    reject_legacy_confirm,
)
from .types import Actor
from .validation import ValidationError, content_hash_for, validate_claim_content, validate_explanation

_DO_NOT_REMEMBER_MAX = 10
_TEXT_MAX = 512


def _require_claim_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("claim_id: must be a non-empty string")
    return value


def _validate_optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("text: must be a non-empty string")
    text = value.strip()
    if len(text) > _TEXT_MAX:
        raise ValidationError("text: is too long")
    return text


def _decode_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return ""


def _register_actor_source(
    actor: Actor,
    *,
    source_kind: str,
    label: str,
    payload: bytes,
    store: PersonalMemoryStore,
) -> dict[str, object]:
    stamp = utcnow_iso()
    digest = hashlib.sha256(f"{label}:{stamp}:{uuid.uuid4()}".encode("utf-8")).hexdigest()[:16]
    external_ref = f"telegram:{label}:{digest}"
    return store.register_source(
        actor,
        source_kind=source_kind,
        external_ref=external_ref,
        content_hash=content_hash_for(payload),
        snapshot=payload,
        observed_at=stamp,
        retention_class="standard",
        metadata={"title": label},
    )


def _parse_confirm(value: object) -> bool:
    """Legacy parser. Destructive tools must call reject_legacy_confirm()."""
    reject_legacy_confirm(value)
    return False


def _preview_claim(
    actor: Actor,
    claim_id: str,
    *,
    language: str,
    store: PersonalMemoryStore,
) -> dict[str, object]:
    row = get_authorized_claim(actor, claim_id, store=store)
    summary = format_claim_summary(row, language=language)
    return {
        "language": language,
        "preview": True,
        "claim_id": claim_id,
        "summary": summary,
        "state": row.get("state"),
        "claim_kind": row.get("claim_kind"),
    }


def _resolve_do_not_remember_targets(
    actor: Actor,
    *,
    target_id: str | None,
    phrase: str | None,
    store: PersonalMemoryStore,
) -> list[str]:
    if target_id is not None:
        get_authorized_claim(actor, target_id, store=store)
        return [target_id]
    assert phrase is not None
    rows = query_claims(actor, include_content=True, limit=500, store=store)
    erased_ids: list[str] = []
    for row in rows:
        if len(erased_ids) >= _DO_NOT_REMEMBER_MAX:
            break
        if not _match_claim_content(row, phrase):
            continue
        erased_ids.append(str(row["claim_id"]))
    return erased_ids


def correct_personal_memory(
    actor: Actor,
    claim_id: object,
    content: object,
    *,
    explanation: object = "",
    language: object = "en",
    store: PersonalMemoryStore,
) -> dict[str, object]:
    lang = normalize_language(language)
    memory = store
    claim = _require_claim_id(claim_id)
    payload = validate_claim_content(content if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8"))
    reason = validate_explanation(explanation)
    confirmation = _register_actor_source(
        actor,
        source_kind="user_confirmation",
        label="correction",
        payload=payload,
        store=memory,
    )
    corrected = memory.correct_claim(
        actor,
        claim,
        content=payload,
        explanation=reason,
        source_ids=[str(confirmation["source_id"])],
    )
    return {
        "language": lang,
        "claim_id": corrected["claim_id"],
        "claim_version_id": corrected["claim_version_id"],
        "version": corrected["version"],
        "state": corrected["state"],
        "confirmation": format_mutation_confirmation(
            "correct",
            language=lang,
            version=corrected["version"],
        ),
    }


def retract_personal_memory(
    actor: Actor,
    claim_id: object,
    *,
    explanation: object = None,
    language: object = "en",
    store: PersonalMemoryStore,
) -> dict[str, object]:
    """Append a terminal ``retracted`` version to one claim.

    The ``user_confirmation`` source is registered only after the claim
    has passed the authorized lookup and active-state check, so denied
    or invalid transitions never mint an orphan source row.
    """
    lang = normalize_language(language)
    memory = store
    claim = _require_claim_id(claim_id)
    reason = validate_explanation(explanation)
    row = get_authorized_claim(actor, claim, store=memory)
    if str(row["state"]) not in {item.value for item in ACTIVE_READ_STATES}:
        with store._tx() as conn:
            tenant_id = store._ensure_principal_on(conn, actor)
            store._reject_claim(
                conn,
                tenant_id=tenant_id,
                action="claim.retract",
                policy_reason="invalid_transition",
            )
        raise ValidationError("claim: invalid transition")
    confirmation = _register_actor_source(
        actor,
        source_kind="user_confirmation",
        label="retraction",
        payload=(reason or f"retract:{claim}").encode("utf-8"),
        store=memory,
    )
    retracted = memory.retract_claim(
        actor,
        claim,
        source_ids=[str(confirmation["source_id"])],
    )
    return {
        "language": lang,
        "claim_id": retracted["claim_id"],
        "claim_version_id": retracted["claim_version_id"],
        "version": retracted["version"],
        "state": retracted["state"],
        "confirmation": format_mutation_confirmation(
            "retract",
            language=lang,
            version=retracted["version"],
        ),
    }


def forget_personal_memory(
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
    get_authorized_claim(actor, claim, store=memory)
    return create_delete_preview(
        actor,
        action="forget_personal_memory",
        claim_ids=[claim],
        language=lang,
        store=memory,
        session_binding=session_binding,
        empty_kind="do_not_remember_none",
        preview_kind="forget_preview",
    )


def _match_claim_content(row: dict[str, object], needle: str) -> bool:
    haystack = _decode_content(row.get("content"))
    return needle.casefold() in haystack.casefold()


def do_not_remember(
    actor: Actor,
    *,
    claim_id: object = None,
    text: object = None,
    confirm: object = False,
    language: object = "en",
    store: PersonalMemoryStore,
    session_binding: SessionBinding | None = None,
) -> dict[str, object]:
    reject_legacy_confirm(confirm)
    lang = normalize_language(language)
    memory = store
    target_id = None if claim_id in (None, "") else _require_claim_id(claim_id)
    phrase = _validate_optional_text(text)
    if target_id is None and phrase is None:
        raise ValidationError("request: claim_id or text is required")
    matched_ids = _resolve_do_not_remember_targets(
        actor,
        target_id=target_id,
        phrase=phrase,
        store=memory,
    )
    return create_delete_preview(
        actor,
        action="do_not_remember",
        claim_ids=matched_ids,
        language=lang,
        store=memory,
        session_binding=session_binding,
        empty_kind="do_not_remember_none",
        preview_kind="do_not_remember_preview",
    )

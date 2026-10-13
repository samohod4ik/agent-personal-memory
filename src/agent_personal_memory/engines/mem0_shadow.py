"""Mem0 shadow adapter with tenant-bound validation and native fallback."""

from __future__ import annotations

from typing import Any, Protocol

from ..query import get_authorized_claim
from ..ranking import rank_claims
from ..store import NotFoundError, PersonalMemoryStore
from ..types import Actor
from ..validation import (
    ValidationError,
    validate_extraction_request,
    validate_retrieval_request,
    validate_shadow_candidate_fields,
)
from .native import NativeMemoryEngine
from .types import (
    Candidate,
    EngineResult,
    ExtractionRequest,
    RetrievalRequest,
    ScoredRecordId,
)

_DEFAULT_EXPIRES = "2099-01-01T00:00:00Z"
_SHADOW_CONFIDENCE_CAP = 0.6


class Mem0ShadowClient(Protocol):
    def extract(
        self,
        *,
        user_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        ...

    def search(
        self,
        *,
        user_id: str,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        ...

    def delete_user(self, *, user_id: str) -> None:
        ...


def _actor_user_id(actor: Actor) -> str:
    return f"{actor.platform}:{actor.user_id}"


def _decode_item_text(item: dict[str, Any]) -> str:
    for key in ("text", "memory", "content"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _candidate_from_text(
    text: str,
    *,
    source_ids: tuple[str, ...],
    expires_at: str,
) -> Candidate:
    fields = validate_shadow_candidate_fields(
        claim_kind="candidate_insight",
        content=text.encode("utf-8"),
        confidence=_SHADOW_CONFIDENCE_CAP,
        explanation="mem0 shadow extraction",
        sensitivity="normal",
        source_ids=list(source_ids),
        expires_at=expires_at,
    )
    return Candidate(
        claim_kind=str(fields["claim_kind"]),
        content=fields["content"],
        confidence=float(fields["confidence"]),
        explanation=str(fields["explanation"]),
        source_ids=tuple(str(item) for item in fields["source_ids"]),
        expires_at=str(fields["expires_at"]),
        sensitivity=str(fields["sensitivity"]),
    )


class Mem0Adapter:
    """Shadow-only Mem0 adapter; never writes canonical storage directly."""

    def __init__(
        self,
        *,
        store: PersonalMemoryStore | None = None,
        mem0_client: Mem0ShadowClient | None = None,
        native: NativeMemoryEngine | None = None,
        default_expires_at: str = _DEFAULT_EXPIRES,
    ) -> None:
        self._store = store
        self._client = mem0_client
        self._native = native or NativeMemoryEngine(store=store)
        self._default_expires_at = default_expires_at

    def extract_candidates(
        self,
        request: ExtractionRequest,
    ) -> tuple[EngineResult, list[Candidate]]:
        validated = validate_extraction_request(request)
        client = self._client
        if client is None:
            return (
                EngineResult(
                    engine="mem0",
                    degraded=True,
                    degraded_reason="mem0_unavailable",
                ),
                [],
            )
        try:
            raw_items = client.extract(
                user_id=_actor_user_id(validated.actor),
                text=validated.text,
                metadata={"source_ids": list(validated.source_ids)},
            )
        except Exception:
            return (
                EngineResult(
                    engine="mem0",
                    degraded=True,
                    degraded_reason="mem0_extract_failed",
                ),
                [],
            )
        candidates: list[Candidate] = []
        for item in raw_items:
            text = _decode_item_text(item)
            if not text:
                continue
            try:
                candidates.append(
                    _candidate_from_text(
                        text,
                        source_ids=validated.source_ids,
                        expires_at=self._default_expires_at,
                    )
                )
            except ValidationError:
                continue
        return EngineResult(engine="mem0", degraded=False), candidates

    def retrieve(
        self,
        request: RetrievalRequest,
    ) -> tuple[EngineResult, list[ScoredRecordId]]:
        validated = validate_retrieval_request(request)
        client = self._client
        if client is None:
            result, records = self._native.retrieve(validated)
            return (
                EngineResult(
                    engine="mem0",
                    degraded=True,
                    degraded_reason="mem0_unavailable",
                ),
                records,
            )
        try:
            shadow_hits = client.search(
                user_id=_actor_user_id(validated.actor),
                query=validated.query_text,
                limit=max(validated.limit, 1),
            )
        except Exception:
            result, records = self._native.retrieve(validated)
            return (
                EngineResult(
                    engine="mem0",
                    degraded=True,
                    degraded_reason="mem0_search_failed",
                ),
                records,
            )
        expanded_query = validated.query_text
        for item in shadow_hits:
            text = _decode_item_text(item)
            if text:
                expanded_query = f"{expanded_query} {text}"
        ranked = rank_claims(
            validated.actor,
            expanded_query.strip(),
            limit=validated.limit,
            store=self._store,
        )
        records: list[ScoredRecordId] = []
        for row in ranked:
            claim_id = str(row["claim_id"])
            try:
                get_authorized_claim(
                    validated.actor,
                    claim_id,
                    include_content=False,
                    store=self._store,
                )
            except NotFoundError:
                continue
            records.append(
                ScoredRecordId(
                    record_kind="claim",
                    record_id=claim_id,
                    score=float(row["rank_score"]),
                    engine="mem0",
                )
            )
        if records:
            return EngineResult(engine="mem0", degraded=False), records
        result, fallback = self._native.retrieve(validated)
        return (
            EngineResult(
                engine="mem0",
                degraded=True,
                degraded_reason="mem0_no_authorized_match",
            ),
            fallback,
        )


def persist_shadow_candidates(
    actor: Actor,
    candidates: object,
    *,
    store: PersonalMemoryStore | None = None,
) -> list[dict[str, object]]:
    memory = store if store is not None else PersonalMemoryStore()
    if not isinstance(candidates, list):
        raise ValidationError("candidates: must be a list")
    created: list[dict[str, object]] = []
    for item in candidates:
        if not isinstance(item, Candidate):
            raise ValidationError("candidates: each item must be a Candidate")
        fields = validate_shadow_candidate_fields(
            claim_kind=item.claim_kind,
            content=item.content,
            confidence=item.confidence,
            explanation=item.explanation,
            sensitivity=item.sensitivity,
            source_ids=list(item.source_ids),
            expires_at=item.expires_at,
        )
        created.append(
            memory.create_candidate(
                actor,
                claim_kind=fields["claim_kind"],
                content=fields["content"],
                confidence=fields["confidence"],
                explanation=fields["explanation"],
                sensitivity=fields["sensitivity"],
                source_ids=fields["source_ids"],
                expires_at=fields["expires_at"],
            )
        )
    return created


def purge_shadow_projection(
    actor: Actor,
    *,
    client: Mem0ShadowClient | None = None,
) -> bool:
    if client is None:
        return False
    try:
        client.delete_user(user_id=_actor_user_id(actor))
    except Exception:
        return False
    return True

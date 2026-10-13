"""Deterministic native MemoryEngine baseline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..insight_extract import extract_insights
from ..ranking import rank_claims
from ..store import PersonalMemoryStore
from .types import (
    Candidate,
    EngineResult,
    ExtractionRequest,
    RetrievalRequest,
    ScoredRecordId,
)


def _insight_expires_at() -> str:
    stamp = datetime.now(timezone.utc) + timedelta(days=3650)
    return stamp.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class NativeMemoryEngine:
    """Mandatory fallback engine using native authorized ranking."""

    def __init__(self, *, store: PersonalMemoryStore | None = None) -> None:
        self._store = store

    def extract_candidates(
        self,
        request: ExtractionRequest,
    ) -> tuple[EngineResult, list[Candidate]]:
        insights = extract_insights(request.text)
        candidates = [
            Candidate(
                claim_kind=item.claim_kind,
                content=item.content.encode("utf-8"),
                confidence=item.confidence,
                explanation=item.explanation,
                source_ids=request.source_ids,
                expires_at=_insight_expires_at(),
                sensitivity="normal",
            )
            for item in insights
        ]
        return EngineResult(engine="native", degraded=False), candidates

    def retrieve(
        self,
        request: RetrievalRequest,
    ) -> tuple[EngineResult, list[ScoredRecordId]]:
        ranked = rank_claims(
            request.actor,
            request.query_text,
            limit=request.limit,
            store=self._store,
        )
        records = [
            ScoredRecordId(
                record_kind="claim",
                record_id=str(row["claim_id"]),
                score=float(row["rank_score"]),
                engine="native",
            )
            for row in ranked
        ]
        return EngineResult(engine="native", degraded=False), records

"""MemoryEngine request/response types."""

from __future__ import annotations

from dataclasses import dataclass

from ..types import Actor


@dataclass(frozen=True)
class ExtractionRequest:
    actor: Actor
    text: str
    source_ids: tuple[str, ...]
    observed_at: str | None = None


@dataclass(frozen=True)
class RetrievalRequest:
    actor: Actor
    query_text: str
    limit: int = 50


@dataclass(frozen=True)
class Candidate:
    claim_kind: str
    content: bytes
    confidence: float
    explanation: str
    source_ids: tuple[str, ...]
    expires_at: str
    sensitivity: str = "normal"


@dataclass(frozen=True)
class ScoredRecordId:
    record_kind: str
    record_id: str
    score: float
    engine: str


@dataclass(frozen=True)
class EngineResult:
    engine: str
    degraded: bool = False
    degraded_reason: str | None = None

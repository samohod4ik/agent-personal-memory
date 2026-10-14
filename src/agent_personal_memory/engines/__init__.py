"""MemoryEngine implementations for personal memory."""

from __future__ import annotations

from .mem0_shadow import Mem0Adapter, persist_shadow_candidates, purge_shadow_projection
from .native import NativeMemoryEngine
from .types import (
    Candidate,
    EngineResult,
    ExtractionRequest,
    RetrievalRequest,
    ScoredRecordId,
)

__all__ = (
    "Candidate",
    "EngineResult",
    "ExtractionRequest",
    "Mem0Adapter",
    "NativeMemoryEngine",
    "RetrievalRequest",
    "ScoredRecordId",
    "persist_shadow_candidates",
    "purge_shadow_projection",
)

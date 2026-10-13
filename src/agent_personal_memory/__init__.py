"""agent_personal_memory - local-first encrypted personal memory for AI agents.

Public API surface. Every call takes an explicit ``Actor`` (and store where
applicable); nothing reads ambient session environment. Destructive flows
route through the two-phase confirm-gate: ``create_delete_preview`` is
non-authorizing, the nonce exists only inside an operator-delivered
``CONFIRM_REGISTRY.open_preview`` payload, and ``consume_confirm`` +
``consume_delete_request`` execute the bound erasure.

``create_candidate`` is re-exported as the unbound
``PersonalMemoryStore.create_candidate`` method for convenience; call it as
``create_candidate(store, actor, **fields)``.
"""

from __future__ import annotations

from .assembler import assemble_context
from .confirmation import (
    REGISTRY as CONFIRM_REGISTRY,
    BoundTarget,
    ConfirmNonce,
    DeleteRegistry,
    DeleteRequest,
    SessionBinding,
    TrustedDeleteDenied,
    consume_delete_request,
    create_delete_preview,
    reject_legacy_confirm,
    reset_registry,
)
from .engines import (
    Mem0Adapter,
    NativeMemoryEngine,
    persist_shadow_candidates,
    purge_shadow_projection,
)
from .engines.types import (
    Candidate,
    EngineResult,
    ExtractionRequest,
    RetrievalRequest,
    ScoredRecordId,
)
from .erasure import ErasureReceipt
from .export import explain_claim, export_owner_data
from .fts_index import build_claim_fts_index, fts_index_present, rank_claims_fts
from .insight_extract import (
    ExtractedInsight,
    extract_insights,
    insight_content_allowed,
    normalize_insight_text,
    turn_has_blocked_secret,
)
from .learn import (
    get_memory_mode,
    learn_from_turn,
    remember_personal_insight,
    set_memory_mode,
)
from .lifecycle import MemoryState
from .mutations import (
    correct_personal_memory,
    do_not_remember,
    forget_personal_memory,
)
from .query import (
    get_authorized_claim,
    get_authorized_source,
    query_claims,
    query_sources,
)
from .ranking import rank_claims
from .review import (
    confirm_candidate_memory,
    list_candidate_memories,
    reject_candidate_memory,
)
from .schemas import (
    ASSEMBLE_CONTEXT_SCHEMA,
    CONFIRM_CANDIDATE_MEMORY_SCHEMA,
    CORRECT_PERSONAL_MEMORY_SCHEMA,
    DO_NOT_REMEMBER_SCHEMA,
    EXPLAIN_CLAIM_SCHEMA,
    EXPLAIN_WHY_REMEMBERED_SCHEMA,
    EXPORT_OWNER_DATA_SCHEMA,
    EXPORT_PERSONAL_MEMORY_SCHEMA,
    FORGET_PERSONAL_MEMORY_SCHEMA,
    GET_AUTHORIZED_CLAIM_SCHEMA,
    GET_AUTHORIZED_SOURCE_SCHEMA,
    GET_MEMORY_MODE_SCHEMA,
    LIST_CANDIDATE_MEMORIES_SCHEMA,
    LIST_PERSONAL_MEMORIES_SCHEMA,
    PERSONAL_MEMORY_CONFIRM_SCHEMA,
    QUERY_CLAIMS_SCHEMA,
    QUERY_SOURCES_SCHEMA,
    RANK_CLAIMS_SCHEMA,
    REJECT_CANDIDATE_MEMORY_SCHEMA,
    REMEMBER_PERSONAL_INSIGHT_SCHEMA,
    SET_MEMORY_MODE_SCHEMA,
    TOOL_SCHEMAS,
)
from .score_locality import apply_snippet_acl, bm25_scores, score_locality
from .store import (
    ConflictError,
    NotFoundError,
    PersonalMemoryStore,
    SchemaVersionError,
)
from .types import Actor
from .ux import (
    explain_why_remembered,
    export_personal_memory,
    list_personal_memories,
)
from .validation import ValidationError

__version__ = "0.1.0"

# Unbound store method re-exported under its source name (see docstring).
create_candidate = PersonalMemoryStore.create_candidate

__all__ = (
    "Actor",
    "ASSEMBLE_CONTEXT_SCHEMA",
    "BoundTarget",
    "CONFIRM_REGISTRY",
    "Candidate",
    "ConflictError",
    "CONFIRM_CANDIDATE_MEMORY_SCHEMA",
    "ConfirmNonce",
    "CORRECT_PERSONAL_MEMORY_SCHEMA",
    "DO_NOT_REMEMBER_SCHEMA",
    "DeleteRegistry",
    "DeleteRequest",
    "EngineResult",
    "ErasureReceipt",
    "EXPLAIN_CLAIM_SCHEMA",
    "EXPLAIN_WHY_REMEMBERED_SCHEMA",
    "EXPORT_OWNER_DATA_SCHEMA",
    "EXPORT_PERSONAL_MEMORY_SCHEMA",
    "ExtractedInsight",
    "ExtractionRequest",
    "FORGET_PERSONAL_MEMORY_SCHEMA",
    "GET_AUTHORIZED_CLAIM_SCHEMA",
    "GET_AUTHORIZED_SOURCE_SCHEMA",
    "GET_MEMORY_MODE_SCHEMA",
    "LIST_CANDIDATE_MEMORIES_SCHEMA",
    "LIST_PERSONAL_MEMORIES_SCHEMA",
    "Mem0Adapter",
    "MemoryState",
    "NativeMemoryEngine",
    "NotFoundError",
    "PERSONAL_MEMORY_CONFIRM_SCHEMA",
    "PersonalMemoryStore",
    "QUERY_CLAIMS_SCHEMA",
    "QUERY_SOURCES_SCHEMA",
    "RANK_CLAIMS_SCHEMA",
    "REJECT_CANDIDATE_MEMORY_SCHEMA",
    "REMEMBER_PERSONAL_INSIGHT_SCHEMA",
    "RetrievalRequest",
    "SET_MEMORY_MODE_SCHEMA",
    "SchemaVersionError",
    "ScoredRecordId",
    "SessionBinding",
    "TOOL_SCHEMAS",
    "TrustedDeleteDenied",
    "ValidationError",
    "apply_snippet_acl",
    "assemble_context",
    "bm25_scores",
    "build_claim_fts_index",
    "confirm_candidate_memory",
    "consume_delete_request",
    "correct_personal_memory",
    "create_candidate",
    "create_delete_preview",
    "do_not_remember",
    "explain_claim",
    "explain_why_remembered",
    "export_owner_data",
    "export_personal_memory",
    "extract_insights",
    "forget_personal_memory",
    "fts_index_present",
    "get_authorized_claim",
    "get_authorized_source",
    "get_memory_mode",
    "insight_content_allowed",
    "learn_from_turn",
    "list_candidate_memories",
    "list_personal_memories",
    "normalize_insight_text",
    "persist_shadow_candidates",
    "purge_shadow_projection",
    "query_claims",
    "query_sources",
    "rank_claims",
    "rank_claims_fts",
    "reject_candidate_memory",
    "reject_legacy_confirm",
    "remember_personal_insight",
    "reset_registry",
    "score_locality",
    "set_memory_mode",
    "turn_has_blocked_secret",
)

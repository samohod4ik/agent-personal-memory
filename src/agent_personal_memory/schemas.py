"""Tool JSON schemas for personal-memory ownership APIs.

Identity is bound by the host from the authenticated caller's Actor. Schemas
must not accept tenant, user, or platform selectors.
"""

from __future__ import annotations

EXPORT_OWNER_DATA_SCHEMA = {
    "name": "export_owner_data",
    "description": (
        "Export the current session owner's personal memory as JSON. "
        "Includes owned sources, claim lifecycle, safe provenance, and "
        "deletion receipts."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

EXPLAIN_CLAIM_SCHEMA = {
    "name": "explain_claim",
    "description": (
        "Explain why a claim is remembered for the current session owner. "
        "Returns source kinds, labels, confidence, timestamps, and state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "claim_id": {
                "type": "string",
                "description": "Claim id from the current session owner's memory",
            }
        },
        "required": ["claim_id"],
    },
}

LIST_PERSONAL_MEMORIES_SCHEMA = {
    "name": "list_personal_memories",
    "description": (
        "Read-only inventory of authorized personal memories for the current "
        "session owner, with a localized summary in Russian or English. "
        "Use for 'what do you remember?' listing. "
        "Do not use this tool to honor forget / stop-remembering / erase-topic "
        "requests — call do_not_remember (preview first), then tell the user "
        "to open /memory_delete."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "enum": ["en", "ru"],
                "default": "en",
                "description": "User-facing summary language. Default en.",
            },
            "include_content": {
                "type": "boolean",
                "description": "When true, decrypt content after authorization",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum rows to return, from 1 to 500",
            },
        },
        "required": [],
    },
}

EXPORT_PERSONAL_MEMORY_SCHEMA = {
    "name": "export_personal_memory",
    "description": (
        "Export the current session owner's personal memory with a localized "
        "heading and summary in Russian or English, plus the full authorized "
        "export payload."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "enum": ["en", "ru"],
                "description": "User-facing summary language",
            },
        },
        "required": [],
    },
}

EXPLAIN_WHY_REMEMBERED_SCHEMA = {
    "name": "explain_why_remembered",
    "description": (
        "Explain in Russian or English why a remembered item exists for the "
        "current session owner, including provenance and lifecycle labels."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "claim_id": {
                "type": "string",
                "description": "Claim id from the current session owner's memory",
            },
            "language": {
                "type": "string",
                "enum": ["en", "ru"],
                "description": "Narrative language",
            },
        },
        "required": ["claim_id"],
    },
}

CORRECT_PERSONAL_MEMORY_SCHEMA = {
    "name": "correct_personal_memory",
    "description": (
        "Replace the content of an authorized personal memory for the current "
        "session owner. Registers a user confirmation source and creates a "
        "new claim version."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "claim_id": {
                "type": "string",
                "description": "Claim id to correct",
            },
            "content": {
                "type": "string",
                "description": "Corrected memory text",
            },
            "explanation": {
                "type": "string",
                "description": "Optional reason for the correction",
            },
            "language": {
                "type": "string",
                "enum": ["en", "ru"],
                "description": "Confirmation language",
            },
        },
        "required": ["claim_id", "content"],
    },
}

FORGET_PERSONAL_MEMORY_SCHEMA = {
    "name": "forget_personal_memory",
    "description": (
        "Prepare a non-authorizing preview of one authorized personal memory "
        "by a known claim_id for the current session owner. Requires claim_id. "
        "Never erases. Returns /memory_delete <request-id> so the authenticated "
        "user can open the exact preview and confirm inbound. "
        "A model confirm/confirmed boolean cannot authorize deletion. "
        "Do not use this for topic-level forget / never-remember-X requests — "
        "call do_not_remember with a text match and preview first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "claim_id": {
                "type": "string",
                "description": "Claim id to forget",
            },
            "language": {
                "type": "string",
                "enum": ["en", "ru"],
                "description": "Confirmation language",
            },
        },
        "required": ["claim_id"],
    },
}

DO_NOT_REMEMBER_SCHEMA = {
    "name": "do_not_remember",
    "description": (
        "Honor forget / do-not-remember / stop-storing requests "
        "(забудь, не запоминай, не храни) by claim_id and/or matching "
        "authorized content text. Prefer this over list_personal_memories "
        "when the user wants a topic forgotten. At least one of claim_id or "
        "text is required. Returns a non-authorizing preview of the exact "
        "matched set (max ten) and /memory_delete <request-id>. The user "
        "must open that inbound preview and confirm; a model confirm boolean "
        "cannot authorize deletion."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "claim_id": {
                "type": "string",
                "description": "Optional claim id to erase",
            },
            "text": {
                "type": "string",
                "description": "Optional substring to match against authorized claim content",
            },
            "language": {
                "type": "string",
                "enum": ["en", "ru"],
                "default": "en",
                "description": "Confirmation language. Default en.",
            },
        },
        "required": [],
    },
}

LIST_CANDIDATE_MEMORIES_SCHEMA = {
    "name": "list_candidate_memories",
    "description": (
        "List candidate personal memories awaiting review for the current "
        "session owner with localized summaries in Russian or English."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "enum": ["en", "ru"],
                "default": "en",
                "description": "User-facing summary language. Default en.",
            },
            "include_content": {
                "type": "boolean",
                "description": "When true, decrypt content after authorization",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum rows to return, from 1 to 500",
            },
        },
        "required": [],
    },
}

CONFIRM_CANDIDATE_MEMORY_SCHEMA = {
    "name": "confirm_candidate_memory",
    "description": (
        "Confirm a candidate personal memory for the current session owner. "
        "Registers a user confirmation source and transitions the claim to "
        "confirmed state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "claim_id": {
                "type": "string",
                "description": "Candidate claim id to confirm",
            },
            "language": {
                "type": "string",
                "enum": ["en", "ru"],
                "description": "Confirmation language",
            },
        },
        "required": ["claim_id"],
    },
}

REJECT_CANDIDATE_MEMORY_SCHEMA = {
    "name": "reject_candidate_memory",
    "description": (
        "Prepare a non-authorizing preview that rejects a candidate personal "
        "memory for the current session owner. Never erases. Returns "
        "/memory_delete <request-id> so the authenticated user can "
        "open the exact preview and confirm inbound. A model confirm boolean "
        "cannot authorize deletion."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "claim_id": {
                "type": "string",
                "description": "Candidate claim id to reject",
            },
            "language": {
                "type": "string",
                "enum": ["en", "ru"],
                "description": "Confirmation language",
            },
        },
        "required": ["claim_id"],
    },
}

QUERY_CLAIMS_SCHEMA = {
    "name": "query_claims",
    "description": (
        "List authorized claim metadata for the current session owner. "
        "Filters run on plaintext state, kind, and valid-time columns before "
        "any decrypt. Default results omit content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "states": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["candidate", "confirmed", "superseded", "deleted"],
                },
                "description": "Newest-version states to include",
            },
            "claim_kinds": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "raw_event",
                        "candidate_insight",
                        "confirmed_fact",
                        "preference",
                        "task",
                        "decision",
                        "summary",
                    ],
                },
                "description": "Claim kinds to include",
            },
            "valid_from": {
                "type": "string",
                "description": "RFC3339 Z start of the overlapping valid-time window",
            },
            "valid_to": {
                "type": "string",
                "description": "RFC3339 Z end of the overlapping valid-time window",
            },
            "include_content": {
                "type": "boolean",
                "description": "When true, decrypt content only after authorization",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum rows to return, from 1 to 500",
            },
        },
        "required": [],
    },
}

QUERY_SOURCES_SCHEMA = {
    "name": "query_sources",
    "description": (
        "List authorized source metadata for the current session owner. "
        "Filters run on plaintext kind and observed-time columns before "
        "any decrypt. Default results omit payload."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_kinds": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "user_message",
                        "user_confirmation",
                        "assistant_message",
                        "tool_result",
                        "document",
                        "email_message",
                        "calendar_event",
                        "travel_record",
                        "import_batch",
                        "auto_insight_turn",
                    ],
                },
                "description": "Source kinds to include",
            },
            "observed_from": {
                "type": "string",
                "description": "RFC3339 Z inclusive start of the observed-time range",
            },
            "observed_to": {
                "type": "string",
                "description": "RFC3339 Z inclusive end of the observed-time range",
            },
            "include_payload": {
                "type": "boolean",
                "description": "When true, decrypt payload only after authorization",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum rows to return, from 1 to 500",
            },
        },
        "required": [],
    },
}

GET_AUTHORIZED_CLAIM_SCHEMA = {
    "name": "get_authorized_claim",
    "description": (
        "Return one authorized claim for the current session owner. "
        "Missing, foreign, and erased ids are indistinguishable not-found."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "claim_id": {
                "type": "string",
                "description": "Claim id from the current session owner's memory",
            },
            "include_content": {
                "type": "boolean",
                "description": "When true, decrypt content only after authorization",
            },
        },
        "required": ["claim_id"],
    },
}

GET_AUTHORIZED_SOURCE_SCHEMA = {
    "name": "get_authorized_source",
    "description": (
        "Return one authorized source for the current session owner. "
        "Missing, foreign, and erased ids are indistinguishable not-found."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "Source id from the current session owner's memory",
            },
            "include_payload": {
                "type": "boolean",
                "description": "When true, decrypt payload only after authorization",
            },
        },
        "required": ["source_id"],
    },
}

ASSEMBLE_CONTEXT_SCHEMA = {
    "name": "assemble_context",
    "description": (
        "Build a bounded personal/session context bundle for the current "
        "session owner. Authorization runs before any decrypt. Session "
        "snippets are caller-supplied only and are never read from the "
        "memory store."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_items": {
                "type": "array",
                "description": "Optional bounded session snippets (role, text, observed_at)",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "text": {"type": "string"},
                        "observed_at": {
                            "type": "string",
                            "description": "RFC3339 Z timestamp",
                        },
                    },
                    "required": ["role", "text"],
                },
            },
            "query_text": {
                "type": "string",
                "description": "Optional query to rank the personal section before budgeting",
            },
            "record_budget": {
                "type": "integer",
                "description": "Maximum assembled records across sections (1-500)",
            },
            "token_budget": {
                "type": "integer",
                "description": "Deterministic token estimate cap across sections (1-32000)",
            },
        },
        "required": [],
    },
}

GET_MEMORY_MODE_SCHEMA = {
    "name": "get_memory_mode",
    "description": (
        "Return the current session owner's personal-memory mode. "
        "Default is auto_insight (quietly learn durable facts from chat). "
        "confirm_required stores chat writes as candidates only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "enum": ["en", "ru"],
                "description": "Summary language",
            },
        },
        "required": [],
    },
}

SET_MEMORY_MODE_SCHEMA = {
    "name": "set_memory_mode",
    "description": (
        "Set the current session owner's personal-memory mode. "
        "auto_insight confirms durable chat insights automatically. "
        "confirm_required stores chat writes as candidates only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["auto_insight", "confirm_required"],
                "description": "Per-actor memory mode",
            },
            "language": {
                "type": "string",
                "enum": ["en", "ru"],
                "description": "Confirmation language",
            },
        },
        "required": ["mode"],
    },
}

REMEMBER_PERSONAL_INSIGHT_SCHEMA = {
    "name": "remember_personal_insight",
    "description": (
        "Remember a durable personal fact, preference, style, or decision "
        "for the current session owner. Use for «запомни…», «remember that…», "
        "biography, and stable preferences. auto_insight saves confirmed; "
        "confirm_required saves "
        "a candidate. Rejects secrets, payment data, ID documents, and "
        "another person's private data."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Durable insight text to remember",
            },
            "claim_kind": {
                "type": "string",
                "enum": [
                    "raw_event",
                    "candidate_insight",
                    "confirmed_fact",
                    "preference",
                    "task",
                    "decision",
                    "summary",
                ],
                "description": "Optional claim kind. Default confirmed_fact.",
            },
            "language": {
                "type": "string",
                "enum": ["en", "ru"],
                "description": "Confirmation language",
            },
        },
        "required": ["content"],
    },
}

RANK_CLAIMS_SCHEMA = {
    "name": "rank_claims",
    "description": (
        "Rank authorized claims for the current session owner using deterministic "
        "lexical, entity, and recency scoring. Filters run before decrypt."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query_text": {
                "type": "string",
                "description": "Non-empty keyword/entity query",
            },
            "states": QUERY_CLAIMS_SCHEMA["parameters"]["properties"]["states"],
            "claim_kinds": QUERY_CLAIMS_SCHEMA["parameters"]["properties"]["claim_kinds"],
            "valid_from": QUERY_CLAIMS_SCHEMA["parameters"]["properties"]["valid_from"],
            "valid_to": QUERY_CLAIMS_SCHEMA["parameters"]["properties"]["valid_to"],
            "limit": QUERY_CLAIMS_SCHEMA["parameters"]["properties"]["limit"],
        },
        "required": ["query_text"],
    },
}

PERSONAL_MEMORY_CONFIRM_SCHEMA = {
    "name": "personal_memory_confirm",
    "description": (
        "Consume a one-time confirmation nonce for a pending personal-memory "
        "deletion preview. The nonce is generated inside the confirm-gate, "
        "exists solely inside the operator-delivered preview text, and is "
        "single-use. A model cannot mint or guess a valid nonce."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "nonce": {
                "type": "string",
                "description": "One-time confirmation nonce from the delivered preview",
            }
        },
        "required": ["nonce"],
    },
}

# Canonical standalone tool surface: name -> schema. Single source for the
# API facade and for host integrations (e.g. an MCP tools/list handler). No
# schema accepts actor, tenant, user, or platform selectors; the host binds
# identity. ``personal_memory_confirm`` is the named consume surface for the
# two-phase confirm-gate; it takes only the nonce.
TOOL_SCHEMAS = {
    "export_owner_data": EXPORT_OWNER_DATA_SCHEMA,
    "export_personal_memory": EXPORT_PERSONAL_MEMORY_SCHEMA,
    "explain_claim": EXPLAIN_CLAIM_SCHEMA,
    "list_personal_memories": LIST_PERSONAL_MEMORIES_SCHEMA,
    "explain_why_remembered": EXPLAIN_WHY_REMEMBERED_SCHEMA,
    "correct_personal_memory": CORRECT_PERSONAL_MEMORY_SCHEMA,
    "forget_personal_memory": FORGET_PERSONAL_MEMORY_SCHEMA,
    "do_not_remember": DO_NOT_REMEMBER_SCHEMA,
    "list_candidate_memories": LIST_CANDIDATE_MEMORIES_SCHEMA,
    "confirm_candidate_memory": CONFIRM_CANDIDATE_MEMORY_SCHEMA,
    "reject_candidate_memory": REJECT_CANDIDATE_MEMORY_SCHEMA,
    "get_memory_mode": GET_MEMORY_MODE_SCHEMA,
    "set_memory_mode": SET_MEMORY_MODE_SCHEMA,
    "remember_personal_insight": REMEMBER_PERSONAL_INSIGHT_SCHEMA,
    "query_claims": QUERY_CLAIMS_SCHEMA,
    "query_sources": QUERY_SOURCES_SCHEMA,
    "get_authorized_claim": GET_AUTHORIZED_CLAIM_SCHEMA,
    "get_authorized_source": GET_AUTHORIZED_SOURCE_SCHEMA,
    "assemble_context": ASSEMBLE_CONTEXT_SCHEMA,
    "rank_claims": RANK_CLAIMS_SCHEMA,
    "personal_memory_confirm": PERSONAL_MEMORY_CONFIRM_SCHEMA,
}

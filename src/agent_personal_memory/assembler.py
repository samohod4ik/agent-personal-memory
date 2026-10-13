"""Actor-bound bounded context assembly over authorized personal-memory APIs.

Only the personal and session sections are assembled; the source's
travel-adapter-backed trip section and its import chain stay outside this
package.
"""

from __future__ import annotations

from .query import query_claims
from .fts_index import rank_claims_fts
from .store import PersonalMemoryStore, utcnow_iso
from .types import Actor
from .validation import (
    validate_assembly_record_budget,
    validate_assembly_session_items,
    validate_assembly_token_budget,
    validate_optional_rank_query_text,
    validate_query_states,
)

_FILL_ORDER = ("personal", "session")


def _memory(store: PersonalMemoryStore | None) -> PersonalMemoryStore:
    return store if store is not None else PersonalMemoryStore()


def _estimate_tokens(value: object) -> int:
    if isinstance(value, (bytes, bytearray)):
        try:
            text = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return max(1, len(value) // 4)
        return max(1, len(text) // 4)
    if isinstance(value, str):
        return max(1, len(value) // 4)
    return 1


def _item_token_estimate(item: dict[str, object]) -> int:
    total = 0
    for key in ("content", "explanation", "text", "snapshot", "title", "destination", "dates", "status"):
        if key in item:
            total += _estimate_tokens(item[key])
    return max(1, total)


def _claim_freshness_label(state: object) -> str:
    if str(state) == "confirmed":
        return "confirmed"
    return "candidate"


def _personal_item(raw: dict[str, object]) -> dict[str, object]:
    item = {
        "claim_id": raw["claim_id"],
        "claim_kind": raw["claim_kind"],
        "state": raw["state"],
        "sensitivity": raw.get("sensitivity"),
        "confidence": raw["confidence"],
        "valid_from": raw["valid_from"],
        "valid_to": raw["valid_to"],
        "content": raw.get("content"),
        "freshness_label": _claim_freshness_label(raw.get("state")),
        "provenance": {
            "claim_id": raw["claim_id"],
            "created_at": raw["created_at"],
        },
    }
    if "rank_score" in raw:
        item["rank_score"] = raw["rank_score"]
    if "match_kinds" in raw:
        item["match_kinds"] = raw["match_kinds"]
    return item


def _session_item(raw: dict[str, object]) -> dict[str, object]:
    item: dict[str, object] = {
        "role": raw["role"],
        "text": raw["text"],
        "freshness_label": "session",
        "provenance": {"role": raw["role"]},
    }
    if raw.get("observed_at") is not None:
        item["observed_at"] = raw["observed_at"]
        item["provenance"] = {"role": raw["role"], "observed_at": raw["observed_at"]}
    return item


def _collect_personal_items(
    actor: Actor,
    *,
    memory: PersonalMemoryStore,
    fetch_limit: int,
    query_text: str | None = None,
    states: object = None,
) -> list[dict[str, object]]:
    if query_text is not None:
        rows = rank_claims_fts(
            actor,
            query_text,
            states=states,
            limit=fetch_limit,
            store=memory,
        )
    else:
        rows = query_claims(
            actor,
            states=states,
            include_content=True,
            limit=fetch_limit,
            store=memory,
        )
    items = [_personal_item(row) for row in rows]
    if query_text is None:
        items.sort(
            key=lambda item: (str(item["provenance"]["created_at"]), str(item["claim_id"])),
            reverse=True,
        )
    return items


def _apply_budgets(
    sections: dict[str, list[dict[str, object]]],
    *,
    record_budget: int,
    token_budget: int,
) -> tuple[dict[str, list[dict[str, object]]], dict[str, object]]:
    selected: dict[str, list[dict[str, object]]] = {
        "personal": [],
        "session": [],
    }
    records_used = 0
    tokens_used = 0
    truncated = False
    pending: list[tuple[str, dict[str, object]]] = []
    for section in _FILL_ORDER:
        pending.extend((section, item) for item in sections[section])
    for section, item in pending:
        item_tokens = _item_token_estimate(item)
        if records_used >= record_budget:
            truncated = True
            break
        if tokens_used + item_tokens > token_budget:
            truncated = True
            break
        selected[section].append(item)
        records_used += 1
        tokens_used += item_tokens
    if records_used < len(pending):
        truncated = True
    budgets = {
        "record_limit": record_budget,
        "token_limit": token_budget,
        "records_used": records_used,
        "tokens_estimated": tokens_used,
        "truncated": truncated,
    }
    return selected, budgets


def assemble_context(
    actor: Actor,
    *,
    memory_store: PersonalMemoryStore | None = None,
    session_items: object = None,
    query_text: object = None,
    states: object = None,
    record_budget: object = 50,
    token_budget: object = 4000,
) -> dict[str, object]:
    memory = _memory(memory_store)
    rec_limit = validate_assembly_record_budget(record_budget)
    tok_limit = validate_assembly_token_budget(token_budget)
    session = validate_assembly_session_items(session_items)
    rank_query = validate_optional_rank_query_text(query_text)
    state_filter = validate_query_states(states)
    fetch_limit = rec_limit
    personal_items = _collect_personal_items(
        actor,
        memory=memory,
        fetch_limit=fetch_limit,
        query_text=rank_query,
        states=state_filter,
    )
    session_section = [_session_item(item) for item in session]
    selected, budgets = _apply_budgets(
        {
            "personal": personal_items,
            "session": session_section,
        },
        record_budget=rec_limit,
        token_budget=tok_limit,
    )
    return {
        "assembled_at": utcnow_iso(),
        "budgets": budgets,
        "personal": {"items": selected["personal"]},
        "session": {"items": selected["session"]},
    }

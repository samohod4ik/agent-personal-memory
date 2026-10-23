"""Bounded current-fact core, recall, grounded links and explicit Markdown export."""
from __future__ import annotations

import math
import uuid

from .journal import _evidence_source_on, evidence_for_claim, query_journal
from .store import NotFoundError, PersonalMemoryStore, utcnow_iso
from .text_norm import query_terms, tokenize
from .types import Actor
from .validation import ValidationError


def current_claims_on(store, conn, tenant):
    """All authorized current facts, not an arbitrary recent-500 window."""
    now = utcnow_iso()
    rows = conn.execute("""SELECT c.id AS claim_id,c.claim_kind,v.* FROM memory_claims c
        JOIN memory_claim_versions v ON v.claim_id=c.id AND v.tenant_id=c.tenant_id
        WHERE c.tenant_id=? AND c.deleted_at IS NULL AND v.state='confirmed'
        AND v.sensitivity<>'restricted' AND (v.expires_at IS NULL OR v.expires_at>?)
        AND (v.valid_from IS NULL OR v.valid_from<=?) AND (v.valid_to IS NULL OR v.valid_to>?)
        AND v.version=(SELECT MAX(x.version) FROM memory_claim_versions x WHERE x.claim_id=c.id)
        ORDER BY v.created_at DESC,c.id""", (tenant, now, now, now)).fetchall()
    out = []
    for row in rows:
        content, _ = store._decrypt_claim_fields(conn, tenant_id=tenant, claim_id=row["claim_id"], version_row=row)
        evidence = [dict(r) for r in conn.execute("SELECT source_id,source_version,span_start,span_end FROM memory_evidence WHERE tenant_id=? AND claim_id=? AND claim_version=?", (tenant, row["claim_id"], row["version"]))]
        out.append({"claim_id": row["claim_id"], "version": row["version"], "claim_kind": row["claim_kind"],
                    "state": "confirmed", "content": content.decode("utf-8"), "created_at": row["created_at"],
                    "evidence": evidence})
    return out


def _claims(actor, store):
    with store._tx() as conn:
        return current_claims_on(store, conn, store._ensure_principal_on(conn, actor))


def build_core(actor: Actor, *, store: PersonalMemoryStore, char_budget: int = 1200) -> dict:
    if type(char_budget) is not int or not 0 <= char_budget <= 12000:
        raise ValidationError("char_budget: 0..12000 required")
    items, lines = [], []
    for item in _claims(actor, store):
        line = "- " + item["content"].replace("\n", " ")
        if len("\n".join(lines + [line])) <= char_budget:
            items.append(item)
            lines.append(line)
    text = "\n".join(lines)
    return {"text": text, "items": items, "chars_used": len(text)}


def _score(query, text):
    terms = query_terms(query)
    if not terms:
        return 0.0
    stems = {t.stem for t in tokenize(text)}
    lower = text.casefold().replace("ё", "е")
    return sum(t.raw in lower or t.stem in stems for t in terms) / len(terms)


def link_memories(actor: Actor, *, from_claim_id: str, to_claim_id: str, relation: str,
                  source_id: str, source_version: int, start: int, end: int, store: PersonalMemoryStore) -> dict:
    """Host proposal for a grounded typed link; names alone never merge entities."""
    if relation not in {"related_to", "depends_on", "same_entity"} or from_claim_id == to_claim_id:
        raise ValidationError("relation: invalid")
    with store._tx() as conn:
        tenant = store._ensure_principal_on(conn, actor)
        current = {c["claim_id"]: c for c in current_claims_on(store, conn, tenant)}
        if from_claim_id not in current or to_claim_id not in current:
            raise NotFoundError("Claim not found")
        event, quote = _evidence_source_on(store, conn, tenant, source_id, source_version, start, end)
        if event["role"] != "user" or event["origin"] != "direct":
            raise ValidationError("relation: direct owner evidence required")
        if any(current[c]["content"] not in quote for c in (from_claim_id, to_claim_id)):
            raise ValidationError("relation: both exact facts must occur in evidence")
        identity = str(uuid.uuid4())
        conn.execute("INSERT INTO memory_relations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (identity, tenant,
            from_claim_id, current[from_claim_id]["version"], to_claim_id, current[to_claim_id]["version"], relation,
            source_id, source_version, start, end))
        return {"relation_id": identity, "relation": relation}


def _neighbors(actor, store, seeds, current, max_nodes):
    selected = []
    with store._tx() as conn:
        tenant = store._ensure_principal_on(conn, actor)
        for row in conn.execute("SELECT * FROM memory_relations WHERE tenant_id=? ORDER BY id", (tenant,)):
            a, b = row["from_claim_id"], row["to_claim_id"]
            if a not in current or b not in current or current[a]["version"] != row["from_version"] or current[b]["version"] != row["to_version"]:
                continue
            if a not in seeds and b not in seeds:
                continue
            try:
                _evidence_source_on(store, conn, tenant, row["source_id"], row["source_version"], row["span_start"], row["span_end"])
            except NotFoundError:
                continue
            for identity in (a, b):
                if identity not in seeds and identity not in selected:
                    selected.append(identity)
                    if len(selected) >= max_nodes:
                        return selected
    return selected


def recall(actor: Actor, *, query: str, store: PersonalMemoryStore, token_budget: int = 4000,
           limit: int = 20, include_journal: bool = False) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValidationError("query: nonempty string up to 2000 characters required")
    if type(token_budget) is not int or not 1 <= token_budget <= 32000 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValidationError("recall budget: out of range")
    core = build_core(actor, store=store, char_budget=min(1200, token_budget * 4))
    used = math.ceil(len(core["text"]) / 4)
    current = {c["claim_id"]: c for c in _claims(actor, store)}
    ranked = sorted((( _score(query, c["content"]), c) for c in current.values()), key=lambda pair: (-pair[0], pair[1]["claim_id"]))
    candidates = [{**c, "layer": "claim", "score": score} for score, c in ranked if score > 0]
    seeds = {c["claim_id"] for c in candidates[:3]}
    for cid in _neighbors(actor, store, seeds, current, 5):
        if cid not in {c["claim_id"] for c in candidates}:
            candidates.append({**current[cid], "layer": "related_claim", "score": 0.0})
    from .consolidation import valid_projections
    for projection in valid_projections(actor, store=store):
        score = _score(query, projection["text"])
        if score:
            candidates.append({"layer": "summary", "content": projection["text"], "score": score,
                               "period": projection["period"], "start": projection["start"], "evidence": projection["evidence"]})
    if include_journal:
        # Explicit bounded original opening; excludes assistant/forwarded text
        # from owner-fact retrieval. Today is searchable before consolidation.
        page = query_journal(actor, store=store, limit=500, query=query, include_text=True)
        for event in page["items"]:
            if event["role"] == "user" and event["origin"] == "direct":
                pos = event["text"].casefold().find(query.casefold())
                start = max(0, pos - 200)
                excerpt = event["text"][start:start + 800]
                candidates.append({"layer": "journal", "content": excerpt, "source_id": event["source_id"],
                                   "source_version": event["version"], "start": start, "end": start + len(excerpt), "score": 1.0})
    items = []
    for item in candidates:
        tokens = max(1, math.ceil(len(item["content"]) / 4))
        if len(items) >= limit:
            break
        if used + tokens <= token_budget:
            items.append(item)
            used += tokens
    return {"core": core, "items": items, "budgets": {"token_limit": token_budget, "tokens_estimated": used, "record_limit": limit, "records_used": len(items), "truncated": len(items) < len(candidates)}}


def export_markdown(actor: Actor, *, store: PersonalMemoryStore) -> str:
    """Explicit owner projection; returns text without writing or publishing it."""
    from .consolidation import valid_projections
    lines = ["# Personal memory", "", "## Current facts", ""]
    for claim in _claims(actor, store):
        lines += [f"### {claim['claim_id']}", "", "> " + claim["content"].replace("\n", "\n> "), ""]
        for evidence in evidence_for_claim(actor, claim["claim_id"], store=store):
            lines.append(f"Source: [[{evidence['source_id']}]] v{evidence['source_version']}, characters {evidence['start']}:{evidence['end']}, {evidence['occurred_at']}")
        lines.append("")
    lines += ["## Period summaries", ""]
    for projection in valid_projections(actor, store=store):
        lines += [f"### {projection['period']} {projection['start']} ({projection['timezone']})", "", "> " + projection["text"].replace("\n", "\n> "), ""]
    return "\n".join(lines)

"""Opt-in host capture and exact evidence over versioned encrypted sources.

Identity and origin are supplied by the authenticated host, never by a model
schema. Source text is data; this module never interprets instructions in it.
"""
from __future__ import annotations

import hashlib
from difflib import SequenceMatcher
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as utc
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .crypto import derive_lookup_key, keyed_lookup_hash
from .insight_extract import insight_content_allowed, turn_has_blocked_secret
from .layered_store import encode, generation_on, invalidate_generation_on, purge_projections_on
from .lifecycle import MemoryState
from .store import ConflictError, NotFoundError, PersonalMemoryStore, utcnow_iso
from .types import Actor
from .validation import ValidationError, content_hash_for, validate_claim_kind, validate_snapshot

_NO_REMEMBER = re.compile(r"(?i)\b(?:не\s+(?:запоминай|сохраняй)|do\s+not\s+(?:remember|save)|don't\s+(?:remember|save))\b")


@dataclass(frozen=True)
class CapturePolicy:
    enabled: bool = False
    retention_days: int = 30

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.retention_days) is not int or not 1 <= self.retention_days <= 3650:
            raise ValidationError("capture policy: boolean enabled and retention_days 1..3650 required")


def zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValidationError("timezone: unknown IANA zone") from exc


def stamp(value):
    if not isinstance(value, str):
        raise ValidationError("timestamp: UTC ISO string required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("timestamp: invalid") from exc
    if parsed.tzinfo is None:
        raise ValidationError("timestamp: timezone required")
    return parsed.astimezone(utc.utc)


def _text(value, label, limit):
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ValidationError(f"{label}: invalid text")
    return value


def record_turn(actor: Actor, *, event_id: str, text: str, occurred_at: str,
                timezone: str = "UTC", role: str = "user", origin: str = "direct",
                session_id: str = "", attachments: tuple[str, ...] = (),
                parent_source_id: str | None = None, parent_source_version: int | None = None,
                policy: CapturePolicy, store: PersonalMemoryStore) -> dict:
    if not isinstance(policy, CapturePolicy):
        raise ValidationError("policy: CapturePolicy required")
    if not policy.enabled:
        return {"status": "skipped", "reason": "capture_disabled"}
    _text(event_id, "event_id", 512)
    _text(text, "text", 65536)
    if _NO_REMEMBER.search(text) or turn_has_blocked_secret(text):
        return {"status": "skipped", "reason": "excluded"}
    validate_snapshot(text.encode("utf-8"))
    if role not in {"user", "assistant", "tool"} or origin not in {"direct", "forwarded", "derived", "imported"}:
        raise ValidationError("role/origin: unknown value")
    if parent_source_id is not None or parent_source_version is not None:
        _text(parent_source_id, "parent_source_id", 128)
        if type(parent_source_version) is not int or parent_source_version < 1 or not (role == "assistant" or origin == "derived"):
            raise ValidationError("parent source: exact version and derived role/origin required")
    if not isinstance(session_id, str) or len(session_id) > 512:
        raise ValidationError("session_id: invalid")
    if not isinstance(attachments, (list, tuple)) or len(attachments) > 32:
        raise ValidationError("attachments: at most 32 references")
    for attachment in attachments:
        _text(attachment, "attachment", 512)
    zone(timezone)
    occurred = stamp(occurred_at)
    if occurred > datetime.now(utc.utc) + timedelta(seconds=60):
        raise ValidationError("occurred_at: future event")
    occurred_at = occurred.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = (occurred + timedelta(days=policy.retention_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if expires <= utcnow_iso():
        return {"status": "skipped", "reason": "expired"}
    metadata = {"title": "Original event", "role": role, "origin": origin, "timezone": timezone,
                "session_id": session_id, "attachments": json.dumps(list(attachments), ensure_ascii=False),
                "parent_source_id": parent_source_id or "", "parent_source_version": str(parent_source_version or "")}
    envelope = hashlib.sha256(encode([text, metadata, occurred_at, expires])).hexdigest()
    with store._tx() as conn:
        tenant = store._ensure_principal_on(conn, actor)
        event_hash = keyed_lookup_hash(derive_lookup_key(store._kek), tenant, "journal", event_id)
        row = conn.execute("SELECT * FROM memory_journal WHERE tenant_id=? AND event_hash=?", (tenant, event_hash)).fetchone()
        if row and row["redacted"]:
            return {"status": "forgotten"}
        if parent_source_id is not None:
            parent = conn.execute("SELECT * FROM memory_journal WHERE tenant_id=? AND source_id=? AND redacted=0 AND expires_at>?", (tenant, parent_source_id, utcnow_iso())).fetchone()
            active = store._newest_active_version(conn, tenant, parent_source_id) if parent else None
            capture = conn.execute("SELECT generation FROM memory_capture_generation WHERE tenant_id=? AND source_id=?", (tenant, parent_source_id)).fetchone()
            if active is None or active["version"] != parent_source_version or capture is None or capture[0] != generation_on(conn, tenant):
                raise NotFoundError("parent source unavailable")
        if row:
            sid = row["source_id"]
            lineage = conn.execute("SELECT parent_source_id,parent_source_version FROM memory_source_lineage WHERE tenant_id=? AND source_id=?", (tenant, sid)).fetchone()
            existing_parent = (lineage[0], lineage[1]) if lineage else (None, None)
            if existing_parent != (parent_source_id, parent_source_version):
                raise ConflictError("event parent identity is immutable")
            old = store._newest_active_version(conn, tenant, sid)
            if old is None:
                return {"status": "forgotten"}
            _, _, old_metadata = store._decrypt_version(conn, tenant, sid, old)
            if (old_metadata.get("role"), old_metadata.get("origin")) != (role, origin):
                raise ConflictError("event role and origin are immutable")
            if row["envelope_hash"] == envelope:
                return {"status": "replayed", "source_id": sid, "version": old["version"], "source_version_id": old["id"]}
            version = old["version"] + 1
            erase_derived_sources_on(store, conn, tenant, sid)
            # Original event edits revoke dependent facts immediately. Keep their
            # version history but remove them from current retrieval and FTS.
            from .fts_index import delete_claim_fts_rows
            for ev in conn.execute("SELECT DISTINCT e.claim_id FROM memory_evidence e WHERE e.tenant_id=? AND e.source_id=? AND e.claim_version=(SELECT MAX(v.version) FROM memory_claim_versions v WHERE v.claim_id=e.claim_id AND v.tenant_id=e.tenant_id)", (tenant, sid)).fetchall():
                latest = store._newest_claim_version(conn, tenant, ev["claim_id"])
                if latest and latest["state"] in {"candidate", "confirmed"}:
                    content, explanation = store._decrypt_claim_fields(conn, tenant_id=tenant, claim_id=ev["claim_id"], version_row=latest)
                    conn.execute("UPDATE memory_claim_versions SET state='superseded' WHERE id=?", (latest["id"],))
                    store._insert_claim_version(conn, tenant_id=tenant, claim_id=ev["claim_id"], version=latest["version"] + 1,
                        state=MemoryState.RETRACTED, sensitivity=latest["sensitivity"], content=content,
                        confidence=latest["confidence"], explanation=explanation, valid_from=latest["valid_from"],
                        valid_to=latest["valid_to"], expires_at=None, supersedes_id=latest["id"], now=utcnow_iso(), change_reason="retracted")
                    delete_claim_fts_rows(conn, tenant_id=tenant, claim_id=ev["claim_id"])
            conn.execute("UPDATE memory_journal SET envelope_hash=?,occurred_at=?,expires_at=? WHERE source_id=?",
                         (envelope, occurred_at, expires, sid))
        else:
            sid, version = str(uuid.uuid4()), 1
            kind = "auto_insight_turn" if role == "user" and origin == "direct" else "assistant_message" if role == "assistant" else "document"
            now = utcnow_iso()
            conn.execute("INSERT INTO memory_sources(id,tenant_id,source_kind,keyed_lookup_hash,created_at) VALUES(?,?,?,?,?)",
                         (sid, tenant, kind, "", now))
            conn.execute("INSERT INTO memory_journal VALUES(?,?,?,?,?,?,0)", (sid, tenant, event_hash, envelope, occurred_at, expires))
            if parent_source_id is not None:
                conn.execute("INSERT INTO memory_source_lineage VALUES(?,?,?,?)", (tenant, sid, parent_source_id, parent_source_version))
        # A late arrival/edit revokes an earlier claim of an empty complete day.
        for coverage in conn.execute("SELECT day,timezone FROM memory_capture_days WHERE tenant_id=?", (tenant,)).fetchall():
            if occurred.astimezone(zone(coverage["timezone"])).date().isoformat() == coverage["day"]:
                conn.execute("DELETE FROM memory_capture_days WHERE tenant_id=? AND day=? AND timezone=?", (tenant, coverage["day"], coverage["timezone"]))
        vid = store._insert_source_version(conn, tenant_id=tenant, source_id=sid, version=version,
                fields={"snapshot": text.encode("utf-8"), "external_ref": event_id, "metadata": metadata,
                        "content_hash": content_hash_for(text.encode("utf-8")), "observed_at": occurred_at, "retention_class": "standard"}, now=utcnow_iso())
        conn.execute("INSERT INTO memory_capture_generation VALUES(?,?,?) ON CONFLICT(source_id) DO UPDATE SET generation=excluded.generation", (sid, tenant, generation_on(conn, tenant)))
        return {"status": "recorded", "source_id": sid, "version": version, "source_version_id": vid}


def get_journal_event(actor: Actor, *, event_id: str, store: PersonalMemoryStore) -> dict | None:
    """Resolve a host event identity without capture, plaintext or learning."""
    _text(event_id, "event_id", 512)
    with store._tx() as conn:
        tenant = store._ensure_principal_on(conn, actor)
        event_hash = keyed_lookup_hash(derive_lookup_key(store._kek), tenant, "journal", event_id)
        row = conn.execute("SELECT source_id,occurred_at FROM memory_journal WHERE tenant_id=? AND event_hash=? AND redacted=0 AND expires_at>?", (tenant, event_hash, utcnow_iso())).fetchone()
        if row is None:
            return None
        active = store._newest_active_version(conn, tenant, row["source_id"])
        if active is None:
            return None
        capture = conn.execute("SELECT generation FROM memory_capture_generation WHERE tenant_id=? AND source_id=?", (tenant, row["source_id"])).fetchone()
        return {"source_id": row["source_id"], "version": active["version"], "source_version_id": active["id"], "occurred_at": row["occurred_at"], "generation": capture[0] if capture else None}


def erase_derived_sources_on(store, conn, tenant, source_id):
    """Forget generated outputs conservatively; exact semantic ancestry is unknown.

    A response can recall an older memory, not only its immediate parent. Purge
    all assistant-origin records for this actor plus declared derived children.
    Unrelated direct user originals are retained.
    """
    # A generated answer may still be in flight for another unchanged prompt.
    # Its parent's capture generation must fail admission after this mutation.
    invalidate_generation_on(conn, tenant)
    children = {r[0] for r in conn.execute("SELECT id FROM memory_sources WHERE tenant_id=? AND source_kind='assistant_message' AND deleted_at IS NULL AND id<>?", (tenant, source_id))}
    edges = conn.execute("SELECT source_id,parent_source_id FROM memory_source_lineage WHERE tenant_id=?", (tenant,)).fetchall()
    pending = {source_id, *children}
    while pending:
        frontier = {r[0] for r in edges if r[1] in pending and r[0] not in children and r[0] != source_id}
        children.update(frontier)
        pending = frontier
    # Mark before recursively erasing supported facts: re-entry then sees no
    # active assistant sources and cycles cannot repeatedly erase the same item.
    now = utcnow_iso()
    for child in children:
        conn.execute("UPDATE memory_sources SET deleted_at=? WHERE tenant_id=? AND id=? AND deleted_at IS NULL", (now, tenant, child))
        conn.execute("DELETE FROM memory_source_lineage WHERE tenant_id=? AND (source_id=? OR parent_source_id=?)", (tenant, child, child))
    conn.execute("DELETE FROM memory_source_lineage WHERE tenant_id=? AND parent_source_id=?", (tenant, source_id))
    for child in children:
        store._erase_source_on(conn, tenant_id=tenant, source_id=child, _cascade_derived=False)


def journal_rows_on(store, conn, tenant):
    return conn.execute("""SELECT j.*,v.id AS source_version_id,v.version,v.snapshot_enc,v.locator_enc,v.metadata_enc
        FROM memory_journal j JOIN memory_sources s ON s.id=j.source_id AND s.tenant_id=j.tenant_id
        JOIN memory_source_versions v ON v.source_id=j.source_id AND v.tenant_id=j.tenant_id
        WHERE j.tenant_id=? AND s.deleted_at IS NULL AND j.expires_at>?
        AND v.deletion_state='active' AND v.version=(SELECT MAX(x.version) FROM memory_source_versions x WHERE x.source_id=j.source_id)
        ORDER BY j.occurred_at,j.source_id""", (tenant, utcnow_iso())).fetchall()


def event_on(store, conn, tenant, row):
    text, _, metadata = store._decrypt_version(conn, tenant, row["source_id"], row)
    return {"source_id": row["source_id"], "version": row["version"], "occurred_at": row["occurred_at"],
            "text": text.decode("utf-8"), "role": metadata.get("role"), "origin": metadata.get("origin"),
            "session_id": metadata.get("session_id", ""), "timezone": metadata.get("timezone", "UTC"),
            "parent_source_id": metadata.get("parent_source_id") or None,
            "parent_source_version": int(metadata["parent_source_version"]) if metadata.get("parent_source_version") else None,
            "attachments": json.loads(metadata.get("attachments", "[]"))}


def query_journal(actor: Actor, *, store: PersonalMemoryStore, limit: int = 50,
                  cursor: str | None = None, query: str | None = None, include_text: bool = False) -> dict:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValidationError("limit: 1..500 required")
    if query is not None and not isinstance(query, str):
        raise ValidationError("query: string required")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 200):
        raise ValidationError("cursor: invalid")
    items, last, more = [], None, False
    with store._tx() as conn:
        tenant = store._ensure_principal_on(conn, actor)
        for row in journal_rows_on(store, conn, tenant):
            key = row["occurred_at"] + "|" + row["source_id"]
            if cursor is not None and key <= cursor:
                continue
            item = event_on(store, conn, tenant, row)
            if query and query.casefold() not in item["text"].casefold():
                continue
            if len(items) == limit:
                more = True
                break
            if not include_text:
                item.pop("text")
            items.append(item)
            last = key
    return {"items": items, "next_cursor": last if more else None}


def _evidence_source_on(store, conn, tenant, source_id, source_version, start, end):
    row = conn.execute("""SELECT j.*,v.id AS source_version_id,v.version,v.snapshot_enc,v.locator_enc,v.metadata_enc
        FROM memory_journal j JOIN memory_source_versions v ON v.source_id=j.source_id AND v.tenant_id=j.tenant_id
        JOIN memory_sources s ON s.id=j.source_id WHERE j.tenant_id=? AND j.source_id=? AND v.version=?
        AND j.expires_at>? AND v.deletion_state='active' AND s.deleted_at IS NULL
        AND v.version=(SELECT MAX(x.version) FROM memory_source_versions x WHERE x.source_id=j.source_id)""",
        (tenant, source_id, source_version, utcnow_iso())).fetchone()
    if row is None:
        raise NotFoundError("Evidence not found")
    event = event_on(store, conn, tenant, row)
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(event["text"]):
        raise ValidationError("evidence: invalid character span")
    return event, event["text"][start:end]


def validate_statement_on(store, conn, tenant, source_id, source_version, start, end):
    event, quote = _evidence_source_on(store, conn, tenant, source_id, source_version, start, end)
    if event["role"] != "user" or event["origin"] != "direct":
        raise ValidationError("evidence: direct owner message required")
    # A quote is not semantic entailment. Refuse partial statements that
    # could drop a negation, condition, speaker or trailing qualification.
    left = max(event["text"].rfind(ch, 0, start) for ch in ".!?\n") + 1
    prefix = event["text"][left:start].strip()
    if prefix and re.fullmatch(r"(?i)(?:please\s+)?(?:remember(?:\s+that)?|запомни(?:\s+что)?|исправь(?:\s+договорённость)?|correct(?:\s+agreement)?)[ :]*", prefix) is None:
        raise ValidationError("evidence: whole statement required")
    tail = "" if quote[-1] in ".!?\n" else re.split(r"[.!?\n]", event["text"][end:], maxsplit=1)[0]
    if tail.strip():
        raise ValidationError("evidence: whole statement required")
    if not insight_content_allowed(quote):
        raise ValidationError("evidence: excluded content")
    return event, quote


def remember_from_evidence(actor: Actor, *, source_id: str, source_version: int, start: int, end: int,
                           claim_kind: str = "confirmed_fact", store: PersonalMemoryStore) -> dict:
    validate_claim_kind(claim_kind)
    with store._tx() as conn:
        tenant = store._ensure_principal_on(conn, actor)
        event, quote = _evidence_source_on(store, conn, tenant, source_id, source_version, start, end)
        validate_statement_on(store, conn, tenant, source_id, source_version, start, end)
        existing = conn.execute("""SELECT e.claim_id FROM memory_evidence e JOIN memory_claims c ON c.id=e.claim_id
            WHERE e.tenant_id=? AND e.source_id=? AND e.source_version=? AND e.span_start=? AND e.span_end=? AND c.deleted_at IS NULL""",
            (tenant, source_id, source_version, start, end)).fetchone()
        if existing:
            latest = store._newest_claim_version(conn, tenant, existing["claim_id"])
            return {"claim_id": existing["claim_id"], "version": latest["version"], "state": latest["state"], "replayed": True}
        overlap = conn.execute("SELECT 1 FROM memory_evidence e JOIN memory_claims c ON c.id=e.claim_id WHERE e.tenant_id=? AND e.source_id=? AND e.source_version=? AND e.span_start<? AND e.span_end>? AND c.deleted_at IS NULL LIMIT 1", (tenant, source_id, source_version, end, start)).fetchone()
        if overlap:
            raise ValidationError("evidence: overlaps another retained fact")
        cid, now = str(uuid.uuid4()), utcnow_iso()
        # Candidates use the existing reconciliation policy before promotion.
        conn.execute("INSERT INTO memory_claims(id,tenant_id,claim_kind,created_at) VALUES(?,?,?,?)", (cid, tenant, claim_kind, now))
        expires = (datetime.now(utc.utc) + timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%SZ")
        vid = store._insert_claim_version(conn, tenant_id=tenant, claim_id=cid, version=1, state=MemoryState.CANDIDATE,
            sensitivity="normal", content=quote.encode(), confidence=0.9, explanation="Exact owner source span",
            valid_from=None, valid_to=None, expires_at=expires, supersedes_id=None, now=now, change_reason="created")
        store._insert_support(conn, tenant_id=tenant, claim_id=cid, source_ids=[source_id], now=now)
        conn.execute("INSERT INTO memory_evidence VALUES(?,?,?,?,?,?,?)", (tenant, cid, 1, source_id, source_version, start, end))
        store._sync_claim_fts_on(conn, tenant_id=tenant, claim_id=cid, state=MemoryState.CANDIDATE, sensitivity="normal", content=quote.encode(), explanation="Exact owner source span")
    from .reconcile import propose_reconciliations
    proposed = propose_reconciliations(actor, cid, store=store)
    result = {"claim_id": cid, "claim_version_id": vid, "version": 1, "state": "candidate", "reconciliations": proposed}
    if store.get_memory_mode(actor) == "auto_insight" and not proposed:
        result.update(store.confirm_claim(actor, cid, reliable_source_id=source_id))
    return result



def candidate_from_evidence_on(store, conn, tenant, proposal):
    """Validate and persist an untrusted extraction proposal as a candidate."""
    if not isinstance(proposal, dict) or set(proposal) - {"source_id", "source_version", "start", "end", "claim_kind"}:
        raise ValidationError("proposal: invalid fields")
    sid, sv, start, end = (proposal.get(k) for k in ("source_id", "source_version", "start", "end"))
    _, quote = validate_statement_on(store, conn, tenant, sid, sv, start, end)
    kind = validate_claim_kind(proposal.get("claim_kind", "candidate_insight"))
    existing = conn.execute("SELECT e.claim_id FROM memory_evidence e JOIN memory_claims c ON c.id=e.claim_id WHERE e.tenant_id=? AND e.source_id=? AND e.source_version=? AND e.span_start<? AND e.span_end>? AND c.deleted_at IS NULL LIMIT 1", (tenant, sid, sv, end, start)).fetchone()
    if existing:
        return {"claim_id": existing["claim_id"], "replayed": True}
    cid, now = str(uuid.uuid4()), utcnow_iso()
    conn.execute("INSERT INTO memory_claims(id,tenant_id,claim_kind,created_at) VALUES(?,?,?,?)", (cid, tenant, kind, now))
    vid = store._insert_claim_version(conn, tenant_id=tenant, claim_id=cid, version=1, state=MemoryState.CANDIDATE,
        sensitivity="normal", content=quote.encode(), confidence=0.5, explanation="Untrusted extractor proposal",
        valid_from=None, valid_to=None, expires_at=(datetime.now(utc.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        supersedes_id=None, now=now, change_reason="created")
    store._insert_support(conn, tenant_id=tenant, claim_id=cid, source_ids=[sid], now=now)
    conn.execute("INSERT INTO memory_evidence VALUES(?,?,?,?,?,?,?)", (tenant, cid, 1, sid, sv, start, end))
    store._sync_claim_fts_on(conn, tenant_id=tenant, claim_id=cid, state=MemoryState.CANDIDATE, sensitivity="normal", content=quote.encode(), explanation="Untrusted extractor proposal")
    store._append_audit(conn, tenant_id=tenant, actor_id=tenant, action="claim.create_candidate", target_kind="claim", target_id=cid, outcome="created", policy_reason="")
    return {"claim_id": cid, "claim_version_id": vid, "version": 1, "state": "candidate"}

def evidence_for_claim(actor: Actor, claim_id: str, *, store: PersonalMemoryStore, max_chars: int = 2000) -> list[dict]:
    """Explicitly open bounded originals; ordinary recall returns pointers only."""
    if type(max_chars) is not int or not 1 <= max_chars <= 8000:
        raise ValidationError("max_chars: 1..8000 required")
    from .query import get_authorized_claim
    claim = get_authorized_claim(actor, claim_id, include_content=False, store=store)
    if claim["state"] != "confirmed":
        return []
    results, used = [], 0
    with store._tx() as conn:
        tenant = store._ensure_principal_on(conn, actor)
        for row in conn.execute("SELECT * FROM memory_evidence WHERE tenant_id=? AND claim_id=? AND claim_version=?", (tenant, claim_id, claim["version"])):
            try:
                event, quote = _evidence_source_on(store, conn, tenant, row["source_id"], row["source_version"], row["span_start"], row["span_end"])
            except NotFoundError:
                continue
            if used + len(quote) > max_chars:
                continue
            results.append({"source_id": row["source_id"], "source_version": row["source_version"], "start": row["span_start"], "end": row["span_end"], "quote": quote, "occurred_at": event["occurred_at"], "role": event["role"], "origin": event["origin"]})
            used += len(quote)
    return results


def copy_evidence_on(conn, tenant, claim_id, previous, current):
    conn.execute("""INSERT OR IGNORE INTO memory_evidence SELECT tenant_id,claim_id,?,source_id,source_version,span_start,span_end
                    FROM memory_evidence WHERE tenant_id=? AND claim_id=? AND claim_version=?""", (current, tenant, claim_id, previous))


def erase_evidence_on(store, conn, tenant, claim_id, now):
    """Forget exact spans across retained originals; preserve non-overlapping data."""
    # Legacy claims can have source support without span records. Their content
    # may still have appeared in a generated response, so purge those too.
    erase_derived_sources_on(store, conn, tenant, "")
    rows = conn.execute("SELECT * FROM memory_evidence WHERE tenant_id=? AND claim_id=?", (tenant, claim_id)).fetchall()
    affected_claims = set()
    by_source = {}
    for row in rows:
        by_source.setdefault(row["source_id"], []).append(row)
    for sid, evidence in by_source.items():
        versions = conn.execute("SELECT * FROM memory_source_versions WHERE tenant_id=? AND source_id=? AND deletion_state='active'", (tenant, sid)).fetchall()
        decoded = []
        for version in versions:
            original, locator, metadata = store._decrypt_version(conn, tenant, sid, version)
            decoded.append((version, original.decode("utf-8"), locator, metadata))
        originals = {version["version"]: text for version, text, _, _ in decoded}
        for version, text, locator, metadata in decoded:
            spans = set()
            for ev in evidence:
                old = originals.get(ev["source_version"])
                if old is None:
                    continue
                if version["version"] == ev["source_version"]:
                    spans.add((ev["span_start"], ev["span_end"]))
                else:
                    # Preserve only unchanged correspondence outside the span.
                    # An edit may relocate/rewrite private text elsewhere;
                    # changed regions therefore use a privacy-first fallback.
                    for tag, a0, a1, b0, b1 in SequenceMatcher(None, old, text, autojunk=False).get_opcodes():
                        begin, finish = ev["span_start"], ev["span_end"]
                        if tag == "equal":
                            lo, hi = max(a0, begin), min(a1, finish)
                            if lo < hi:
                                spans.add((b0 + lo - a0, b0 + hi - a0))
                        elif tag in {"insert", "replace"} and b0 < b1:
                            # Changed regions may be relocated or rewritten
                            # forgotten text, even outside the old offsets.
                            # Only equal regions outside the forgotten span
                            # are provably unrelated; preserve those above.
                            spans.add((b0, b1))
            for begin, finish in spans:
                affected_claims.update(r[0] for r in conn.execute("SELECT DISTINCT claim_id FROM memory_evidence WHERE tenant_id=? AND source_id=? AND source_version=? AND claim_id<>? AND span_start<? AND span_end>?", (tenant, sid, version["version"], claim_id, finish, begin)))
            chars = list(text)
            for start, end in spans:
                chars[start:end] = " " * (end - start)
            redacted = "".join(chars)
            if redacted == text:
                continue
            # Rotate the version DEK; old ciphertext cannot be read with the new key.
            wrapped, snap, loc, meta = store._encrypt_source_fields(tenant_id=tenant, source_id=sid, version=version["version"], snapshot=redacted.encode(), external_ref=locator, metadata=metadata)
            conn.execute("UPDATE memory_keys SET wrapped_dek=? WHERE tenant_id=? AND entity_kind='source' AND entity_id=? AND version=?", (wrapped, tenant, sid, version["version"]))
            conn.execute("UPDATE memory_source_versions SET snapshot_enc=?,locator_enc=?,metadata_enc=?,content_hash=? WHERE id=?", (snap, loc, meta, content_hash_for(redacted.encode()), version["id"]))
        conn.execute("UPDATE memory_journal SET redacted=1 WHERE tenant_id=? AND source_id=?", (tenant, sid))
        erase_derived_sources_on(store, conn, tenant, sid)
    conn.execute("DELETE FROM memory_evidence WHERE tenant_id=? AND claim_id=?", (tenant, claim_id))
    conn.execute("DELETE FROM memory_relations WHERE tenant_id=? AND (from_claim_id=? OR to_claim_id=?)", (tenant, claim_id, claim_id))
    # A boundary-crossing edit may make a second derived claim inseparable
    # from the redacted range. Erase that derived claim without recursively
    # redacting any provably unrelated source spans elsewhere.
    for affected in affected_claims:
        if store._erasable_claim(conn, tenant, affected) is not None:
            store._erase_claim_on(conn, tenant_id=tenant, claim_id=affected, _redact_sources=False)
    purge_projections_on(store, conn, tenant, now)


def prune_journal(actor: Actor, *, store: PersonalMemoryStore, now: str | None = None) -> dict:
    cutoff = stamp(now or utcnow_iso()).strftime("%Y-%m-%dT%H:%M:%SZ")
    with store._tx() as conn:
        tenant = store._ensure_principal_on(conn, actor)
        ids = [r[0] for r in conn.execute("SELECT j.source_id FROM memory_journal j JOIN memory_sources s ON s.id=j.source_id WHERE j.tenant_id=? AND j.expires_at<=? AND s.deleted_at IS NULL", (tenant, cutoff))]
        for sid in ids:
            store._erase_source_on(conn, tenant_id=tenant, source_id=sid)
    if ids:
        store._compact_after_erasure()
    return {"expired": len(ids)}


def correct_from_evidence(actor: Actor, claim_id: str, *, source_id: str, source_version: int,
                          start: int, end: int, explanation: str = "Owner source correction",
                          store: PersonalMemoryStore) -> dict:
    """Correct and bind the new source atomically; old lineage remains erasable."""
    with store._tx() as conn:
        tenant = store._ensure_principal_on(conn, actor)
        _, quote = validate_statement_on(store, conn, tenant, source_id, source_version, start, end)
    return store.correct_claim(actor, claim_id, content=quote.encode("utf-8"), explanation=explanation,
        source_ids=[source_id], _evidence=(source_id, source_version, start, end))

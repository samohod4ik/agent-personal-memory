"""Deterministic policy for the BB pilot's global reply-length preference."""
from __future__ import annotations

from .fts_index import delete_claim_fts_rows
from .journal import event_on
from .store import utcnow_iso
from .validation import content_hash_for

REPLY_LENGTH_POLICY = "auto_core_reply_length"
_STATEMENTS = {
    "Во всех чатах я предпочитаю краткие ответы": "brief",
    "Во всех чатах я предпочитаю короткие ответы": "brief",
    "Во всех чатах я предпочитаю подробные ответы": "detailed",
    "I prefer brief replies in all chats": "brief",
    "I prefer concise replies in all chats": "brief",
    "I prefer detailed replies in all chats": "detailed",
}
_SOURCE_HASHES = {
    content_hash_for(text.encode("utf-8"))
    for statement in _STATEMENTS
    for text in (statement, statement + ".")
}


def reply_length(text: str) -> str | None:
    return _STATEMENTS.get(text.removesuffix("."))


def latest_reply_length_on(store, conn, tenant: str) -> tuple[str, str | None] | None:
    """Read exact owner assertions independently of which periods were judged."""
    hashes = ",".join("?" for _ in _SOURCE_HASHES)
    rows = conn.execute(f"""SELECT j.*,v.id AS source_version_id,v.version,v.snapshot_enc,v.locator_enc,v.metadata_enc
        FROM memory_journal j JOIN memory_sources s ON s.id=j.source_id AND s.tenant_id=j.tenant_id
        JOIN memory_source_versions v ON v.source_id=s.id AND v.tenant_id=s.tenant_id
        WHERE j.tenant_id=? AND j.expires_at>? AND j.redacted=0
        AND s.source_kind='auto_insight_turn' AND s.deleted_at IS NULL
        AND v.deletion_state='active' AND v.content_hash IN ({hashes})
        AND v.version=(SELECT MAX(x.version) FROM memory_source_versions x WHERE x.source_id=s.id)
        ORDER BY j.occurred_at DESC""", (tenant, utcnow_iso(), *_SOURCE_HASHES))
    latest = None
    values = set()
    for row in rows:
        if latest is not None and row["occurred_at"] != latest:
            break
        event = event_on(store, conn, tenant, row)
        value = reply_length(event["text"])
        if value and event.get("whole_owner_turn") and not event["attachments"]:
            latest = row["occurred_at"]
            values.add(value)
    if latest is None:
        return None
    return latest, next(iter(values)) if len(values) == 1 else None


def promote_reply_length_on(store, conn, tenant: str, candidates: list, proof: list) -> bool:
    """Apply at most one promotion, consuming only a freshly created candidate."""
    matches = [
        (candidate, event, value)
        for candidate, (event, quote) in zip(candidates, proof, strict=True)
        if not candidate.get("replayed") and quote == event["text"] and event.get("whole_owner_turn")
        and not event["attachments"]
        and (value := reply_length(quote)) is not None
    ]
    if not matches:
        return False
    latest = latest_reply_length_on(store, conn, tenant)
    from .layered import current_claims_on

    # Existing confirmed prose has no authoritative scope/slot metadata.
    # The pilot leaves those stores to their current confirmed facts.
    if current_claims_on(store, conn, tenant, include_auto_core=False):
        return False
    for candidate, event, value in matches:
        if latest != (event["occurred_at"], value):
            continue
        active = conn.execute("""SELECT v.id, v.claim_id, j.occurred_at
            FROM memory_claim_versions v JOIN memory_evidence e ON e.tenant_id=v.tenant_id
            AND e.claim_id=v.claim_id AND e.claim_version=v.version
            JOIN memory_journal j ON j.tenant_id=e.tenant_id AND j.source_id=e.source_id
            WHERE v.tenant_id=? AND v.state='confirmed' AND v.change_reason=?""",
            (tenant, REPLY_LENGTH_POLICY)).fetchall()
        if any(row["occurred_at"] >= event["occurred_at"] for row in active):
            continue
        source = conn.execute(
            "SELECT expires_at FROM memory_journal WHERE tenant_id=? AND source_id=? AND redacted=0 AND expires_at>?",
            (tenant, event["source_id"], utcnow_iso()),
        ).fetchone()
        if source is None:
            continue
        claim_id = candidate["claim_id"]
        conn.execute("UPDATE memory_claims SET claim_kind='preference' WHERE tenant_id=? AND id=?",
                     (tenant, claim_id))
        confirmed = store._confirm_claim_on(
            conn, tenant, claim_row=store._lookup_claim(conn, tenant, claim_id),
            newest=store._newest_claim_version(conn, tenant, claim_id),
            support_ids=[event["source_id"]], change_reason=REPLY_LENGTH_POLICY,
            expires_at=source["expires_at"], policy_reason=REPLY_LENGTH_POLICY,
        )
        for old in active:
            conn.execute("UPDATE memory_claim_versions SET state='superseded' WHERE id=?", (old["id"],))
            delete_claim_fts_rows(conn, tenant_id=tenant, claim_id=old["claim_id"])
            store._append_audit(
                conn, tenant_id=tenant, actor_id=tenant, action="claim.supersede",
                target_kind="claim_version", target_id=old["id"], outcome="superseded",
                policy_reason=REPLY_LENGTH_POLICY,
            )
        candidate.update(
            claim_version_id=confirmed["claim_version_id"], version=confirmed["version"],
            state=confirmed["state"], policy=REPLY_LENGTH_POLICY,
        )
        return True
    return False

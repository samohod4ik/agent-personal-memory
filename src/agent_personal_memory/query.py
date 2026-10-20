"""Actor-bound authorized queries over the encrypted personal-memory store."""

from __future__ import annotations

from typing import Any

from .lifecycle import ACTIVE_READ_STATES, MemoryState
from .store import NotFoundError, PersonalMemoryStore, utcnow_iso
from .types import Actor
from .validation import (
    ValidationError,
    validate_list_limit,
    validate_query_claim_kinds,
    validate_query_source_kinds,
    validate_query_states,
    validate_query_time_window,
)

_CLAIM_NOT_FOUND = "Claim not found"
_SOURCE_NOT_FOUND = "Source not found"


def _memory(store: PersonalMemoryStore | None) -> PersonalMemoryStore:
    return store if store is not None else PersonalMemoryStore()


def _reject(
    store: PersonalMemoryStore,
    conn: Any,
    *,
    tenant_id: str,
    action: str,
    target_kind: str,
) -> None:
    store._append_audit(
        conn,
        tenant_id=tenant_id,
        actor_id=tenant_id,
        action=action,
        target_kind=target_kind,
        target_id="",
        outcome="rejected",
        policy_reason="validation_failed",
    )


def _deny(
    store: PersonalMemoryStore,
    conn: Any,
    *,
    tenant_id: str,
    action: str,
    target_kind: str,
) -> None:
    store._append_audit(
        conn,
        tenant_id=tenant_id,
        actor_id=tenant_id,
        action=action,
        target_kind=target_kind,
        target_id="",
        outcome="denied",
        policy_reason="not_found",
    )


def _claim_metadata(row: Any) -> dict[str, object]:
    return {
        "claim_id": str(row["claim_id"]),
        "claim_version_id": str(row["claim_version_id"]),
        "version": int(row["version"]),
        "claim_kind": str(row["claim_kind"]),
        "state": str(row["state"]),
        "sensitivity": str(row["sensitivity"]),
        "confidence": float(row["confidence"]),
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "expires_at": row["expires_at"],
        "created_at": str(row["created_at"]),
    }


def _source_metadata(row: Any) -> dict[str, object]:
    return {
        "source_id": str(row["source_id"]),
        "source_version_id": str(row["source_version_id"]),
        "version": int(row["version"]),
        "source_kind": str(row["source_kind"]),
        "content_hash": str(row["content_hash"]),
        "observed_at": str(row["observed_at"]),
        "retention_class": str(row["retention_class"]),
        "created_at": str(row["created_at"]),
        "deletion_state": str(row["deletion_state"]),
    }


def query_claims(
    actor: Actor,
    *,
    states: object = None,
    claim_kinds: object = None,
    valid_from: object = None,
    valid_to: object = None,
    include_content: bool = False,
    include_sources: bool = False,
    limit: object = 50,
    store: PersonalMemoryStore | None = None,
) -> list[dict[str, object]]:
    memory = _memory(store)
    pending_error: Exception | None = None
    rows: list[dict[str, object]] | None = None
    with memory._tx() as conn:
        tenant_id = memory._ensure_principal_on(conn, actor)
        try:
            state_filter = validate_query_states(states)
            kind_filter = validate_query_claim_kinds(claim_kinds)
            window_from, window_to = validate_query_time_window(
                "valid_from", valid_from, "valid_to", valid_to
            )
            lim = validate_list_limit(limit)
        except ValidationError as exc:
            _reject(
                memory,
                conn,
                tenant_id=tenant_id,
                action="claim.query",
                target_kind="claim",
            )
            pending_error = exc
        if pending_error is None:
            if state_filter == [] or kind_filter == []:
                rows = []
            else:
                now = utcnow_iso()
                sql = (
                    "SELECT c.id AS claim_id, v.id AS claim_version_id, v.version, "
                    "c.claim_kind, v.state, v.sensitivity, v.confidence, "
                    "v.valid_from, v.valid_to, v.expires_at, v.created_at, "
                    "v.content_enc, v.explanation_enc "
                    "FROM memory_claims c "
                    "JOIN memory_claim_versions v "
                    "ON v.claim_id = c.id AND v.tenant_id = c.tenant_id "
                    "WHERE c.tenant_id = ? AND c.deleted_at IS NULL "
                    "AND v.version = ("
                    "SELECT MAX(v2.version) FROM memory_claim_versions v2 "
                    "WHERE v2.claim_id = c.id"
                    ") "
                    "AND v.sensitivity <> 'restricted' "
                    "AND (v.expires_at IS NULL OR v.expires_at > ?)"
                )
                params: list[object] = [tenant_id, now]
                if state_filter is None:
                    active = sorted(
                        item.value for item in ACTIVE_READ_STATES
                    )
                    placeholders = ",".join("?" for _ in active)
                    sql += f" AND v.state IN ({placeholders})"
                    params.extend(active)
                else:
                    placeholders = ",".join("?" for _ in state_filter)
                    sql += f" AND v.state IN ({placeholders})"
                    params.extend(state_filter)
                if kind_filter is not None:
                    placeholders = ",".join("?" for _ in kind_filter)
                    sql += f" AND c.claim_kind IN ({placeholders})"
                    params.extend(kind_filter)
                if window_from is not None:
                    sql += " AND (v.valid_to IS NULL OR v.valid_to >= ?)"
                    params.append(window_from)
                if window_to is not None:
                    sql += " AND (v.valid_from IS NULL OR v.valid_from <= ?)"
                    params.append(window_to)
                sql += " ORDER BY v.created_at DESC, v.rowid DESC LIMIT ?"
                params.append(lim)
                fetched = conn.execute(sql, params).fetchall()
                results: list[dict[str, object]] = []
                for row in fetched:
                    item = _claim_metadata(row)
                    if include_sources:
                        source = conn.execute(
                            "SELECT s.id AS source_id, s.source_kind FROM memory_support m "
                            "JOIN memory_sources s ON s.id = m.source_id AND s.tenant_id = m.tenant_id "
                            "JOIN memory_source_versions v ON v.source_id = s.id AND v.tenant_id = s.tenant_id "
                            "WHERE m.tenant_id = ? AND m.claim_id = ? AND s.deleted_at IS NULL "
                            "AND v.deletion_state = 'active' AND v.version = ("
                            "SELECT MAX(v2.version) FROM memory_source_versions v2 "
                            "WHERE v2.source_id = s.id) ORDER BY s.created_at, s.id LIMIT 1",
                            (tenant_id, str(row["claim_id"])),
                        ).fetchone()
                        if source is not None:
                            item["source_id"] = str(source["source_id"])
                            item["source_kind"] = str(source["source_kind"])
                    if include_content:
                        content, explanation = memory._decrypt_claim_fields(
                            conn,
                            tenant_id=tenant_id,
                            claim_id=str(row["claim_id"]),
                            version_row=row,
                        )
                        item["content"] = content
                        item["explanation"] = explanation
                    results.append(item)
                rows = results
    if pending_error is not None:
        raise pending_error
    return rows if rows is not None else []


def query_sources(
    actor: Actor,
    *,
    source_kinds: object = None,
    observed_from: object = None,
    observed_to: object = None,
    include_payload: bool = False,
    limit: object = 50,
    store: PersonalMemoryStore | None = None,
) -> list[dict[str, object]]:
    memory = _memory(store)
    pending_error: Exception | None = None
    rows: list[dict[str, object]] | None = None
    with memory._tx() as conn:
        tenant_id = memory._ensure_principal_on(conn, actor)
        try:
            kind_filter = validate_query_source_kinds(source_kinds)
            window_from, window_to = validate_query_time_window(
                "observed_from", observed_from, "observed_to", observed_to
            )
            lim = validate_list_limit(limit)
        except ValidationError as exc:
            _reject(
                memory,
                conn,
                tenant_id=tenant_id,
                action="source.query",
                target_kind="source",
            )
            pending_error = exc
        if pending_error is None:
            if kind_filter == []:
                rows = []
            else:
                sql = (
                    "SELECT s.id AS source_id, v.id AS source_version_id, v.version, "
                    "s.source_kind, v.content_hash, v.observed_at, v.retention_class, "
                    "s.created_at, v.deletion_state, v.snapshot_enc, v.locator_enc, "
                    "v.metadata_enc "
                    "FROM memory_sources s "
                    "JOIN memory_source_versions v "
                    "ON v.source_id = s.id AND v.tenant_id = s.tenant_id "
                    "WHERE s.tenant_id = ? AND s.deleted_at IS NULL "
                    "AND v.deletion_state = 'active' AND v.version = ("
                    "SELECT MAX(v2.version) FROM memory_source_versions v2 "
                    "WHERE v2.source_id = s.id AND v2.deletion_state = 'active'"
                    ")"
                )
                params: list[object] = [tenant_id]
                if kind_filter is not None:
                    placeholders = ",".join("?" for _ in kind_filter)
                    sql += f" AND s.source_kind IN ({placeholders})"
                    params.extend(kind_filter)
                if window_from is not None:
                    sql += " AND v.observed_at >= ?"
                    params.append(window_from)
                if window_to is not None:
                    sql += " AND v.observed_at <= ?"
                    params.append(window_to)
                sql += " ORDER BY v.observed_at DESC, s.id DESC LIMIT ?"
                params.append(lim)
                fetched = conn.execute(sql, params).fetchall()
                results: list[dict[str, object]] = []
                for row in fetched:
                    item = _source_metadata(row)
                    if include_payload:
                        snapshot, locator, metadata = memory._decrypt_version(
                            conn, tenant_id, str(row["source_id"]), row
                        )
                        item["snapshot"] = snapshot
                        item["external_ref"] = locator
                        item["metadata"] = metadata
                    results.append(item)
                rows = results
    if pending_error is not None:
        raise pending_error
    return rows if rows is not None else []


def get_authorized_claim(
    actor: Actor,
    claim_id: object,
    *,
    include_content: bool = False,
    store: PersonalMemoryStore | None = None,
) -> dict[str, object]:
    memory = _memory(store)
    pending_error: Exception | None = None
    result: dict[str, object] | None = None
    with memory._tx() as conn:
        tenant_id = memory._ensure_principal_on(conn, actor)
        row = None
        newest = None
        if isinstance(claim_id, str) and claim_id:
            row = memory._lookup_claim(conn, tenant_id, claim_id)
            if row is not None and row["deleted_at"] is None:
                newest = memory._newest_claim_version(conn, tenant_id, str(row["id"]))
        now = utcnow_iso()
        if (
            row is None
            or newest is None
            or str(newest["state"]) == MemoryState.DELETED
            or str(newest["sensitivity"]) == "restricted"
            or (
                newest["expires_at"] is not None
                and str(newest["expires_at"]) <= now
            )
        ):
            _deny(
                memory,
                conn,
                tenant_id=tenant_id,
                action="claim.query",
                target_kind="claim",
            )
            pending_error = NotFoundError(_CLAIM_NOT_FOUND)
        else:
            result = _claim_metadata(
                {
                    "claim_id": row["id"],
                    "claim_version_id": newest["id"],
                    "version": newest["version"],
                    "claim_kind": row["claim_kind"],
                    "state": newest["state"],
                    "sensitivity": newest["sensitivity"],
                    "confidence": newest["confidence"],
                    "valid_from": newest["valid_from"],
                    "valid_to": newest["valid_to"],
                    "expires_at": newest["expires_at"],
                    "created_at": newest["created_at"],
                }
            )
            if include_content:
                content, explanation = memory._decrypt_claim_fields(
                    conn,
                    tenant_id=tenant_id,
                    claim_id=str(row["id"]),
                    version_row=newest,
                )
                result["content"] = content
                result["explanation"] = explanation
    if pending_error is not None:
        raise pending_error
    if result is None:
        raise NotFoundError(_CLAIM_NOT_FOUND)
    return result


def get_authorized_source(
    actor: Actor,
    source_id: object,
    *,
    include_payload: bool = False,
    store: PersonalMemoryStore | None = None,
) -> dict[str, object]:
    memory = _memory(store)
    pending_error: Exception | None = None
    result: dict[str, object] | None = None
    with memory._tx() as conn:
        tenant_id = memory._ensure_principal_on(conn, actor)
        row = None
        newest = None
        if isinstance(source_id, str) and source_id:
            row = memory._visible_source(conn, tenant_id, source_id)
            if row is not None:
                newest = memory._newest_active_version(conn, tenant_id, str(row["id"]))
        if row is None or newest is None:
            _deny(
                memory,
                conn,
                tenant_id=tenant_id,
                action="source.query",
                target_kind="source",
            )
            pending_error = NotFoundError(_SOURCE_NOT_FOUND)
        else:
            result = _source_metadata(
                {
                    "source_id": row["id"],
                    "source_version_id": newest["id"],
                    "version": newest["version"],
                    "source_kind": row["source_kind"],
                    "content_hash": newest["content_hash"],
                    "observed_at": newest["observed_at"],
                    "retention_class": newest["retention_class"],
                    "created_at": row["created_at"],
                    "deletion_state": newest["deletion_state"],
                }
            )
            if include_payload:
                snapshot, locator, metadata = memory._decrypt_version(
                    conn, tenant_id, str(row["id"]), newest
                )
                result["snapshot"] = snapshot
                result["external_ref"] = locator
                result["metadata"] = metadata
    if pending_error is not None:
        raise pending_error
    if result is None:
        raise NotFoundError(_SOURCE_NOT_FOUND)
    return result

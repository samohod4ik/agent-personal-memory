"""Owner-facing claim history: version trail plus accepted-link traversal."""

from __future__ import annotations

import sqlite3
from typing import Any

from .lifecycle import MemoryState
from .ranking import rank_claims
from .store import NotFoundError, PersonalMemoryStore, utcnow_iso
from .types import Actor
from .validation import ValidationError

_CLAIM_NOT_FOUND = "Claim not found"
_HISTORY_STATES = ["candidate", "confirmed", "superseded", "retracted"]
_MAX_HOPS = 20
_MAX_CURRENT_STEPS = 20


def _visible_claim(
    memory: PersonalMemoryStore,
    conn: sqlite3.Connection,
    tenant_id: str,
    claim_id: str,
) -> sqlite3.Row | None:
    """Return the newest version row when the claim is visible to the
    owner under ``explain_claim``'s rule (missing/foreign/deleted claim
    row, missing newest, deleted/restricted/expired newest -> None)."""
    row = memory._lookup_claim(conn, tenant_id, claim_id)
    if row is None or row["deleted_at"] is not None:
        return None
    newest = memory._newest_claim_version(conn, tenant_id, str(row["id"]))
    now = utcnow_iso()
    if (
        newest is None
        or str(newest["state"]) == MemoryState.DELETED
        or str(newest["sensitivity"]) == "restricted"
        or (
            newest["expires_at"] is not None
            and str(newest["expires_at"]) <= now
        )
    ):
        return None
    return newest


def _component_claims(
    memory: PersonalMemoryStore,
    conn: sqlite3.Connection,
    tenant_id: str,
    root_claim_id: str,
) -> set[str]:
    """BFS over accepted links in both directions, at most ``_MAX_HOPS``
    from the root. An invisible linked claim is neither included nor
    traversed through; the visited set makes cycles safe."""
    included = {root_claim_id}
    visited = {root_claim_id}
    frontier = [(root_claim_id, 0)]
    while frontier:
        claim_id, depth = frontier.pop(0)
        if depth >= _MAX_HOPS:
            continue
        links = conn.execute(
            "SELECT from_claim_id, to_claim_id FROM memory_claim_links "
            "WHERE tenant_id = ? AND status = 'accepted' "
            "AND (from_claim_id = ? OR to_claim_id = ?)",
            (tenant_id, claim_id, claim_id),
        ).fetchall()
        for link in links:
            other = str(link["from_claim_id"])
            if other == claim_id:
                other = str(link["to_claim_id"])
            if other in visited:
                continue
            visited.add(other)
            if _visible_claim(memory, conn, tenant_id, other) is None:
                continue
            included.add(other)
            frontier.append((other, depth + 1))
    return included


def _accepted_links_to(
    conn: sqlite3.Connection,
    tenant_id: str,
    included: set[str],
) -> dict[str, list[sqlite3.Row]]:
    """Accepted links pointing into each included claim, keyed by the
    superseded (``to_claim_id``) side. A link counts only when its
    ``from_claim_id`` is also in the visible component."""
    links: dict[str, list[sqlite3.Row]] = {}
    for claim_id in included:
        rows = conn.execute(
            "SELECT id, from_claim_id, to_claim_id, to_version, resolved_at "
            "FROM memory_claim_links "
            "WHERE tenant_id = ? AND status = 'accepted' AND to_claim_id = ?",
            (tenant_id, claim_id),
        ).fetchall()
        visible = [
            row for row in rows if str(row["from_claim_id"]) in included
        ]
        if visible:
            links[claim_id] = visible
    return links


def _claim_entries(
    memory: PersonalMemoryStore,
    conn: sqlite3.Connection,
    tenant_id: str,
    claim_id: str,
    links_to: dict[str, list[sqlite3.Row]],
    *,
    include_content: bool,
) -> list[dict[str, Any]]:
    versions = conn.execute(
        "SELECT id, version, state, content_enc, explanation_enc, "
        "created_at, change_reason "
        "FROM memory_claim_versions "
        "WHERE claim_id = ? AND tenant_id = ? ORDER BY version",
        (claim_id, tenant_id),
    ).fetchall()
    entries: list[dict[str, Any]] = []
    for version_row in versions:
        version = int(version_row["version"])
        reason, inferred = memory._version_reason(
            conn, tenant_id, claim_id, version_row
        )
        linked_claim_id = None
        for link in links_to.get(claim_id, []):
            if version == int(link["to_version"]) + 1:
                linked_claim_id = str(link["from_claim_id"])
                break
        entry: dict[str, Any] = {
            "claim_id": claim_id,
            "version": version,
            "at": str(version_row["created_at"]),
            "state": str(version_row["state"]),
            "reason": reason,
            "inferred": inferred,
            "linked_claim_id": linked_claim_id,
            "promoted_at": None,
        }
        if include_content:
            content = ""
            decrypted = memory._try_decrypt_claim_version(
                conn, tenant_id, claim_id, version_row
            )
            if decrypted is not None:
                try:
                    content = decrypted[0].decode("utf-8")
                except UnicodeDecodeError:
                    content = ""
            entry["content"] = content
        entries.append(entry)
    return entries


def _collapse_promoted(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop a ``promoted`` entry whose previous same-claim version is
    ``created``; the created entry keeps the promotion timestamp in
    ``promoted_at``."""
    by_key = {
        (str(item["claim_id"]), int(item["version"])): item
        for item in entries
    }
    kept: list[dict[str, Any]] = []
    for item in entries:
        previous = by_key.get(
            (str(item["claim_id"]), int(item["version"]) - 1)
        )
        if (
            item["reason"] == "promoted"
            and previous is not None
            and previous["reason"] == "created"
        ):
            previous["promoted_at"] = item["at"]
            continue
        kept.append(item)
    return kept


def _current_claim_id(
    conn: sqlite3.Connection,
    tenant_id: str,
    root_claim_id: str,
    included: set[str],
) -> str:
    """Walk supersedes chains forward: while an accepted link names the
    current claim as its ``to_claim_id`` and the ``from_claim_id`` is in
    the component, move to it (latest ``resolved_at``, then ``id``);
    stop after ``_MAX_CURRENT_STEPS`` or on revisit."""
    current = root_claim_id
    seen = {root_claim_id}
    for _ in range(_MAX_CURRENT_STEPS):
        rows = conn.execute(
            "SELECT id, from_claim_id FROM memory_claim_links "
            "WHERE tenant_id = ? AND status = 'accepted' "
            "AND to_claim_id = ? "
            "ORDER BY resolved_at DESC, id DESC",
            (tenant_id, current),
        ).fetchall()
        following = None
        for row in rows:
            candidate = str(row["from_claim_id"])
            if candidate in included:
                following = candidate
                break
        if following is None or following in seen:
            break
        seen.add(following)
        current = following
    return current


def history_entries_on(
    memory: PersonalMemoryStore,
    conn: sqlite3.Connection,
    tenant_id: str,
    root_claim_id: str,
    *,
    collapse_promoted: bool,
    include_content: bool = True,
) -> dict[str, Any]:
    """History for the accepted-link component around ``root_claim_id``.

    Caller guarantees the root claim is visible; this helper runs inside
    the caller's transaction so ``store.explain_claim`` can reuse it.
    With ``include_content=False`` entries carry no ``content`` key and
    nothing is decrypted (the explain safe-provenance contract).
    """
    included = _component_claims(memory, conn, tenant_id, root_claim_id)
    links_to = _accepted_links_to(conn, tenant_id, included)
    entries: list[dict[str, Any]] = []
    for claim_id in sorted(included):
        entries.extend(
            _claim_entries(
                memory,
                conn,
                tenant_id,
                claim_id,
                links_to,
                include_content=include_content,
            )
        )
    if collapse_promoted:
        entries = _collapse_promoted(entries)
    entries.sort(
        key=lambda item: (
            str(item["at"]),
            str(item["claim_id"]),
            int(item["version"]),
        )
    )
    return {
        "claim_id": root_claim_id,
        "current_claim_id": _current_claim_id(
            conn, tenant_id, root_claim_id, included
        ),
        "entries": entries,
    }


def personal_memory_history(
    actor: Actor,
    claim_id: str | None = None,
    *,
    query: str | None = None,
    collapse_promoted: bool = True,
    store: PersonalMemoryStore,
) -> dict[str, Any]:
    """Chronological owner history for one claim and its accepted-link
    component. Exactly one of ``claim_id``/``query`` selects the root;
    ``query`` resolves to the first ``rank_claims`` hit over the
    candidate/confirmed/superseded/retracted states."""
    if claim_id is not None and (
        not isinstance(claim_id, str) or not claim_id
    ):
        raise ValidationError("claim_id: must be a non-empty string")
    if query is not None and (not isinstance(query, str) or not query):
        raise ValidationError("query: must be a non-empty string")
    if (claim_id is None) == (query is None):
        raise ValidationError(
            "history: exactly one of claim_id or query is required"
        )
    if not isinstance(collapse_promoted, bool):
        raise ValidationError("collapse_promoted: must be a bool")
    memory = store
    if query is not None:
        ranked = rank_claims(
            actor,
            query,
            states=_HISTORY_STATES,
            store=memory,
        )
        if not ranked:
            raise NotFoundError(_CLAIM_NOT_FOUND)
        claim_id = str(ranked[0]["claim_id"])
    root_id = str(claim_id)
    pending_error: Exception | None = None
    result: dict[str, Any] | None = None
    with memory._tx() as conn:
        tenant_id = memory._ensure_principal_on(conn, actor)
        newest = _visible_claim(memory, conn, tenant_id, root_id)
        if newest is None:
            memory._deny_claim(
                conn, tenant_id=tenant_id, action="claim.history"
            )
            pending_error = NotFoundError(_CLAIM_NOT_FOUND)
        else:
            result = history_entries_on(
                memory,
                conn,
                tenant_id,
                root_id,
                collapse_promoted=collapse_promoted,
            )
            memory._append_audit(
                conn,
                tenant_id=tenant_id,
                actor_id=tenant_id,
                action="claim.history",
                target_kind="claim",
                target_id=root_id,
                outcome="explained",
                policy_reason="",
            )
    if pending_error is not None:
        raise pending_error
    if result is None:
        raise NotFoundError(_CLAIM_NOT_FOUND)
    return result

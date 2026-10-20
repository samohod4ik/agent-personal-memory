"""Slot-rule conflict judge and replacement proposals (issue #2).

A replacement is modeled as two separate claims plus an explicit
``memory_claim_links`` row (``relation='supersedes'``) that stays
``pending`` until the owner resolves it. The rule judge is deliberately
conservative: it only links claims that share a small closed set of
personal slots (home/name/name_style) with disjoint values, and prefers
a false negative over a false replacement.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Protocol

from .lifecycle import MemoryState
from .query import query_claims
from .store import NotFoundError, PersonalMemoryStore
from .text_norm import tokenize
from .types import Actor


class Relation(str, Enum):
    CONTRADICTS = "contradicts"
    REFINES = "refines"
    UNRELATED = "unrelated"
    TEMPORARY = "temporary"


class ConflictJudge(Protocol):
    judge_id: str

    def judge(self, new_text: str, old_text: str) -> Relation: ...


_CLAUSE_RE = re.compile(r"[,;:.!?—\n]+")

_SLOT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("home", re.compile(r"\bя\s+живу\s+(?:в|во)\s+(.+)$")),
    ("home", re.compile(r"\bя\s+переехал[а]?\s+(?:в|во)\s+(.+)$")),
    ("home", re.compile(r"\bмы\s+переехали\s+(?:в|во)\s+(.+)$")),
    ("home", re.compile(r"\bi\s+live\s+in\s+(.+)$")),
    ("home", re.compile(r"\bi\s+moved\s+to\s+(.+)$")),
    ("home", re.compile(r"\bi\s+relocated\s+to\s+(.+)$")),
    ("name", re.compile(r"\bменя\s+зовут\s+(.+)$")),
    ("name", re.compile(r"\bmy\s+name\s+is\s+(.+)$")),
    ("name_style", re.compile(r"\bназывай\s+меня\s+(.+)$")),
    ("name_style", re.compile(r"\bcall\s+me\s+(.+)$")),
)

_NEGATION_RE = re.compile(
    r"\b(?:больше\s+не|не\s+живу|no\s+longer|don'?t\s+live|"
    r"do\s+not\s+live|not\s+living)\b"
)

_TEMPORARY_RE = re.compile(
    r"\b(?:сегодня|завтра|сейчас|пока|временно|на\s+этой\s+неделе|"
    r"на\s+выходных|в\s+отпуске|в\s+командировке|today|tomorrow|"
    r"this\s+week|right\s+now|currently|for\s+now|temporarily|"
    r"on\s+vacation|on\s+a\s+business\s+trip)\b"
)


def _normalize(text: str) -> str:
    return text.casefold().replace("ё", "е")


def slot_of(text: str) -> tuple[str, frozenset[str]] | None:
    """Return ``(slot_name, value_stems)`` for the first slot-bearing
    clause, or ``None``. Negated clauses never contribute a value."""
    normalized = _normalize(text)
    for clause in _CLAUSE_RE.split(normalized):
        clause = clause.strip()
        if not clause or _NEGATION_RE.search(clause):
            continue
        for slot, pattern in _SLOT_PATTERNS:
            match = pattern.search(clause)
            if match is not None:
                value = frozenset(
                    token.stem for token in tokenize(match.group(1))[:3]
                )
                if value:
                    return (slot, value)
    return None


class SlotRuleJudge:
    judge_id = "slot_rule.v1"

    @staticmethod
    def slot_of(text: str) -> tuple[str, frozenset[str]] | None:
        return slot_of(text)

    def judge(self, new_text: str, old_text: str) -> Relation:
        if _TEMPORARY_RE.search(_normalize(new_text)):
            return Relation.TEMPORARY
        new_slot = slot_of(new_text)
        old_slot = slot_of(old_text)
        if new_slot is None or old_slot is None or new_slot[0] != old_slot[0]:
            return Relation.UNRELATED
        if new_slot[1].isdisjoint(old_slot[1]):
            return Relation.CONTRADICTS
        return Relation.REFINES


DEFAULT_JUDGE = SlotRuleJudge()

_CANDIDATE_BUDGET = 5


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return ""


def _content_stems(text: str) -> set[str]:
    return {token.stem for token in tokenize(text) if not token.stop}


def _result_item(link: dict[str, object], new_claim_id: str, old_claim_id: str) -> dict[str, object]:
    return {
        "link_id": link["link_id"],
        "new_claim_id": new_claim_id,
        "old_claim_id": old_claim_id,
        "relation": link["relation"],
        "status": link["status"],
        "origin": link["origin"],
    }


def propose_reconciliations(
    actor: Actor,
    claim_id: str,
    *,
    judge: ConflictJudge | None = None,
    store: PersonalMemoryStore,
) -> list[dict[str, object]]:
    """Judge the new claim against up to five confirmed claims of the
    same tenant; for CONTRADICTS create a pending supersedes link
    (new -> old). Only the judge verdict creates links."""
    memory = store
    arbiter: ConflictJudge = judge if judge is not None else DEFAULT_JUDGE
    rows = query_claims(
        actor, include_content=True, limit=500, store=memory
    )
    new_row = next(
        (row for row in rows if str(row["claim_id"]) == claim_id), None
    )
    if new_row is None:
        raise NotFoundError("Claim not found")
    new_text = _as_text(new_row.get("content"))
    new_slot = slot_of(new_text)
    new_stems = _content_stems(new_text)
    confirmed = [
        row
        for row in rows
        if str(row["state"]) == MemoryState.CONFIRMED.value
        and str(row["claim_id"]) != claim_id
    ]
    if new_slot is not None:
        same_slot = [
            row
            for row in confirmed
            if (other := slot_of(_as_text(row.get("content")))) is not None
            and other[0] == new_slot[0]
        ]
    else:
        same_slot = []
    same_ids = {str(row["claim_id"]) for row in same_slot}
    scored_rest: list[tuple[int, dict[str, object]]] = []
    for row in confirmed:
        if str(row["claim_id"]) in same_ids:
            continue
        overlap = len(new_stems & _content_stems(_as_text(row.get("content"))))
        if overlap > 0:
            scored_rest.append((-overlap, row))
    scored_rest.sort(key=lambda item: item[0])
    selected = (same_slot + [row for _, row in scored_rest])[:_CANDIDATE_BUDGET]
    results: list[dict[str, object]] = []
    for row in selected:
        old_text = _as_text(row.get("content"))
        if arbiter.judge(new_text, old_text) != Relation.CONTRADICTS:
            continue
        link = memory.create_claim_link(
            actor,
            from_claim_id=claim_id,
            to_claim_id=str(row["claim_id"]),
            origin=arbiter.judge_id,
        )
        results.append(_result_item(link, claim_id, str(row["claim_id"])))
    return results


def propose_reconciliation(
    actor: Actor,
    new_claim_id: str,
    old_claim_id: str,
    *,
    origin: str = "manual",
    store: PersonalMemoryStore,
) -> dict[str, object]:
    """Manual replacement proposal: link new -> old without a judge."""
    link = store.create_claim_link(
        actor,
        from_claim_id=new_claim_id,
        to_claim_id=old_claim_id,
        origin=origin,
    )
    return _result_item(link, new_claim_id, old_claim_id)

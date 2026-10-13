"""Deterministic durable-insight extraction from a user turn."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .validation import (
    CLAIM_KINDS,
    CREDENTIAL_MARKERS,
    ID_DOC_MARKERS,
    PAYMENT_MARKERS,
    THIRD_PARTY_MARKERS,
    ValidationError,
    validate_claim_content,
)

TURN_BUDGET = 3
_INSIGHT_MIN = 8
_INSIGHT_MAX = 240

_SECRET_TERMS = re.compile(
    r"(?i)\b("
    r"password|passwd|passphrase|пароль|secret|secrets|token|tokens|"
    r"api[_\s-]?key|private[_\s-]?key|credential|otp|pin|cvv|cvc|"
    r"passport|паспорт|ssn|credit\s*card|номер\s+карты"
    r")\b"
)
_TRAVEL_SEARCH = re.compile(
    r"(?i)\b("
    r"найди|поищи|search|find tickets|find hotels|билеты в|отели в"
    r")\b"
)
_REMEMBER_CUE = re.compile(
    r"(?i)^(?:please\s+)?(?:"
    r"remember(?:\s+that)?|"
    r"don't forget(?:\s+that)?|"
    r"запомни(?:те)?|"
    r"запомнить"
    r")\s*(?:that|this|это|что|:)?\s*"
)
_QUESTION = re.compile(r"^\s*.+\?\s*$")
_GENERIC = frozenset(
    {"it", "this", "that", "them", "here", "there", "это", "то", "так"}
)

_PATTERNS: tuple[tuple[re.Pattern[str], str, float, str], ...] = (
    (re.compile(r"(?i)\bI prefer\s+(.+)$"), "preference", 0.86, "stated preference"),
    (re.compile(r"(?i)\bI (?:don't|do not) like\s+(.+)$"), "preference", 0.84, "stated dislike"),
    (re.compile(r"(?i)\bI (?:don't|do not) eat\s+(.+)$"), "preference", 0.86, "stated diet"),
    (re.compile(r"(?i)\bI like\s+(.+)$"), "preference", 0.80, "stated like"),
    (re.compile(r"(?i)\bcall me\s+(.+)$"), "preference", 0.88, "stated name style"),
    (re.compile(r"(?i)\bmy name is\s+(.+)$"), "confirmed_fact", 0.88, "stated name"),
    (re.compile(r"(?i)\bI live in\s+(.+)$"), "confirmed_fact", 0.86, "stated home"),
    (re.compile(r"(?i)\bI decided(?:\s+to)?\s+(.+)$"), "decision", 0.82, "stated decision"),
    (re.compile(r"(?i)\bя предпочитаю\s+(.+)$"), "preference", 0.86, "stated preference"),
    (re.compile(r"(?i)\bя не люблю\s+(.+)$"), "preference", 0.84, "stated dislike"),
    (re.compile(r"(?i)\bя люблю\s+(.+)$"), "preference", 0.80, "stated like"),
    (re.compile(r"(?i)\bя не ем\s+(.+)$"), "preference", 0.86, "stated diet"),
    (re.compile(r"(?i)\bназывай меня\s+(.+)$"), "preference", 0.88, "stated name style"),
    (re.compile(r"(?i)\bменя зовут\s+(.+)$"), "confirmed_fact", 0.88, "stated name"),
    (re.compile(r"(?i)\bя живу(?:\s+в)?\s+(.+)$"), "confirmed_fact", 0.86, "stated home"),
    (re.compile(r"(?i)\bя решил(?:а)?\s+(.+)$"), "decision", 0.82, "stated decision"),
)


@dataclass(frozen=True)
class ExtractedInsight:
    claim_kind: str
    content: str
    confidence: float
    explanation: str


def normalize_insight_text(value: str) -> str:
    cleaned = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value)
    collapsed = " ".join(cleaned.lower().split())
    return collapsed.strip(" .,:;!-")


def turn_has_blocked_secret(text: str) -> bool:
    if not text or not text.strip():
        return False
    if _SECRET_TERMS.search(text):
        return True
    if CREDENTIAL_MARKERS.search(text):
        return True
    if PAYMENT_MARKERS.search(text):
        return True
    if ID_DOC_MARKERS.search(text):
        return True
    if THIRD_PARTY_MARKERS.search(text):
        return True
    return False


def insight_content_allowed(text: str) -> bool:
    if turn_has_blocked_secret(text):
        return False
    try:
        validate_claim_content(text.encode("utf-8"))
    except (ValidationError, UnicodeEncodeError):
        return False
    return True


def _split_clauses(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?;])\s+|\n+", text.strip())
    return [part.strip() for part in parts if part.strip()]


def _clean_content(value: str) -> str:
    return " ".join(value.strip().strip(" .,:;").split())


def _usable_content(value: str) -> str | None:
    content = _clean_content(value)
    if not _INSIGHT_MIN <= len(content) <= _INSIGHT_MAX:
        return None
    if _QUESTION.match(content):
        return None
    if normalize_insight_text(content) in _GENERIC:
        return None
    if not insight_content_allowed(content):
        return None
    return content


def _match_patterns(clause: str) -> list[ExtractedInsight]:
    found: list[ExtractedInsight] = []
    for pattern, kind, confidence, explanation in _PATTERNS:
        match = pattern.search(clause)
        if match is None:
            continue
        content = _usable_content(match.group(0))
        if content is None:
            continue
        found.append(
            ExtractedInsight(
                claim_kind=kind,
                content=content,
                confidence=confidence,
                explanation=explanation,
            )
        )
    return found


def extract_insights(text: str) -> list[ExtractedInsight]:
    raw = (text or "").strip()
    if not raw or turn_has_blocked_secret(raw):
        return []
    if _TRAVEL_SEARCH.search(raw) and _REMEMBER_CUE.search(raw) is None:
        return []

    remember = _REMEMBER_CUE.match(raw)
    remainder = raw[remember.end() :].strip() if remember else raw
    if remember is None and _QUESTION.match(raw):
        return []
    if not remainder:
        return []

    collected: list[ExtractedInsight] = []
    seen: set[str] = set()

    def _add(item: ExtractedInsight) -> None:
        key = normalize_insight_text(item.content)
        if not key or key in seen or item.claim_kind not in CLAIM_KINDS:
            return
        seen.add(key)
        collected.append(item)

    clauses = _split_clauses(remainder)
    if remember and remainder and remainder not in clauses:
        clauses.insert(0, remainder)
    for clause in clauses:
        if _QUESTION.match(clause):
            continue
        for item in _match_patterns(clause):
            _add(item)

    if remember and not collected:
        leftover = _usable_content(remainder)
        if leftover is not None:
            _add(
                ExtractedInsight(
                    claim_kind="confirmed_fact",
                    content=leftover,
                    confidence=0.80,
                    explanation="explicit remember",
                )
            )

    collected.sort(key=lambda item: item.confidence, reverse=True)
    return collected[:TURN_BUDGET]

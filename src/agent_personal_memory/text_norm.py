"""Deterministic ru/en token normalization: casefold, ё-fold, Snowball stems."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

import snowballstemmer

NORMALIZER_VERSION: int = 1

_TOKEN_RE = re.compile(r"[\w\u0400-\u04FF]+", re.UNICODE)
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?")

RU_STOP_WORDS = frozenset(
    {
        "я", "мне", "меня", "мой", "моя",
        "у", "в", "во", "на", "с", "и", "а",
        "что", "как", "какой", "какая", "какие",
        "где", "когда", "это",
    }
)
EN_STOP_WORDS = frozenset(
    {
        "i", "me", "my", "the", "a", "an",
        "in", "on", "at", "to", "of", "is",
        "what", "where", "when", "which",
    }
)
STOP_WORDS = RU_STOP_WORDS | EN_STOP_WORDS

_STEMMERS = threading.local()


@dataclass(frozen=True)
class Token:
    raw: str
    stem: str
    protected: bool
    stop: bool


def _stemmer(name: str):
    cached = getattr(_STEMMERS, "cached", None)
    if cached is None:
        cached = {}
        _STEMMERS.cached = cached
    stemmer = cached.get(name)
    if stemmer is None:
        stemmer = snowballstemmer.stemmer(name)
        cached[name] = stemmer
    return stemmer


def _normalize(value: str) -> str:
    return value.casefold().replace("ё", "е")


def _has_cyrillic(value: str) -> bool:
    return any("\u0400" <= char <= "\u04FF" for char in value)


def _has_latin(value: str) -> bool:
    return any("a" <= char <= "z" for char in value)


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans = [match.span() for match in _UUID_RE.finditer(text)]
    spans.extend(match.span() for match in _DATE_RE.finditer(text))
    return spans


def _is_protected(
    original: str, raw: str, span: tuple[int, int], spans: list[tuple[int, int]]
) -> bool:
    if any(char.isdigit() for char in raw):
        return True
    if _has_cyrillic(raw) and _has_latin(raw):
        return True
    if original.isupper() and len(original) <= 6:
        return True
    return any(start <= span[0] and span[1] <= end for start, end in spans)


def _stem(raw: str, protected: bool) -> str:
    if protected:
        return raw
    if _has_cyrillic(raw):
        return _stemmer("russian").stemWord(raw)
    if _has_latin(raw):
        return _stemmer("english").stemWord(raw)
    return raw


def tokenize(text: str) -> list[Token]:
    spans = _protected_spans(text)
    tokens: list[Token] = []
    for match in _TOKEN_RE.finditer(text):
        original = match.group(0)
        raw = _normalize(original)
        if len(raw) < 2:
            continue
        protected = _is_protected(original, raw, match.span(), spans)
        tokens.append(
            Token(
                raw=raw,
                stem=_stem(raw, protected),
                protected=protected,
                stop=raw in STOP_WORDS,
            )
        )
    return tokens


def query_terms(text: str) -> list[Token]:
    seen: set[str] = set()
    terms: list[Token] = []
    for token in tokenize(text):
        if token.stem in seen:
            continue
        seen.add(token.stem)
        terms.append(token)
    content_terms = [term for term in terms if not term.stop]
    return content_terms if content_terms else terms


def stem_text(text: str) -> str:
    return " ".join(token.stem for token in tokenize(text))

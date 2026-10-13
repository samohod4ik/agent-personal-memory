"""Closed-vocabulary validation for personal-memory sources and claims."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .lifecycle import MemoryState

SOURCE_KIND_VOCAB_VERSION = 2
SOURCE_KINDS = frozenset(
    {
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
    }
)
AUTO_INSIGHT_SOURCE_KIND = "auto_insight_turn"

MEMORY_MODE_AUTO_INSIGHT = "auto_insight"
MEMORY_MODE_CONFIRM_REQUIRED = "confirm_required"
MEMORY_MODES = frozenset({MEMORY_MODE_AUTO_INSIGHT, MEMORY_MODE_CONFIRM_REQUIRED})
DEFAULT_MEMORY_MODE = MEMORY_MODE_AUTO_INSIGHT

RETENTION_CLASS_VOCAB_VERSION = 1
RETENTION_CLASSES = frozenset({"ephemeral", "standard", "durable"})

CLAIM_KIND_VOCAB_VERSION = 1
CLAIM_KINDS = frozenset(
    {
        "raw_event",
        "candidate_insight",
        "confirmed_fact",
        "preference",
        "task",
        "decision",
        "summary",
    }
)

SENSITIVITY_VOCAB_VERSION = 1
SENSITIVITY_CLASSES = frozenset({"normal", "sensitive", "restricted"})

RELIABLE_SOURCE_KINDS = frozenset(
    {
        "calendar_event",
        "travel_record",
        "email_message",
        "tool_result",
        "document",
    }
)

SECRET_KEY_SEGMENTS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "passphrase",
        "secret",
        "secrets",
        "token",
        "tokens",
        "key",
        "keys",
        "privatekey",
        "apikey",
        "credential",
        "credentials",
        "cred",
        "creds",
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "session",
        "otp",
        "pin",
        "cvv",
        "cvc",
        "mnemonic",
        "seed",
        "signature",
        "sig",
        "certificate",
        "refresh",
    }
)

CREDENTIAL_MARKERS = re.compile(
    r"(-----BEGIN )"
    r"|(\bBearer\s+[A-Za-z0-9._\-]{8,})"
    r"|(\b(?:sk-|pk_live_|ghp_|gho_|ghs_|github_pat_|glpat-|AKIA|ASIA|AIza|ya29\.|xox[baprs]-)[A-Za-z0-9._]{8,})"
    r"|([A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
OPAQUE_CREDENTIAL_RUN = re.compile(
    r"^(?=[^\s]*[a-z])(?=[^\s]*[A-Z])(?=[^\s]*[0-9])[A-Za-z0-9+/=_-]{40,}$"
)
PAYMENT_MARKERS = re.compile(r"(?:\d[ -]*?){13,19}")
ID_DOC_MARKERS = re.compile(
    r"(?:\b\d{3}-\d{2}-\d{4}\b)|(?:\bpassport\s*(?:no|number|#)\b)",
    re.IGNORECASE,
)
THIRD_PARTY_MARKERS = re.compile(
    r"another person's|someone else's",
    re.IGNORECASE,
)

_METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_OBSERVED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_JSON_INT_LIMIT = 2**53
_SNAPSHOT_MAX = 65536
_EXPLANATION_MAX = 4096
_EXTERNAL_REF_MAX = 512
_METADATA_MAX_KEYS = 20
_METADATA_STR_MAX = 256
_METADATA_JSON_MAX = 4096
_OBSERVED_AT_MIN = datetime(2000, 1, 1, tzinfo=timezone.utc)
_FUTURE_SLACK = timedelta(seconds=60)


class ValidationError(ValueError):
    """Raised when a source field fails closed-vocabulary validation."""


def content_hash_for(snapshot: bytes) -> str:
    return hashlib.sha256(snapshot).hexdigest()


def canonicalize_metadata(metadata: Mapping[str, Any] | None) -> bytes:
    payload: Mapping[str, Any] = {} if metadata is None else metadata
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _fail(field: str, rule: str) -> None:
    raise ValidationError(f"{field}: {rule}")


def _has_controls(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def validate_source_kind(value: object) -> str:
    if not isinstance(value, str) or value not in SOURCE_KINDS:
        _fail("source_kind", "unknown value")
    return value


def validate_memory_mode(value: object) -> str:
    if not isinstance(value, str) or value not in MEMORY_MODES:
        _fail("memory_mode", "unknown value")
    return value


def confirmation_source_is_reliable(source_kind: str, memory_mode: str) -> bool:
    """ADR 0007 reliable kinds, plus auto_insight_turn only in auto_insight mode."""
    if source_kind in RELIABLE_SOURCE_KINDS:
        return True
    return (
        source_kind == AUTO_INSIGHT_SOURCE_KIND
        and memory_mode == MEMORY_MODE_AUTO_INSIGHT
    )


def validate_retention_class(value: object) -> str:
    if not isinstance(value, str) or value not in RETENTION_CLASSES:
        _fail("retention_class", "unknown value")
    return value


def validate_external_ref(value: object) -> str:
    if not isinstance(value, str):
        _fail("external_ref", "must be a string")
    if not 1 <= len(value) <= _EXTERNAL_REF_MAX:
        _fail("external_ref", "length must be 1-512")
    if _has_controls(value):
        _fail("external_ref", "control characters are not allowed")
    if CREDENTIAL_MARKERS.search(value):
        _fail("external_ref", "value looks like a credential")
    return value


def validate_snapshot(value: object) -> bytes:
    if not isinstance(value, (bytes, bytearray)):
        _fail("snapshot", "must be bytes")
    data = bytes(value)
    if not 1 <= len(data) <= _SNAPSHOT_MAX:
        _fail("snapshot", "length must be 1-65536")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    if CREDENTIAL_MARKERS.search(text):
        _fail("snapshot", "value looks like a credential")
    return data


def validate_observed_at(value: object) -> str:
    if not isinstance(value, str) or _OBSERVED_AT_RE.fullmatch(value) is None:
        _fail("observed_at", "must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        _fail("observed_at", "must be YYYY-MM-DDTHH:MM:SSZ")
    if parsed < _OBSERVED_AT_MIN:
        _fail("observed_at", "is before 2000-01-01T00:00:00Z")
    if parsed > datetime.now(timezone.utc) + _FUTURE_SLACK:
        _fail("observed_at", "is more than 60 seconds in the future")
    return value


def validate_metadata(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail("metadata", "must be an object")
    if len(value) > _METADATA_MAX_KEYS:
        _fail("metadata", "too many keys")
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _METADATA_KEY_RE.fullmatch(key) is None:
            _fail("metadata", "invalid key")
        if any(segment in SECRET_KEY_SEGMENTS for segment in key.split("_")):
            _fail(f"metadata.{key}", "secret-like key is not allowed")
        if item is None:
            cleaned[key] = None
            continue
        if isinstance(item, bool):
            cleaned[key] = item
            continue
        if isinstance(item, int):
            if abs(item) > _JSON_INT_LIMIT:
                _fail(f"metadata.{key}", "integer is out of range")
            cleaned[key] = item
            continue
        if isinstance(item, str):
            if len(item) > _METADATA_STR_MAX:
                _fail(f"metadata.{key}", "string is too long")
            if _has_controls(item):
                _fail(f"metadata.{key}", "control characters are not allowed")
            if CREDENTIAL_MARKERS.search(item) or OPAQUE_CREDENTIAL_RUN.match(item):
                _fail(f"metadata.{key}", "value looks like a credential")
            cleaned[key] = item
            continue
        _fail(f"metadata.{key}", "unsupported value type")
    if len(canonicalize_metadata(cleaned)) > _METADATA_JSON_MAX:
        _fail("metadata", "canonical JSON exceeds 4096 bytes")
    return cleaned


def validate_list_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
        _fail("limit", "must be between 1 and 500")
    return value


def validate_assembly_record_budget(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
        _fail("record_budget", "must be between 1 and 500")
    return value


def validate_assembly_token_budget(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32000:
        _fail("token_budget", "must be between 1 and 32000")
    return value


def validate_optional_trip_ref(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        _fail("trip_ref", "must be a non-empty string")
    if _has_controls(value):
        _fail("trip_ref", "control characters are not allowed")
    if len(value) > _EXTERNAL_REF_MAX:
        _fail("trip_ref", "length must be 1-512")
    return value


_RANK_QUERY_MAX = 512


def validate_rank_query_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("query_text", "must be a non-empty string")
    text = value.strip()
    if len(text) > _RANK_QUERY_MAX:
        _fail("query_text", "is too long")
    if _has_controls(text):
        _fail("query_text", "control characters are not allowed")
    return text


def validate_optional_rank_query_text(value: object) -> str | None:
    if value is None:
        return None
    return validate_rank_query_text(value)


def validate_assembly_session_items(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        _fail("session_items", "must be a list")
    cleaned: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _fail("session_items", "each item must be an object")
        role = item.get("role")
        text = item.get("text")
        if not isinstance(role, str) or not role:
            _fail(f"session_items[{index}].role", "must be a non-empty string")
        if not isinstance(text, str):
            _fail(f"session_items[{index}].text", "must be a string")
        if _has_controls(role) or _has_controls(text):
            _fail(f"session_items[{index}]", "control characters are not allowed")
        if len(text) > _SNAPSHOT_MAX:
            _fail(f"session_items[{index}].text", "is too long")
        observed = item.get("observed_at")
        if observed is not None:
            observed = validate_rfc3339_z(f"session_items[{index}].observed_at", observed)
        cleaned.append({"role": role, "text": text, "observed_at": observed})
    return cleaned


def validate_optional_source_kind(value: object) -> str | None:
    if value is None:
        return None
    return validate_source_kind(value)


def validate_register_fields(
    *,
    source_kind: object,
    external_ref: object,
    content_hash: object,
    snapshot: object,
    observed_at: object,
    retention_class: object,
    metadata: object = None,
) -> dict[str, Any]:
    kind = validate_source_kind(source_kind)
    ref = validate_external_ref(external_ref)
    snap = validate_snapshot(snapshot)
    if not isinstance(content_hash, str) or _CONTENT_HASH_RE.fullmatch(content_hash) is None:
        _fail("content_hash", "must be 64 lowercase hex characters")
    if content_hash != content_hash_for(snap):
        _fail("content_hash", "must equal sha256(snapshot)")
    observed = validate_observed_at(observed_at)
    retention = validate_retention_class(retention_class)
    cleaned = validate_metadata(metadata)
    return {
        "source_kind": kind,
        "external_ref": ref,
        "content_hash": content_hash,
        "snapshot": snap,
        "observed_at": observed,
        "retention_class": retention,
        "metadata": cleaned,
    }


def validate_claim_kind(value: object) -> str:
    if not isinstance(value, str) or value not in CLAIM_KINDS:
        _fail("claim_kind", "unknown value")
    return value


def validate_optional_claim_kind(value: object) -> str | None:
    if value is None:
        return None
    return validate_claim_kind(value)


def validate_sensitivity(value: object) -> str:
    if not isinstance(value, str) or value not in SENSITIVITY_CLASSES:
        _fail("sensitivity", "unknown value")
    return value


def validate_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("confidence", "must be a number between 0 and 1")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        _fail("confidence", "must be a number between 0 and 1")
    return number


def validate_rfc3339_z(field: str, value: object) -> str:
    if not isinstance(value, str) or _OBSERVED_AT_RE.fullmatch(value) is None:
        _fail(field, "must be YYYY-MM-DDTHH:MM:SSZ")
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return value


def validate_optional_rfc3339_z(field: str, value: object) -> str | None:
    if value is None:
        return None
    return validate_rfc3339_z(field, value)


def _validate_vocab_collection(
    field: str,
    value: object,
    allowed: frozenset[str],
) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple, set, frozenset)):
        _fail(field, "must be a list of known values")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, MemoryState):
            item = item.value
        if not isinstance(item, str) or item not in allowed:
            _fail(field, "unknown value")
        if item in seen:
            continue
        seen.add(item)
        cleaned.append(item)
    return cleaned


def validate_query_states(value: object) -> list[str] | None:
    return _validate_vocab_collection(
        "states",
        value,
        frozenset(item.value for item in MemoryState),
    )


def validate_query_claim_kinds(value: object) -> list[str] | None:
    return _validate_vocab_collection("claim_kinds", value, CLAIM_KINDS)


def validate_query_source_kinds(value: object) -> list[str] | None:
    return _validate_vocab_collection("source_kinds", value, SOURCE_KINDS)


def validate_query_time_window(
    start_field: str,
    start: object,
    end_field: str,
    end: object,
) -> tuple[str | None, str | None]:
    start_stamp = validate_optional_rfc3339_z(start_field, start)
    end_stamp = validate_optional_rfc3339_z(end_field, end)
    if start_stamp is not None and end_stamp is not None and start_stamp > end_stamp:
        _fail(end_field, f"must be on or after {start_field}")
    return start_stamp, end_stamp


def validate_expires_at(value: object) -> str:
    stamp = validate_rfc3339_z("expires_at", value)
    parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if parsed <= datetime.now(timezone.utc):
        _fail("expires_at", "must be in the future")
    return stamp


def validate_explanation(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        _fail("explanation", "must be a string")
    if len(value) > _EXPLANATION_MAX:
        _fail("explanation", "is too long")
    if _has_controls(value):
        _fail("explanation", "control characters are not allowed")
    if CREDENTIAL_MARKERS.search(value):
        _fail("explanation", "value looks like a credential")
    return value


def validate_claim_content(value: object) -> bytes:
    if not isinstance(value, (bytes, bytearray)):
        _fail("content", "must be bytes")
    data = bytes(value)
    if not 1 <= len(data) <= _SNAPSHOT_MAX:
        _fail("content", "length must be 1-65536")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    if CREDENTIAL_MARKERS.search(text):
        _fail("content", "value looks like a credential")
    if PAYMENT_MARKERS.search(text):
        _fail("content", "payment data is not allowed")
    if ID_DOC_MARKERS.search(text):
        _fail("content", "identity-document data is not allowed")
    if THIRD_PARTY_MARKERS.search(text):
        _fail("content", "third-party private data is not allowed")
    return data


def validate_source_ids(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        _fail("source_ids", "must be a non-empty list")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item:
            _fail("source_ids", "must contain source ids")
        if item in seen:
            continue
        seen.add(item)
        cleaned.append(item)
    return cleaned


def validate_valid_time_order(
    valid_from: object = None,
    valid_to: object = None,
) -> None:
    if valid_from is not None and valid_to is not None and valid_from > valid_to:
        _fail("valid_to", "must be on or after valid_from")


def validate_create_candidate_fields(
    *,
    claim_kind: object,
    content: object,
    confidence: object,
    explanation: object,
    sensitivity: object,
    source_ids: object,
    valid_from: object = None,
    valid_to: object = None,
    expires_at: object,
) -> dict[str, Any]:
    kind = validate_claim_kind(claim_kind)
    payload = validate_claim_content(content)
    score = validate_confidence(confidence)
    reason = validate_explanation(explanation)
    sensitivity_class = validate_sensitivity(sensitivity)
    sources = validate_source_ids(source_ids)
    start = validate_optional_rfc3339_z("valid_from", valid_from)
    end = validate_optional_rfc3339_z("valid_to", valid_to)
    validate_valid_time_order(start, end)
    expiry = validate_expires_at(expires_at)
    return {
        "claim_kind": kind,
        "content": payload,
        "confidence": score,
        "explanation": reason,
        "sensitivity": sensitivity_class,
        "source_ids": sources,
        "valid_from": start,
        "valid_to": end,
        "expires_at": expiry,
    }


def validate_correct_fields(
    *,
    content: object,
    explanation: object,
    source_ids: object,
    confidence: object = None,
    sensitivity: object = None,
    valid_from: object = None,
    valid_to: object = None,
    expires_at: object = None,
) -> dict[str, Any]:
    payload = validate_claim_content(content)
    reason = validate_explanation(explanation)
    sources = validate_source_ids(source_ids)
    score = None if confidence is None else validate_confidence(confidence)
    sensitivity_class = None if sensitivity is None else validate_sensitivity(sensitivity)
    start = validate_optional_rfc3339_z("valid_from", valid_from)
    end = validate_optional_rfc3339_z("valid_to", valid_to)
    validate_valid_time_order(start, end)
    expiry = None if expires_at is None else validate_expires_at(expires_at)
    return {
        "content": payload,
        "explanation": reason,
        "source_ids": sources,
        "confidence": score,
        "sensitivity": sensitivity_class,
        "valid_from": start,
        "valid_to": end,
        "expires_at": expiry,
    }


_EXTRACTION_TEXT_MAX = 8192
_SHADOW_CONFIDENCE_CAP = 0.6
_SHADOW_CLAIM_KINDS = frozenset({"candidate_insight", "raw_event"})


def validate_extraction_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("text", "must be a non-empty string")
    text = value.strip()
    if len(text) > _EXTRACTION_TEXT_MAX:
        _fail("text", "is too long")
    if _has_controls(text):
        _fail("text", "control characters are not allowed")
    return text


def validate_extraction_request(value: object) -> object:
    from .engines.types import ExtractionRequest

    if not isinstance(value, ExtractionRequest):
        _fail("request", "must be ExtractionRequest")
    text = validate_extraction_text(value.text)
    sources = validate_source_ids(list(value.source_ids))
    observed = None
    if value.observed_at is not None:
        observed = validate_rfc3339_z("observed_at", value.observed_at)
    return ExtractionRequest(
        actor=value.actor,
        text=text,
        source_ids=tuple(sources),
        observed_at=observed,
    )


def validate_retrieval_request(value: object) -> object:
    from .engines.types import RetrievalRequest

    if not isinstance(value, RetrievalRequest):
        _fail("request", "must be RetrievalRequest")
    query = validate_rank_query_text(value.query_text)
    limit = validate_list_limit(value.limit)
    return RetrievalRequest(actor=value.actor, query_text=query, limit=limit)


def validate_shadow_candidate_fields(
    *,
    claim_kind: object,
    content: object,
    confidence: object,
    explanation: object,
    sensitivity: object,
    source_ids: object,
    expires_at: object,
) -> dict[str, Any]:
    if not isinstance(claim_kind, str) or claim_kind not in _SHADOW_CLAIM_KINDS:
        _fail("claim_kind", "shadow extraction must use candidate kinds")
    score = validate_confidence(confidence)
    if score > _SHADOW_CONFIDENCE_CAP:
        _fail("confidence", f"must be <= {_SHADOW_CONFIDENCE_CAP} for shadow extraction")
    return validate_create_candidate_fields(
        claim_kind=claim_kind,
        content=content,
        confidence=score,
        explanation=explanation,
        sensitivity=sensitivity,
        source_ids=source_ids,
        expires_at=expires_at,
    )

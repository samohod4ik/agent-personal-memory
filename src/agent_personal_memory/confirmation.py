"""Two-phase inbound confirmation for destructive personal-memory deletion.

Model-facing calls may only create a non-authorizing preview request:
``create_delete_preview`` returns a ``request_id`` plus human-readable text,
never a nonce. Erasure happens only after a real user opens the canonical
preview over an authenticated messaging session and consumes a one-time
nonce.

Delivery is injected by the host as ``send(text) -> bool``. The nonce is
generated inside the registry, exists solely inside the send() payload text,
and is registered for consumption only after send() returns truthy
(``mark_preview_sent``). A failed or unverifiable send leaves no usable
nonce. Host-side inbound command parsing, authorization, session resolution
and transport stay outside this package.
"""

from __future__ import annotations

import inspect
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .locale import (
    format_claim_summary,
    format_mutation_confirmation,
    normalize_language,
)
from .query import get_authorized_claim
from .store import PersonalMemoryStore
from .types import Actor
from .validation import ValidationError, content_hash_for

MEMORY_DELETE_COMMAND = "memory-delete"
MEMORY_CONFIRM_COMMAND = "memory-confirm"
MEMORY_CANCEL_COMMAND = "memory-cancel"
TRUSTED_DELETE_COMMANDS = frozenset(
    {
        MEMORY_DELETE_COMMAND,
        MEMORY_CONFIRM_COMMAND,
        MEMORY_CANCEL_COMMAND,
    }
)

_INVALID_USER_IDS = frozenset({"", "0", "unknown", "none"})
_REQUEST_TTL_SECONDS = 600.0
_MAX_REGISTRY_ITEMS = 64
_TOKEN_BYTES = 18


class TrustedDeleteDenied(ValidationError):
    """Raised when a model-supplied or otherwise untrusted confirm is rejected."""


@dataclass(frozen=True)
class SessionBinding:
    platform: str
    user_id: str
    session_key: str
    session_id: str
    chat_id: str
    thread_id: str = ""
    profile: str = ""
    display_name: str = ""

    def actor(self) -> Actor:
        return Actor(
            platform=self.platform,
            user_id=self.user_id,
            display_name=self.display_name,
        )


@dataclass(frozen=True)
class BoundTarget:
    claim_id: str
    version: int
    claim_version_id: str
    content_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "version": self.version,
            "claim_version_id": self.claim_version_id,
            "content_hash": self.content_hash,
        }


@dataclass
class DeleteRequest:
    request_id: str
    action: str
    actor: Actor
    binding: SessionBinding
    targets: tuple[BoundTarget, ...]
    language: str
    created_mono: float
    expires_mono: float
    require_state: str | None = None
    preview_sent: bool = False
    nonce: str | None = None
    consumed: bool = False
    cancelled: bool = False
    items: list[dict[str, object]] = field(default_factory=list)


@dataclass
class ConfirmNonce:
    nonce: str
    request_id: str
    binding: SessionBinding
    created_mono: float
    expires_mono: float
    consumed: bool = False


class DeleteRegistry:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] | None = None,
        ttl_seconds: float = _REQUEST_TTL_SECONDS,
        max_items: int = _MAX_REGISTRY_ITEMS,
    ) -> None:
        self._lock = threading.Lock()
        self._monotonic = monotonic or time.monotonic
        self._ttl = float(ttl_seconds)
        self._max_items = int(max_items)
        self._requests: dict[str, DeleteRequest] = {}
        self._nonces: dict[str, ConfirmNonce] = {}

    def now(self) -> float:
        return float(self._monotonic())

    def reset(self) -> None:
        with self._lock:
            self._requests.clear()
            self._nonces.clear()

    def create_request(
        self,
        *,
        action: str,
        actor: Actor,
        binding: SessionBinding,
        targets: list[BoundTarget],
        language: str,
        require_state: str | None = None,
        items: list[dict[str, object]] | None = None,
    ) -> DeleteRequest:
        if not targets:
            raise ValidationError("request: no authorized targets to delete")
        if not _binding_complete(binding):
            raise ValidationError("session: incomplete binding")
        with self._lock:
            self._expire_locked()
            if len(self._requests) >= self._max_items:
                raise ValidationError("request: delete registry is full")
            request_id = _new_token("r")
            now = self.now()
            record = DeleteRequest(
                request_id=request_id,
                action=action,
                actor=actor,
                binding=binding,
                targets=tuple(targets),
                language=language,
                created_mono=now,
                expires_mono=now + self._ttl,
                require_state=require_state,
                items=list(items or []),
            )
            self._requests[request_id] = record
            return record

    def prepare_preview(
        self, request_id: str, binding: SessionBinding
    ) -> tuple[DeleteRequest, str, str]:
        with self._lock:
            self._expire_locked()
            record = self._requests.get(request_id)
            if record is None or not self._request_usable_locked(record, binding):
                raise TrustedDeleteDenied("delete request is not available")
            nonce = record.nonce or _new_token("n")
            return record, nonce, _canonical_preview_text(record, nonce)

    def mark_preview_sent(
        self, request_id: str, binding: SessionBinding, nonce: str
    ) -> DeleteRequest:
        if not nonce:
            raise TrustedDeleteDenied("delete preview could not be sent")
        with self._lock:
            self._expire_locked()
            current = self._requests.get(request_id)
            if current is None or not self._request_usable_locked(current, binding):
                raise TrustedDeleteDenied("delete request is not available")
            now = self.now()
            current.preview_sent = True
            current.nonce = nonce
            self._nonces[nonce] = ConfirmNonce(
                nonce=nonce,
                request_id=request_id,
                binding=current.binding,
                created_mono=now,
                expires_mono=current.expires_mono,
            )
            return current

    def open_preview(
        self,
        request_id: str,
        binding: SessionBinding,
        *,
        send: Callable[[str], bool],
    ) -> DeleteRequest:
        """Deliver the nonce-bearing preview via an injected transport.

        ``send`` must synchronously deliver the payload and return a truthy
        value. A non-truthy return, a raised exception, or an awaitable
        return (an ``async def``/task-based transport cannot confirm
        delivery synchronously) is a failed send: no nonce is registered and
        nothing becomes consumable.
        """
        _record, nonce, body = self.prepare_preview(request_id, binding)
        try:
            delivered = send(body)
        except Exception as exc:
            raise TrustedDeleteDenied("delete preview could not be sent") from exc
        if inspect.isawaitable(delivered):
            if inspect.iscoroutine(delivered):
                delivered.close()
            raise TrustedDeleteDenied("delete preview could not be sent")
        if not delivered:
            raise TrustedDeleteDenied("delete preview could not be sent")
        return self.mark_preview_sent(request_id, binding, nonce)

    def consume_confirm(
        self,
        nonce: str,
        binding: SessionBinding,
    ) -> DeleteRequest:
        with self._lock:
            self._expire_locked()
            token = self._nonces.get(nonce)
            record = self._requests.get(token.request_id) if token is not None else None
            # An observer in a shared chat has no authority to consume another
            # user's grant, even when they can see the nonce in the preview.
            if token is not None and (
                not _bindings_equal(token.binding, binding)
                or (record is not None and not _bindings_equal(record.binding, binding))
            ):
                raise TrustedDeleteDenied("confirmation is not available")
            # The dict lookup is already hash-keyed; compare_digest is
            # belt-and-braces so the token check does not short-circuit on
            # prefix length for a same-length candidate.
            if (
                token is None
                or not secrets.compare_digest(token.nonce, nonce)
                or record is None
                or token.consumed
                or record.consumed
                or record.cancelled
                or not record.preview_sent
                or not secrets.compare_digest(record.nonce or "", nonce)
                or self.now() >= token.expires_mono
                or self.now() >= record.expires_mono
                or not _bindings_equal(token.binding, binding)
                or not _bindings_equal(record.binding, binding)
            ):
                if token is not None:
                    token.consumed = True
                if record is not None and _bindings_equal(record.binding, binding):
                    record.consumed = True
                raise TrustedDeleteDenied("confirmation is not available")
            token.consumed = True
            record.consumed = True
            return record

    def cancel(
        self,
        token: str,
        binding: SessionBinding,
    ) -> DeleteRequest:
        with self._lock:
            self._expire_locked()
            record = self._requests.get(token)
            if record is None:
                nonce = self._nonces.get(token)
                record = self._requests.get(nonce.request_id) if nonce is not None else None
            if record is None or not _bindings_equal(record.binding, binding):
                raise TrustedDeleteDenied("delete request is not available")
            record.cancelled = True
            record.consumed = True
            if record.nonce:
                held = self._nonces.get(record.nonce)
                if held is not None:
                    held.consumed = True
            return record

    def peek(self, request_id: str) -> DeleteRequest | None:
        with self._lock:
            return self._requests.get(request_id)

    def _request_usable_locked(self, record: DeleteRequest, binding: SessionBinding) -> bool:
        return (
            not record.consumed
            and not record.cancelled
            and self.now() < record.expires_mono
            and _bindings_equal(record.binding, binding)
        )

    def _expire_locked(self) -> None:
        now = self.now()
        expired_ids = [
            request_id
            for request_id, record in self._requests.items()
            if now >= record.expires_mono
        ]
        for request_id in expired_ids:
            record = self._requests.pop(request_id, None)
            if record is not None and record.nonce:
                self._nonces.pop(record.nonce, None)
        expired_nonces = [
            nonce for nonce, held in self._nonces.items() if now >= held.expires_mono
        ]
        for nonce in expired_nonces:
            self._nonces.pop(nonce, None)


REGISTRY = DeleteRegistry()


def reset_registry() -> None:
    REGISTRY.reset()


def reject_legacy_confirm(value: object) -> None:
    """No model-supplied confirm/confirmed boolean can authorize deletion."""
    if value is None or value is False:
        return
    raise TrustedDeleteDenied(
        "confirm: model-supplied confirmation cannot authorize deletion"
    )


def bind_targets(
    actor: Actor,
    claim_ids: list[str],
    *,
    store: PersonalMemoryStore,
    language: str,
    require_state: str | None = None,
) -> tuple[list[BoundTarget], list[dict[str, object]]]:
    targets: list[BoundTarget] = []
    items: list[dict[str, object]] = []
    for claim_id in claim_ids:
        row = get_authorized_claim(actor, claim_id, include_content=True, store=store)
        if require_state is not None and str(row.get("state")) != require_state:
            raise ValidationError("claim: not a candidate")
        content = row.get("content")
        if isinstance(content, str):
            payload = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray)):
            payload = bytes(content)
        else:
            raise ValidationError("claim: content unavailable")
        targets.append(
            BoundTarget(
                claim_id=str(row["claim_id"]),
                version=int(row["version"]),
                claim_version_id=str(row["claim_version_id"]),
                content_hash=content_hash_for(payload),
            )
        )
        items.append(
            {
                "claim_id": str(row["claim_id"]),
                "summary": format_claim_summary(row, language=language, include_content=True),
                "state": row.get("state"),
                "claim_kind": row.get("claim_kind"),
                "version": int(row["version"]),
            }
        )
    return targets, items


def create_delete_preview(
    actor: Actor,
    *,
    action: str,
    claim_ids: list[str],
    language: object = "en",
    store: PersonalMemoryStore,
    session_binding: SessionBinding | None = None,
    require_state: str | None = None,
    empty_kind: str,
    preview_kind: str,
) -> dict[str, object]:
    """Create a non-authorizing delete preview.

    The returned payload contains only ``request_id``, the matched items and
    human-readable confirmation text. It never contains a nonce: the nonce is
    created later inside :meth:`DeleteRegistry.open_preview` and exists solely
    inside the send() payload delivered to the user. ``store`` and
    ``session_binding`` are explicit arguments; there is no ambient session
    lookup or default store construction.
    """
    lang = normalize_language(language)
    binding = session_binding
    if not claim_ids:
        return {
            "language": lang,
            "preview": True,
            "matched_claim_ids": [],
            "items": [],
            "confirmation": format_mutation_confirmation(empty_kind, language=lang),
        }
    targets, items = bind_targets(
        actor,
        claim_ids,
        store=store,
        language=lang,
        require_state=require_state,
    )
    payload: dict[str, object] = {
        "language": lang,
        "preview": True,
        "matched_claim_ids": [item["claim_id"] for item in items],
        "items": items,
        "confirmation": format_mutation_confirmation(
            preview_kind,
            language=lang,
            count=len(items),
            summary=items[0]["summary"] if items else "",
        ),
    }
    if len(items) == 1:
        payload["claim_id"] = items[0]["claim_id"]
        payload["summary"] = items[0]["summary"]
        payload["state"] = items[0]["state"]
        payload["claim_kind"] = items[0]["claim_kind"]
    if binding is None:
        payload["delete_command"] = None
        payload["request_id"] = None
        payload["confirmation"] = (
            str(payload["confirmation"])
            + " "
            + _session_required_text(lang)
        )
        return payload
    record = REGISTRY.create_request(
        action=action,
        actor=actor,
        binding=binding,
        targets=targets,
        language=lang,
        require_state=require_state,
        items=items,
    )
    command = f"/{MEMORY_DELETE_COMMAND.replace('-', '_')} {record.request_id}"
    payload["request_id"] = record.request_id
    payload["delete_command"] = command
    payload["confirmation"] = (
        str(payload["confirmation"]) + " " + _open_preview_text(lang, command)
    )
    return payload


def consume_delete_request(
    record: DeleteRequest,
    *,
    store: PersonalMemoryStore,
) -> dict[str, object]:
    """Execute the bound erasure for a consumed request.

    ``store`` is required: the caller must pass the same store the preview was
    bound against. ``erase_bound_claims`` rechecks claim id, version, version
    id and content hash inside one transaction and denies the whole batch on
    any mismatch.
    """
    receipts = store.erase_bound_claims(
        record.actor,
        [target.as_dict() for target in record.targets],
        require_state=record.require_state,
    )
    erased_ids = [target.claim_id for target in record.targets]
    kind = {
        "forget_personal_memory": "forget",
        "do_not_remember": "do_not_remember_some",
        "reject_candidate_memory": "reject_candidate",
    }.get(record.action, "forget")
    confirmation = format_mutation_confirmation(
        kind,
        language=record.language,
        count=len(erased_ids),
    )
    payload: dict[str, object] = {
        "language": record.language,
        "preview": False,
        "erased_claim_ids": erased_ids,
        "confirmation": confirmation,
    }
    if receipts:
        payload["receipt_id"] = receipts[0].receipt_id
        payload["erased_at"] = receipts[0].erased_at
    if len(erased_ids) == 1:
        payload["claim_id"] = erased_ids[0]
    return payload


# --- Private helpers -------------------------------------------------------


def _binding_complete(binding: SessionBinding) -> bool:
    return bool(
        binding.platform
        and binding.user_id not in _INVALID_USER_IDS
        and binding.session_key
        and binding.session_id
        and binding.chat_id
    )


def _bindings_equal(left: SessionBinding, right: SessionBinding) -> bool:
    return (
        left.platform == right.platform
        and left.user_id == right.user_id
        and left.session_key == right.session_key
        and left.session_id == right.session_id
        and left.chat_id == right.chat_id
        and left.thread_id == right.thread_id
        and left.profile == right.profile
    )


def _new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(_TOKEN_BYTES)


def _canonical_preview_text(record: DeleteRequest, nonce: str) -> str:
    lang = record.language
    lines = [_preview_heading(lang, record.action, len(record.targets))]
    for item in record.items:
        lines.append(f"- {item.get('summary')}")
    confirm = f"/{MEMORY_CONFIRM_COMMAND.replace('-', '_')} {nonce}"
    cancel = f"/{MEMORY_CANCEL_COMMAND.replace('-', '_')} {record.request_id}"
    if lang == "ru":
        lines.append(f"Подтвердите удаление: {confirm}")
        lines.append(f"Отмена: {cancel}")
    else:
        lines.append(f"Confirm deletion: {confirm}")
        lines.append(f"Cancel: {cancel}")
    return "\n".join(lines)


def _preview_heading(language: str, action: str, count: int) -> str:
    if language == "ru":
        return f"Точный список на удаление ({action}), записей: {count}."
    return f"Exact deletion preview ({action}), items: {count}."


def _open_preview_text(language: str, command: str) -> str:
    if language == "ru":
        return f"Откройте точный список командой {command}."
    return f"Open the exact preview with {command}."


def _session_required_text(language: str) -> str:
    if language == "ru":
        return "Удаление возможно только из подтверждённой сессии."
    return "Deletion requires an authenticated messaging session."

"""Owner export and safe claim explanation. Actor comes from the caller."""

from __future__ import annotations

import json

from .store import PersonalMemoryStore
from .types import Actor


def export_owner_data(
    actor: Actor, store: PersonalMemoryStore | None = None
) -> bytes:
    memory = store if store is not None else PersonalMemoryStore()
    payload = memory.export_owner_data(actor)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def explain_claim(
    actor: Actor,
    claim_id: str,
    store: PersonalMemoryStore | None = None,
) -> dict[str, object]:
    memory = store if store is not None else PersonalMemoryStore()
    return memory.explain_claim(actor, claim_id)

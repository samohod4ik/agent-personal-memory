"""Closed claim lifecycle states for personal memory."""

from __future__ import annotations

from enum import Enum


class MemoryState(str, Enum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


ACTIVE_READ_STATES = frozenset({MemoryState.CANDIDATE, MemoryState.CONFIRMED})

"""Typed identities for the agent-personal-memory package."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    platform: str
    user_id: str
    display_name: str = ""

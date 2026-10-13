"""Typed identities for the personal-memory plugin."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    platform: str
    user_id: str
    display_name: str = ""

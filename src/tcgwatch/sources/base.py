"""Interface every price source implements."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from typing import Protocol

from ..config import GameConfig
from ..models import Card, PricePoint


class PriceSource(Protocol):
    def snapshot_date(self) -> date:
        """Date the source's current dataset represents (not the local clock)."""
        ...

    def published_at(self) -> datetime:
        """Full publication timestamp of the current dataset. Optional: sources without it
        fall back to snapshot_date() for the snapshot stability check."""
        ...

    def resolve_category(self, game: GameConfig) -> int:
        ...

    def fetch_singles(self, game: GameConfig) -> Iterator[tuple[list[Card], list[PricePoint]]]:
        """Yield (cards, prices) per set. Only singles; sealed product is skipped."""
        ...

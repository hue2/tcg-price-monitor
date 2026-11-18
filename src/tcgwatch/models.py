"""Plain data types shared between sources, storage, and movement logic."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Card:
    product_id: int
    game_key: str
    group_id: int
    group_name: str
    name: str
    number: str | None
    rarity: str
    url: str | None = None
    image_url: str | None = None


@dataclass(frozen=True)
class PricePoint:
    product_id: int
    printing: str  # TCGplayer subTypeName: "Normal", "Foil", ...
    market: float | None
    low: float | None
    mid: float | None
    high: float | None


@dataclass(frozen=True)
class Observation:
    """One (product, printing) series sampled at a baseline date and the current date."""

    card: Card
    printing: str
    baseline_date: str
    current_date: str
    baseline: dict[str, float | None]
    current: dict[str, float | None]


@dataclass(frozen=True)
class Mover:
    card: Card
    printing: str
    price_field: str
    baseline_date: str
    current_date: str
    baseline_price: float
    current_price: float
    pct_change: float


@dataclass(frozen=True)
class Unpaired:
    """A watched series with no comparison baseline: current price (if any), nothing to compare to."""
    card: Card
    printing: str
    price_field: str | None
    current_date: str | None
    current_price: float | None

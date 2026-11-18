"""Watchlist argument validation shared by the web API and the CLI."""

from __future__ import annotations

from typing import Any

MAX_PRODUCT_ID = 2**63 - 1  # SQLite INTEGER range


def validate_watch(product_id: Any, printing: Any) -> str | None:
    """Return an error message if (product_id, printing) is not a valid watchlist key, else None."""
    if (not isinstance(product_id, int) or isinstance(product_id, bool) or not isinstance(printing, str)
            or not printing.strip()):
        return "body must be {product_id: int, printing: str}"
    if not 1 <= product_id <= MAX_PRODUCT_ID:
        return "product_id out of range"
    return None

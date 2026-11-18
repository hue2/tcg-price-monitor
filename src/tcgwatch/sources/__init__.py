"""Price source registry. Register new sources here; games pick one via `source:`."""

from __future__ import annotations

from ..config import AppConfig, ConfigError
from .base import PriceSource
from .tcgcsv import TcgCsvSource


def build_source(name: str, cfg: AppConfig) -> PriceSource:
    if name == "tcgcsv":
        return TcgCsvSource(cfg.user_agent, cfg.request_delay_seconds)
    raise ConfigError(f"unknown price source {name!r}")


__all__ = ["PriceSource", "TcgCsvSource", "build_source"]

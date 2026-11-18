"""Configuration loading, validation, and alert-rule merging."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field, fields, replace
from datetime import time, timedelta
from pathlib import Path
from typing import Any

import yaml

BASELINE_GRACE_DAYS = 2  # mirrors store.BASELINE_GRACE_DAYS (store imports nothing from config)
PRICE_FIELDS = ("market", "mid", "low", "high")
DIRECTIONS = ("up", "down", "both")
_DURATION = re.compile(r"^\s*(\d+)\s*([hdw])\s*$", re.IGNORECASE)


class ConfigError(ValueError):
    pass


def parse_duration(value: Any, *, allow_hours: bool = False) -> timedelta:
    """Parse '7d', '2w', or (if allowed) '12h'. A bare int means days."""
    if isinstance(value, int) and not isinstance(value, bool):
        delta = timedelta(days=value)
    else:
        m = _DURATION.match(str(value))
        if not m:
            raise ConfigError(f"invalid duration {value!r}; use a whole number plus a unit: "
                              f"d (days) or w (weeks), e.g. '7d' or '2w'" + (", or h (hours)" if allow_hours else ""))
        n, unit = int(m[1]), m[2].lower()
        if unit == "h" and not allow_hours:
            raise ConfigError(f"duration {value!r} is in hours, but only d (days) and w (weeks) are accepted "
                              f"here, e.g. '7d' or '2w'")
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
    if delta <= timedelta(0):
        raise ConfigError(f"duration {value!r} must be positive")
    return delta


def parse_days(value: Any) -> int:
    return parse_duration(value).days


def _to_float(key: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a number, got {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ConfigError(f"{key} must be a finite number, got {value!r}") from None
    if not math.isfinite(result):
        raise ConfigError(f"{key} must be a finite number, got {value!r}")
    return result


@dataclass(frozen=True)
class AlertRule:
    window_days: int = 7
    threshold_pct: float = 30.0
    direction: str = "up"
    min_price: float = 0.50
    min_abs_change: float = 0.25
    price_fields: tuple[str, ...] = ("market", "mid")
    cooldown_days: int | None = None  # None = same as window
    discord_threshold_pct: float | None = None  # None = same as threshold_pct
    max_fallback_spread: float | None = 10.0  # None/0 = off; see movement.is_noisy_fallback

    @property
    def effective_cooldown_days(self) -> int:
        return self.window_days if self.cooldown_days is None else self.cooldown_days

    @property
    def effective_discord_threshold(self) -> float:
        """Discord can only post what was flagged, so it never goes below threshold_pct."""
        if self.discord_threshold_pct is None:
            return self.threshold_pct
        return max(self.threshold_pct, self.discord_threshold_pct)

    def merged(self, raw: dict[str, Any] | None) -> AlertRule:
        """Return a copy with keys from a raw YAML `alert:` block applied."""
        if not raw:
            return self
        known = {"window", "threshold_pct", "direction", "min_price", "min_abs_change",
                 "price_fields", "cooldown", "discord_threshold_pct", "max_fallback_spread"}
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(f"unknown alert keys: {sorted(unknown)}")
        changes: dict[str, Any] = {}
        if "window" in raw:
            changes["window_days"] = parse_days(raw["window"])
        if "cooldown" in raw:
            changes["cooldown_days"] = None if raw["cooldown"] is None else parse_days(raw["cooldown"])
        for key in ("threshold_pct", "min_price", "min_abs_change"):
            if key in raw:
                changes[key] = _to_float(key, raw[key])
        if "discord_threshold_pct" in raw:
            value = raw["discord_threshold_pct"]
            changes["discord_threshold_pct"] = None if value is None else _to_float("discord_threshold_pct", value)
        if "max_fallback_spread" in raw:
            value = raw["max_fallback_spread"]
            changes["max_fallback_spread"] = None if value is None else _to_float("max_fallback_spread", value)
        if "direction" in raw:
            changes["direction"] = str(raw["direction"]).lower()
        if "price_fields" in raw:
            changes["price_fields"] = tuple(str(f).lower() for f in raw["price_fields"])
        return replace(self, **changes).validated()

    def validated(self) -> AlertRule:
        if self.direction not in DIRECTIONS:
            raise ConfigError(f"direction must be one of {DIRECTIONS}, got {self.direction!r}")
        if self.threshold_pct < 0:
            raise ConfigError("threshold_pct must be >= 0")
        if self.discord_threshold_pct is not None and self.discord_threshold_pct < 0:
            raise ConfigError("discord_threshold_pct must be >= 0")
        if self.max_fallback_spread is not None and self.max_fallback_spread < 0:
            raise ConfigError("max_fallback_spread must be >= 0 (0 or null = off)")
        bad = [f for f in self.price_fields if f not in PRICE_FIELDS]
        if bad or not self.price_fields:
            raise ConfigError(f"price_fields must be a non-empty subset of {PRICE_FIELDS}")
        return self


@dataclass(frozen=True)
class GameConfig:
    key: str
    name: str
    alert: AlertRule
    enabled: bool = True
    source: str = "tcgcsv"
    category_id: int | None = None
    category_name: str | None = None
    rarities: tuple[str, ...] = ()  # empty = all rarities
    exclude_name_patterns: tuple[str, ...] = ()
    printings: tuple[str, ...] = ()  # empty = all printings
    # Sets: an int is a TCGplayer group ID, a string is a case-insensitive regex on the set name.
    include_sets: tuple[int | str, ...] = ()  # empty = all sets
    exclude_sets: tuple[int | str, ...] = ()


MAX_LATE_WINDOW = timedelta(hours=3)
DEFAULT_RETENTION_DAYS = 180
MIN_CATCH_UP_DAYS, DEFAULT_CATCH_UP_DAYS, MAX_DEFAULT_CATCH_UP_DAYS, MAX_CATCH_UP_DAYS = 1, 7, 30, 90
_AT = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def parse_at(value: Any) -> time:
    """'20:30' -> time(20, 30). Always UTC (TCGCSV publishes on UTC)."""
    m = _AT.match(str(value))
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ConfigError(f"invalid schedule.at {value!r}; use 24-hour HH:MM in UTC, e.g. '20:30'")
    return time(int(m[1]), int(m[2]))


@dataclass(frozen=True)
class AppConfig:
    database: Path
    user_agent: str
    request_delay_seconds: float
    alert: AlertRule
    console: bool
    # `tcgwatch schedule` fires at this UTC wall-clock time each day.
    at: time = time(20, 30)
    # How long after `at` to keep polling for a late TCGCSV publication (every 10 minutes).
    late_window: timedelta = MAX_LATE_WINDOW
    # None = max(7, largest alert window), capped at 30; an explicit value overrides.
    catch_up_days_setting: int | None = None
    # Deprecated: if set, `schedule`/`watch` run in legacy fixed-interval mode.
    check_every: timedelta | None = None
    # Price rows older than this many days (before the newest stored day) are pruned; None = keep forever.
    # Never shorter than the largest alert window (+ baseline grace) or catch_up_days: see retention_days_for.
    retention_days: int | None = DEFAULT_RETENTION_DAYS
    discord_webhook_url: str | None = None
    discord_webhook_env: str | None = None
    discord_format: str = "text"  # text | embeds
    web_auth_token: str | None = None  # web.auth_token; env TCGWATCH_WEB_TOKEN overrides (see web_token)
    games: dict[str, GameConfig] = field(default_factory=dict)

    @property
    def catch_up_days(self) -> int:
        if self.catch_up_days_setting is not None:
            return self.catch_up_days_setting
        largest = max([self.alert.window_days, *(g.alert.window_days for g in self.games.values())])
        return min(MAX_DEFAULT_CATCH_UP_DAYS, max(DEFAULT_CATCH_UP_DAYS, largest))

    def retention_days_for(self, game: GameConfig, window_days: int | None = None) -> int | None:
        """Days of history kept for a game: the configured retention, floored so pruning can never touch
        data an alert baseline (window + grace) or catch-up still needs. `window_days` is the EFFECTIVE check
        window (a scheduled `--window` override); the floor uses the larger of it and the configured one.
        None = pruning disabled."""
        if self.retention_days is None:
            return None
        window = max(game.alert.window_days, window_days or 0)
        return max(self.retention_days, window + BASELINE_GRACE_DAYS, self.catch_up_days)

    @property
    def web_token(self) -> str | None:
        """Web UI access token: env TCGWATCH_WEB_TOKEN wins over web.auth_token. None = no auth."""
        return (os.environ.get("TCGWATCH_WEB_TOKEN") or "").strip() or self.web_auth_token

    @property
    def discord_webhook(self) -> str | None:
        """Webhook from config, else from the named env var. None = Discord off."""
        if self.discord_webhook_url:
            return self.discord_webhook_url
        return os.environ.get(self.discord_webhook_env) if self.discord_webhook_env else None

    def selected_games(self, keys: list[str] | None = None) -> list[GameConfig]:
        if keys:
            missing = [k for k in keys if k not in self.games]
            if missing:
                raise ConfigError(f"unknown game(s): {missing}; configured: {list(self.games)}")
            return [self.games[k] for k in keys]
        return [g for g in self.games.values() if g.enabled]


def _tuple(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    return tuple(str(x) for x in raw)


def parse_sets(raw: Any, where: str = "sets") -> tuple[int | str, ...]:
    """Set selectors: ints are group IDs, strings are regexes on the set name."""
    if raw is None:
        return ()
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[int | str] = []
    for item in items:
        if isinstance(item, int) and not isinstance(item, bool):
            out.append(item)
        elif isinstance(item, str) and item.strip().isdigit():
            out.append(int(item))
        else:
            _check_regex(str(item), where)
            out.append(str(item))
    return tuple(out)


def _check_regex(pattern: str, where: str) -> None:
    try:
        re.compile(pattern)
    except re.error as e:
        raise ConfigError(f"{where}: bad regex {pattern!r}: {e}") from e


def _game(key: str, raw: dict[str, Any], default_rule: AlertRule) -> GameConfig:
    allowed = {f.name for f in fields(GameConfig)} - {"key"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"game {key!r}: unknown keys {sorted(unknown)}")
    if raw.get("category_id") is None and not raw.get("category_name"):
        raise ConfigError(f"game {key!r}: set category_id or category_name")
    for pattern in _tuple(raw.get("exclude_name_patterns")):
        _check_regex(pattern, f"game {key!r}")
    return GameConfig(
        key=key,
        name=str(raw.get("name", key)),
        alert=default_rule.merged(raw.get("alert")),
        enabled=bool(raw.get("enabled", True)),
        source=str(raw.get("source", "tcgcsv")),
        category_id=int(raw["category_id"]) if raw.get("category_id") is not None else None,
        category_name=raw.get("category_name"),
        rarities=_tuple(raw.get("rarities")),
        exclude_name_patterns=_tuple(raw.get("exclude_name_patterns")),
        printings=_tuple(raw.get("printings")),
        include_sets=parse_sets(raw.get("include_sets"), f"game {key!r} include_sets"),
        exclude_sets=parse_sets(raw.get("exclude_sets"), f"game {key!r} exclude_sets"),
    )


def parse_config(raw: dict[str, Any], base_dir: Path = Path(".")) -> AppConfig:
    raw = raw or {}
    source = raw.get("source") or {}
    schedule = raw.get("schedule") or {}
    notify = raw.get("notify") or {}
    discord = notify.get("discord") or {}
    webhook_url = (discord.get("webhook_url") or "").strip() or None
    if webhook_url and not webhook_url.startswith("https://"):
        raise ConfigError("notify.discord.webhook_url must be an https:// URL")
    raw_format = discord.get("format", "text")
    if raw_format is None:
        raw_format = "text"
    if not isinstance(raw_format, str):
        raise ConfigError("notify.discord.format must be 'text' or 'embeds'")
    discord_format = raw_format.strip().lower() or "text"
    if discord_format not in ("text", "embeds"):
        raise ConfigError("notify.discord.format must be 'text' or 'embeds'")
    web = raw.get("web") or {}
    if not isinstance(web, dict):
        raise ConfigError("`web` must be a mapping")
    web_token = web.get("auth_token")
    if web_token is not None and not isinstance(web_token, str):
        raise ConfigError("web.auth_token must be a string")
    rule = AlertRule().merged(raw.get("alert"))
    games_raw = raw.get("games") or {}
    if not isinstance(games_raw, dict):
        raise ConfigError("`games` must be a mapping of key -> game settings")
    database = Path(raw.get("database", "data/tcgwatch.db"))
    late_window = parse_duration(schedule.get("late_window", "3h"), allow_hours=True)
    if late_window > MAX_LATE_WINDOW:
        raise ConfigError("schedule.late_window must be at most 3h")
    catch_up = schedule.get("catch_up_days")
    if catch_up is not None:
        if isinstance(catch_up, bool) or not isinstance(catch_up, int) \
                or not MIN_CATCH_UP_DAYS <= catch_up <= MAX_CATCH_UP_DAYS:
            raise ConfigError(f"schedule.catch_up_days must be a whole number {MIN_CATCH_UP_DAYS}-{MAX_CATCH_UP_DAYS}")
    check_every = schedule.get("check_every")
    retention = raw.get("retention_days", DEFAULT_RETENTION_DAYS)
    if retention is not None:
        if isinstance(retention, bool) or not isinstance(retention, int) or retention < 0:
            raise ConfigError("retention_days must be a whole number of days >= 0 (0 or null keeps all history)")
        retention = retention or None
    return AppConfig(
        database=database if database.is_absolute() else base_dir / database,
        user_agent=str(source.get("user_agent", "tcg-price-watch/0.12.0")),
        request_delay_seconds=float(source.get("request_delay_seconds", 0.2)),
        at=parse_at(schedule.get("at", "20:30")),
        late_window=late_window,
        catch_up_days_setting=catch_up,
        check_every=parse_duration(check_every, allow_hours=True) if check_every is not None else None,
        retention_days=retention,
        alert=rule,
        console=bool(notify.get("console", True)),
        discord_webhook_url=webhook_url,
        discord_webhook_env=discord.get("webhook_env"),
        discord_format=discord_format,
        web_auth_token=(web_token or "").strip() or None,
        games={k: _game(k, v or {}, rule) for k, v in games_raw.items()},
    )


def default_config_path() -> Path:
    return Path(os.environ.get("TCGWATCH_CONFIG", "config.yaml"))


def load_config(path: Path | None = None) -> AppConfig:
    path = path or default_config_path()
    if not path.exists():
        raise ConfigError(f"config not found: {path} (copy config.example.yaml to config.yaml)")
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return parse_config(raw, base_dir=path.resolve().parent)

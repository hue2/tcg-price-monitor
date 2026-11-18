"""Pure price-movement evaluation. No I/O, so it is easy to test."""

from __future__ import annotations

from collections.abc import Iterable

from .config import AlertRule
from .models import Mover, Observation


def pct_change(baseline: float, current: float) -> float:
    return (current - baseline) / baseline * 100.0


def compare(obs: Observation, rule: AlertRule) -> Mover | None:
    """Price change for the first field present on both dates, whether or not it's flagged."""
    for field in rule.price_fields:
        base, cur = obs.baseline.get(field), obs.current.get(field)
        if base and cur and base > 0:  # same field on both sides
            break
    else:
        return None
    return Mover(
        card=obs.card,
        printing=obs.printing,
        price_field=field,
        baseline_date=obs.baseline_date,
        current_date=obs.current_date,
        baseline_price=base,
        current_price=cur,
        pct_change=pct_change(base, cur),
    )


def is_flagged(m: Mover, rule: AlertRule) -> bool:
    base, cur, pct = m.baseline_price, m.current_price, m.pct_change
    if max(base, cur) < rule.min_price:
        return False
    if abs(cur - base) < rule.min_abs_change:
        return False
    if rule.direction == "up":
        return pct >= rule.threshold_pct
    if rule.direction == "down":
        return pct <= -rule.threshold_pct
    return abs(pct) >= rule.threshold_pct


def _noisy(prices: dict[str, float | None], spread: float) -> bool:
    if prices.get("market") is not None:
        return False  # real market price on this side, so mid is not a fallback
    low, high = prices.get("low"), prices.get("high")
    return bool(high) and high / max(low or 0.0, 0.01) > spread


def is_noisy_fallback(obs: Observation, m: Mover, rule: AlertRule) -> bool:
    """True when `mid` was used because marketPrice is null and high/low is too wide.

    Checked on both the baseline and current side; a missing low counts as 0.01.
    """
    spread = rule.max_fallback_spread
    if not spread or m.price_field != "mid":
        return False
    return _noisy(obs.baseline, spread) or _noisy(obs.current, spread)


def evaluate_ex(obs: Observation, rule: AlertRule) -> tuple[Mover | None, bool]:
    """(mover, skipped): skipped=True when it would be flagged but the noisy-fallback guard dropped it."""
    m = compare(obs, rule)
    if not m or not is_flagged(m, rule):
        return None, False
    if is_noisy_fallback(obs, m, rule):
        return None, True
    return m, False


def evaluate(obs: Observation, rule: AlertRule) -> Mover | None:
    """Return a Mover if the observation crosses the rule's threshold, else None."""
    return evaluate_ex(obs, rule)[0]


def find_movers_ex(observations: Iterable[Observation], rule: AlertRule) -> tuple[list[Mover], int]:
    """Sorted movers plus the count skipped by max_fallback_spread."""
    movers, skipped = [], 0
    for o in observations:
        m, skip = evaluate_ex(o, rule)
        if m:
            movers.append(m)
        skipped += skip
    return sorted(movers, key=lambda m: abs(m.pct_change), reverse=True), skipped


def find_movers(observations: Iterable[Observation], rule: AlertRule) -> list[Mover]:
    return find_movers_ex(observations, rule)[0]

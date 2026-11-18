import pytest

from tcgwatch.config import AlertRule
from tcgwatch.models import Card, Observation
from tcgwatch.movement import evaluate, find_movers

CARD = Card(product_id=1, game_key="g", group_id=1, group_name="Set", name="X", number=None, rarity="R")


def obs(base, cur, field="market", **extra):
    b = {"market": None, "mid": None, "low": None, "high": None}
    c = dict(b)
    b[field], c[field] = base, cur
    for k, (bv, cv) in extra.items():
        b[k], c[k] = bv, cv
    return Observation(CARD, "Normal", "2026-10-30", "2026-11-06", b, c)


@pytest.mark.parametrize("base,cur,hit", [
    (1.00, 1.30, True),   # exactly +30%
    (1.00, 1.29, False),
    (10.0, 20.0, True),
    (10.0, 5.0, False),   # down move, rule is up-only
])
def test_default_rule(base, cur, hit):
    assert (evaluate(obs(base, cur), AlertRule()) is not None) is hit


def test_direction_down_and_both():
    assert evaluate(obs(10, 6), AlertRule(direction="down"))
    assert not evaluate(obs(10, 14), AlertRule(direction="down"))
    assert evaluate(obs(10, 6), AlertRule(direction="both"))
    assert evaluate(obs(10, 14), AlertRule(direction="both"))


def test_noise_floors():
    assert not evaluate(obs(0.10, 0.40), AlertRule())          # under min_price on both sides
    assert not evaluate(obs(0.50, 0.70), AlertRule())          # +40% but only $0.20 move
    assert evaluate(obs(0.50, 0.70), AlertRule(min_abs_change=0))


def test_falls_back_to_next_field_present_on_both_dates():
    o = obs(None, None, mid=(2.0, 3.0))
    m = evaluate(o, AlertRule())
    assert m and m.price_field == "mid" and m.pct_change == pytest.approx(50)


def test_never_mixes_fields():
    # market only on baseline, mid only on current -> no comparable field
    o = obs(2.0, None)
    o.current["mid"] = 10.0
    assert evaluate(o, AlertRule()) is None


def test_adjustable_threshold():
    assert not evaluate(obs(1, 1.4), AlertRule(threshold_pct=50))
    assert evaluate(obs(1, 1.6), AlertRule(threshold_pct=50))


def test_sorted_by_magnitude():
    movers = find_movers([obs(1, 1.5), obs(1, 3.0), obs(1, 1.35)], AlertRule())
    assert [round(m.pct_change) for m in movers] == [200, 50, 35]


def test_zero_or_missing_baseline_is_not_a_mover():
    from tcgwatch.movement import compare

    assert compare(obs(0.0, 5.0), AlertRule()) is None  # no divide-by-zero, no mover
    assert compare(obs(None, 5.0), AlertRule()) is None


def test_no_price_field_present_on_both_dates_is_not_a_mover():
    from tcgwatch.movement import compare

    o = Observation(CARD, "Normal", "2026-10-30", "2026-11-06",
                    {"market": 1.0, "mid": None, "low": None, "high": None},
                    {"market": None, "mid": 2.0, "low": None, "high": None})
    assert compare(o, AlertRule(price_fields=("market", "mid"))) is None


def test_noisy_mid_fallback_skipped():
    from tcgwatch.movement import find_movers_ex
    noisy = obs(1.0, 2.0, field="mid", low=(0.5, 0.5), high=(2.0, 20.0))  # current high/low = 40
    calm = obs(1.0, 2.0, field="mid", low=(0.9, 1.8), high=(1.1, 2.2))
    rule = AlertRule(price_fields=("mid",))
    assert evaluate(noisy, rule) is None
    assert evaluate(calm, rule)
    movers, skipped = find_movers_ex([noisy, calm], rule)
    assert len(movers) == 1 and skipped == 1


def test_fallback_guard_off_and_market_exempt():
    noisy = obs(1.0, 2.0, field="mid", low=(0.5, 0.5), high=(2.0, 20.0))
    assert evaluate(noisy, AlertRule(price_fields=("mid",), max_fallback_spread=None))
    assert evaluate(noisy, AlertRule(price_fields=("mid",), max_fallback_spread=0))
    # market present on both sides: mid isn't a fallback, guard doesn't apply
    both = obs(1.0, 2.0, field="market", low=(0.5, 0.5), high=(2.0, 20.0))
    assert evaluate(both, AlertRule())

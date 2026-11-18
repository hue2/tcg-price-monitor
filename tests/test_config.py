from datetime import timedelta

import pytest

from tcgwatch.config import AlertRule, ConfigError, parse_config, parse_days, parse_duration


@pytest.mark.parametrize("value,days", [("7d", 7), ("2w", 14), ("30D", 30), (5, 5)])
def test_parse_days(value, days):
    assert parse_days(value) == days


@pytest.mark.parametrize("value", ["0d", "abc", "12h", "-3d"])
def test_parse_days_rejects(value):
    with pytest.raises(ConfigError):
        parse_days(value)


def test_schedule_allows_hours():
    assert parse_duration("12h", allow_hours=True) == timedelta(hours=12)


def test_example_config_defaults(example_cfg):
    assert set(example_cfg.games) == {"one_piece", "riftbound"}
    rule = example_cfg.games["one_piece"].alert
    assert rule.window_days == 7
    assert rule.threshold_pct == 30
    assert rule.direction == "up"
    assert rule.effective_cooldown_days == 7
    assert example_cfg.games["riftbound"].category_id == 89


def test_per_game_override_inherits_global():
    cfg = parse_config({
        "alert": {"window": "2w", "threshold_pct": 25},
        "games": {
            "a": {"category_id": 1, "alert": {"threshold_pct": 50}},
            "b": {"category_name": "Foo"},
        },
    })
    assert cfg.games["a"].alert == AlertRule(window_days=14, threshold_pct=50)
    assert cfg.games["b"].alert == AlertRule(window_days=14, threshold_pct=25)


@pytest.mark.parametrize("game", [
    {},  # no category
    {"category_id": 1, "bogus": True},
    {"category_id": 1, "exclude_name_patterns": ["(unclosed"]},
    {"category_id": 1, "alert": {"direction": "sideways"}},
    {"category_id": 1, "alert": {"price_fields": ["average"]}},
])
def test_invalid_game_config(game):
    with pytest.raises(ConfigError):
        parse_config({"games": {"x": game}})


def test_discord_threshold_defaults_and_floor():
    assert AlertRule().effective_discord_threshold == 30
    assert AlertRule(discord_threshold_pct=75).effective_discord_threshold == 75
    # can't post what wasn't flagged
    assert AlertRule(threshold_pct=40, discord_threshold_pct=10).effective_discord_threshold == 40
    cfg = parse_config({"alert": {"discord_threshold_pct": 50},
                        "games": {"a": {"category_id": 1, "alert": {"discord_threshold_pct": 80}},
                                  "b": {"category_id": 2}}})
    assert cfg.games["a"].alert.effective_discord_threshold == 80
    assert cfg.games["b"].alert.effective_discord_threshold == 50


def test_discord_webhook_config_then_env(monkeypatch):
    monkeypatch.setenv("HOOK", "https://discord.com/api/webhooks/env")
    from_env = parse_config({"notify": {"discord": {"webhook_env": "HOOK"}}})
    assert from_env.discord_webhook == "https://discord.com/api/webhooks/env"
    direct = parse_config({"notify": {"discord": {"webhook_url": "https://discord.com/api/webhooks/x",
                                                  "webhook_env": "HOOK"}}})
    assert direct.discord_webhook == "https://discord.com/api/webhooks/x"
    assert parse_config({}).discord_webhook is None
    with pytest.raises(ConfigError):
        parse_config({"notify": {"discord": {"webhook_url": "http://insecure"}}})


def test_sets_config():
    cfg = parse_config({"games": {"a": {"category_id": 1, "include_sets": [24833, "Royal"],
                                        "exclude_sets": "Promo"}}})
    assert cfg.games["a"].include_sets == (24833, "Royal")
    assert cfg.games["a"].exclude_sets == ("Promo",)
    with pytest.raises(ConfigError):
        parse_config({"games": {"a": {"category_id": 1, "include_sets": ["(bad"]}}})


def test_selected_games_skips_disabled():
    cfg = parse_config({"games": {"a": {"category_id": 1}, "b": {"category_id": 2, "enabled": False}}})
    assert [g.key for g in cfg.selected_games()] == ["a"]
    assert [g.key for g in cfg.selected_games(["b"])] == ["b"]
    with pytest.raises(ConfigError):
        cfg.selected_games(["nope"])


def test_non_numeric_threshold_is_a_config_error():
    with pytest.raises(ConfigError):
        AlertRule().merged({"threshold_pct": "lots"})


NUMERIC_KEYS = ("threshold_pct", "min_price", "min_abs_change", "discord_threshold_pct", "max_fallback_spread")
BAD_NUMBERS = (True, False, "nan", "inf", "-inf", float("nan"), float("inf"), 10**400)


@pytest.mark.parametrize("key", NUMERIC_KEYS)
@pytest.mark.parametrize("value", BAD_NUMBERS)
def test_bool_nan_inf_and_huge_rejected_global_and_per_game(key, value):
    with pytest.raises(ConfigError, match=key):
        parse_config({"alert": {key: value}})
    with pytest.raises(ConfigError, match=key):
        parse_config({"games": {"a": {"category_id": 1, "alert": {key: value}}}})


def test_valid_numbers_still_accepted():
    assert AlertRule().merged({"threshold_pct": "12.5", "min_price": 3}).min_price == 3.0


def test_max_fallback_spread_config():
    from tcgwatch.config import AlertRule, ConfigError
    assert AlertRule().max_fallback_spread == 10.0
    assert AlertRule().merged({"max_fallback_spread": 5}).max_fallback_spread == 5.0
    assert AlertRule().merged({"max_fallback_spread": None}).max_fallback_spread is None
    import pytest
    with pytest.raises(ConfigError):
        AlertRule().merged({"max_fallback_spread": -1})


def test_discord_format_defaults_to_text_and_validates():
    from tcgwatch.config import ConfigError, parse_config
    assert parse_config({}).discord_format == "text"
    assert parse_config({"notify": {"discord": {"format": "Embeds"}}}).discord_format == "embeds"
    with pytest.raises(ConfigError):
        parse_config({"notify": {"discord": {"format": "fancy"}}})


@pytest.mark.parametrize("bad", [0, False, [], {}, 1, True])
def test_discord_format_non_string_rejected(bad):
    with pytest.raises(ConfigError, match="format"):
        parse_config({"notify": {"discord": {"format": bad}}})

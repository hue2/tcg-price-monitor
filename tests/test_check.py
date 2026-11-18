"""`tcgwatch check` end to end, with Discord stubbed out."""

from datetime import date

import pytest
import yaml

from tcgwatch import cli
from tcgwatch.models import Card, PricePoint
from tcgwatch.store import Store

CARDS = [
    Card(1, "op", 100, "Set A", "Mild", "A-1", "C"),     # +50%
    Card(2, "op", 100, "Set A", "Spike", "A-2", "C"),    # +100%
    Card(3, "op", 200, "Set B", "Other set", "B-1", "C"),  # +100%
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    with Store(db) as s:
        s.upsert_cards(CARDS)
        s.upsert_prices(date(2026, 10, 30), [PricePoint(c.product_id, "Normal", 2.0, None, None, None)
                                             for c in CARDS])
        s.upsert_prices(date(2026, 11, 6), [PricePoint(1, "Normal", 3.0, None, None, None),
                                            PricePoint(2, "Normal", 4.0, None, None, None),
                                            PricePoint(3, "Normal", 4.0, None, None, None)])
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "database": str(db),
        "alert": {"discord_threshold_pct": 80},
        "notify": {"discord": {"webhook_url": "https://discord.test/hook"}},
        "games": {"op": {"name": "OP", "category_id": 68}},
    }))
    posts = []
    monkeypatch.setattr(cli, "send_discord", lambda url, game, rule, movers: posts.append(
        sorted(m.card.name for m in movers)) if movers else None)
    return cfg, db, posts


def run(cfg, *args):
    assert cli.main(["--config", str(cfg), "check", *args]) == 0


def test_only_moves_over_discord_threshold_are_posted(env, capsys):
    cfg, _, posts = env
    run(cfg)
    assert posts == [["Other set", "Spike"]]
    out = capsys.readouterr().out
    assert "3 mover(s)" in out and "posted 2 to Discord" in out


def test_cooldown_prevents_repeat_posts(env):
    cfg, _, posts = env
    run(cfg)
    run(cfg)
    assert posts == [["Other set", "Spike"]]


def test_card_flagged_below_discord_threshold_still_posts_later(env):
    cfg, _, posts = env
    run(cfg, "--discord-threshold", "1000")  # flagged on console only
    assert posts == []
    run(cfg)  # now Discord threshold is 80%: the console cooldown must not block the post
    assert posts == [["Other set", "Spike"]]


def test_preview_never_posts(env):
    cfg, _, posts = env
    run(cfg, "--preview")
    assert posts == []


def test_set_flag_limits_check(env):
    cfg, _, posts = env
    run(cfg, "--set", "200")
    assert posts == [["Other set"]]
    run(cfg, "--set", "set a", "--preview")  # regex on name


def test_failed_post_is_retried_next_run(env, monkeypatch):
    cfg, db, posts = env

    def boom(*a):
        raise OSError("network down")

    monkeypatch.setattr(cli, "send_discord", boom)
    run(cfg)
    with Store(db) as s:
        assert s.recently_alerted(date(2026, 11, 1), discord=True) == set()
    monkeypatch.setattr(cli, "send_discord", lambda url, game, rule, movers: posts.append(
        sorted(m.card.name for m in movers)))
    run(cfg)
    assert posts == [["Other set", "Spike"]]


def test_partial_discord_failure_does_not_repost_delivered_cards(env, monkeypatch):
    from tcgwatch import notify

    cfg, _, _ = env
    delivered = []
    state = {"calls": 0, "fail_on": 3}  # 1 = header, 2 = first card, 3 = second card

    def flaky_post(url, content, **kw):
        state["calls"] += 1
        if state["calls"] == state["fail_on"]:
            raise OSError("Discord 502")
        delivered.append(content)

    monkeypatch.setattr(cli, "send_discord", notify.send_discord)
    monkeypatch.setattr(notify, "post_discord", flaky_post)
    monkeypatch.setattr(notify, "DISCORD_LIMIT", 1)  # one line per message
    run(cfg)  # partial failure
    state["fail_on"] = None  # later posts all succeed
    run(cfg)  # retry

    for name in ("Spike", "Other set"):
        posts_with_card = sum(name in c for c in delivered)
        assert posts_with_card == 1, f"{name} posted {posts_with_card} times"


def test_discord_failure_still_prints_console_and_exits_ok(env, monkeypatch, capsys):
    cfg, _, _ = env

    def down(*a):
        raise OSError("Discord 502")

    monkeypatch.setattr(cli, "send_discord", down)
    assert cli.main(["--config", str(cfg), "check"]) == 0
    captured = capsys.readouterr()
    assert "3 mover(s)" in captured.out  # console results survive the failed post
    assert "Discord post failed: Discord 502" in captured.err
    assert "posted" not in captured.out


def test_discord_failure_records_console_alerts_but_not_discord(env, monkeypatch):
    cfg, db, _ = env

    def down(*a):
        raise OSError("timeout")

    monkeypatch.setattr(cli, "send_discord", down)
    run(cfg)
    with Store(db) as s:
        console = {c for c in s.recently_alerted(date(2026, 11, 1))}
        discord = s.recently_alerted(date(2026, 11, 1), discord=True)
    assert console and discord == set()


def test_non_network_discord_error_is_not_swallowed(env, monkeypatch):
    cfg, _, _ = env

    def bug(*a):
        raise ValueError("bad payload")

    monkeypatch.setattr(cli, "send_discord", bug)
    with pytest.raises(ValueError, match="bad payload"):
        run(cfg)


def test_watchlist_reported_bypassing_filters_and_posted_only_past_threshold(env, capsys):
    cfg, db, posts = env
    with Store(db) as s:  # card 4: rarity-filtered out + below min price; +20% (under 80%)
        s.upsert_cards([Card(4, "op", 100, "Set A", "Quiet", "A-4", "Epic")])
        s.upsert_prices(date(2026, 10, 30), [PricePoint(4, "Normal", 1.0, None, None, None)])
        s.upsert_prices(date(2026, 11, 6), [PricePoint(4, "Normal", 1.2, None, None, None)])
        s.watch(4, "Normal")
        s.watch(1, "Normal")  # +50%, under Discord threshold
    run(cfg)
    out = capsys.readouterr().out
    assert "Watchlist, 2 card(s)" in out and "Quiet" in out and "+20.0%" in out
    assert posts == [["Other set", "Spike"]]  # nothing watched crossed 80%


def test_watched_card_crossing_threshold_posts_with_cooldown(env, capsys):
    cfg, db, posts = env
    with Store(db) as s:  # filtered-out card, +100%
        s.upsert_cards([Card(4, "op", 100, "Set A", "Hot", "A-4", "Epic")])
        s.upsert_prices(date(2026, 10, 30), [PricePoint(4, "Normal", 1.0, None, None, None)])
        s.upsert_prices(date(2026, 11, 6), [PricePoint(4, "Normal", 2.0, None, None, None)])
        s.watch(4, "Normal")
    run(cfg)
    run(cfg)
    assert posts == [["Hot", "Other set", "Spike"]]


def test_watchlist_json_and_quiet(env, tmp_path, capsys):
    import json
    cfg, db, _ = env
    with Store(db) as s:
        s.watch(1, "Normal")
    out = tmp_path / "o.json"
    run(cfg, "--quiet", "--json", str(out))
    rows = json.loads(out.read_text())
    assert capsys.readouterr().out == ""
    assert {r["card"]["name"]: r["watched"] for r in rows}["Mild"] is True


def test_watched_without_baseline_reports_price_in_console_and_json(env, tmp_path, capsys):
    import json
    cfg, db, posts = env
    with Store(db) as s:  # only one snapshot date -> no comparison baseline
        s.upsert_cards([Card(9, "op", 100, "Set A", "NewOnly", None, "C")])
        s.upsert_prices(date(2026, 11, 6), [PricePoint(9, "Normal", 12.34, None, None, None)])
        s.watch(9, "Normal")
    out = tmp_path / "o.json"
    run(cfg, "--preview", "--json", str(out))
    line = next(l for l in capsys.readouterr().out.splitlines() if "NewOnly" in l)
    assert "$   12.34" in line and "n/a" in line and "0.0%" not in line
    row = next(r for r in json.loads(out.read_text()) if r["card"]["name"] == "NewOnly")
    assert row["watched"] is True and row["current_price"] == 12.34 and row["current_date"] == "2026-11-06"
    assert row["price_field"] == "market"
    assert row["baseline_price"] is None and row["baseline_date"] is None and row["pct_change"] is None
    run(cfg)
    assert all("NewOnly" not in names for names in posts)


@pytest.mark.parametrize("bad", ["lots", [], {}])
def test_bad_max_fallback_spread_is_a_clean_cli_error(env, capsys, bad):
    cfg, _, _ = env
    data = yaml.safe_load(cfg.read_text())
    data["alert"]["max_fallback_spread"] = bad
    cfg.write_text(yaml.safe_dump(data))
    assert cli.main(["--config", str(cfg), "check"]) != 0
    assert "max_fallback_spread" in capsys.readouterr().err

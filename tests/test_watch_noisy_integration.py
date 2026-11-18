"""Integration: a watched card with a noisy mid fallback (WL-check x LIQ-1).

Watchlist cards bypass the noisy-fallback guard: a watched noisy card is still reported
in the watchlist section, in --json, and posted to Discord exactly once. An unwatched
noisy card with the same prices is skipped and counted in the documented
"skipped N card(s) with a noisy mid fallback" line. Discord is stubbed; no network.
"""

import json
from datetime import date

import pytest
import yaml

from tcgwatch import cli
from tcgwatch.models import Card, PricePoint
from tcgwatch.store import Store

BASE, CUR = date(2026, 10, 30), date(2026, 11, 6)

# Mid is the only price (market is null), so `mid` is the fallback field.
# Baseline is calm (high/low ~1.2). Current high/low is 20 / 0.5 = 40, above the
# default max_fallback_spread of 10, so the guard drops the card when it is not watched.
CALM_BASE = dict(market=None, low=1.8, mid=2.0, high=2.2)
NOISY_CUR = dict(market=None, low=0.5, mid=4.0, high=20.0)  # +100% on mid, noisy

CARDS = [
    Card(1, "op", 100, "Set A", "Watched Noisy", "W-1", "C"),
    Card(2, "op", 100, "Set A", "Unwatched Noisy", "U-1", "C"),
    Card(3, "op", 100, "Set A", "Calm Mover", "M-1", "C"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    with Store(db) as s:
        s.upsert_cards(CARDS)
        s.upsert_prices(BASE, [PricePoint(1, "Normal", None, 1.8, 2.0, 2.2),
                               PricePoint(2, "Normal", None, 1.8, 2.0, 2.2),
                               PricePoint(3, "Normal", None, 3.9, 4.0, 4.1)])
        # Calm mover: +100% on mid with a calm spread, so the guard does not touch it.
        s.upsert_prices(CUR, [PricePoint(1, "Normal", None, 0.5, 4.0, 20.0),
                              PricePoint(2, "Normal", None, 0.5, 4.0, 20.0),
                              PricePoint(3, "Normal", None, 7.9, 8.0, 8.1)])
        assert s.watch(1, "Normal")  # only card 1 is on the watchlist
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "database": str(db),
        "alert": {"price_fields": ["mid"], "discord_threshold_pct": 80},
        "notify": {"discord": {"webhook_url": "https://discord.test/hook"}},
        "games": {"op": {"name": "OP", "category_id": 68}},
    }))
    posts = []
    monkeypatch.setattr(cli, "send_discord", lambda url, game, rule, movers: posts.append(
        sorted(m.card.name for m in movers)) if movers else None)
    return cfg, posts


def run(cfg, *args):
    assert cli.main(["--config", str(cfg), "check", *args]) == 0


def test_watched_noisy_card_is_reported_and_posted_once(env, tmp_path, capsys):
    cfg, posts = env
    out_json = tmp_path / "o.json"
    run(cfg, "--json", str(out_json))

    stdout = capsys.readouterr().out
    assert "Watchlist, 1 card(s)" in stdout and "Watched Noisy" in stdout  # watchlist ignores the guard
    rows = json.loads(out_json.read_text())
    watched = [r for r in rows if r["card"]["name"] == "Watched Noisy"]
    assert len(watched) == 1 and watched[0]["watched"] is True
    assert watched[0]["price_field"] == "mid" and watched[0]["pct_change"] == pytest.approx(100.0)

    # Discord gets the watched noisy card (and the calm mover), each once.
    assert posts == [["Calm Mover", "Watched Noisy"]]

    # A second run inside the cooldown must not repost the watched card.
    run(cfg)
    assert posts == [["Calm Mover", "Watched Noisy"]]


def test_unwatched_noisy_card_is_skipped_and_counted(env, tmp_path, capsys):
    cfg, posts = env
    out_json = tmp_path / "o.json"
    run(cfg, "--json", str(out_json))

    stdout = capsys.readouterr().out
    # Documented (docs/LIQUIDITY.md, "Watchlist exception"): the skipped count comes from the normal
    # scan alone, so the watched noisy card is counted here too (2 = watched + unwatched), even though
    # the watchlist still reports it. The unwatched card is the one that is dropped for good.
    assert "skipped 2 card(s) with a noisy mid fallback (high/low > 10)" in stdout
    rows = json.loads(out_json.read_text())
    names = {r["card"]["name"] for r in rows}
    assert "Unwatched Noisy" not in names  # the guard drops it from movers and JSON
    assert "Calm Mover" in names
    assert all("Unwatched Noisy" not in p for p in posts)


def test_skipped_count_line_is_absent_when_guard_is_off(env, capsys):
    cfg, posts = env
    data = yaml.safe_load(cfg.read_text())
    data["alert"]["max_fallback_spread"] = 0  # 0 = off
    cfg.write_text(yaml.safe_dump(data))
    run(cfg, "--preview")
    stdout = capsys.readouterr().out
    assert "noisy mid fallback" not in stdout  # nothing skipped when the guard is off
    assert "Unwatched Noisy" in stdout

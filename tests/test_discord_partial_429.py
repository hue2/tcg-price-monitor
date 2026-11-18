"""Realistic partial-failure regression for Discord posting (TEST-2).

Unlike the DISCORD_LIMIT=1 tests, this keeps the production 1900-char limit so the run
produces several real multi-card chunks. A 429 hits a later chunk, the retry gets a 502,
and only the movers in delivered chunks may be recorded as posted. The rest must post on
the next run, exactly once each. Discord is faked at urllib level; sleep is monkeypatched.
"""

import io
import json
import re
import urllib.error
from datetime import date

import pytest
import yaml

from tcgwatch import cli, notify
from tcgwatch.models import Card, PricePoint
from tcgwatch.store import Store

N_CARDS = 60
WEBHOOK = "https://discord.test/hook"


def _cards():
    # Long names and groups so each card line is ~200 chars: about 9 cards per 1900-char post.
    return [
        Card(product_id=i, game_key="op", group_id=100 + (i % 4),
             group_name=f"Booster Pack Series Number {i % 4} Extended Edition",
             name=f"Monkey D. Luffy Gear Five Alternate Art Parallel Variant {i:03d}",
             number=f"OP{i % 4:02d}-{i:03d}", rarity="SEC",
             url=f"https://www.tcgplayer.example/product/{i}")
        for i in range(1, N_CARDS + 1)
    ]


@pytest.fixture
def env(tmp_path):
    cards = _cards()
    db = tmp_path / "t.db"
    with Store(db) as s:
        s.upsert_cards(cards)
        s.upsert_prices(date(2026, 10, 30),
                        [PricePoint(c.product_id, "Normal", 2.0, None, None, None) for c in cards])
        s.upsert_prices(date(2026, 11, 6),
                        [PricePoint(c.product_id, "Normal", 4.0, None, None, None) for c in cards])
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "database": str(db),
        "alert": {"discord_threshold_pct": 80},
        "notify": {"discord": {"webhook_url": WEBHOOK}},
        "games": {"op": {"name": "OP", "category_id": 68}},
    }))
    return cfg, db


class _Ok:
    def close(self):
        pass


def _http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    err = urllib.error.HTTPError(WEBHOOK, code, "failure", headers, io.BytesIO(b""))
    err.close()  # the code under test never reads the body; an open one warns on GC
    return err


class FakeDiscord:
    """Each urlopen call consumes one entry of `plan`: None = accept, an int = HTTP error code."""

    def __init__(self, plan):
        self.plan = list(plan)
        self.attempts = []      # every content string sent, including failed attempts
        self.delivered = []     # content strings Discord accepted

    def __call__(self, req, timeout):
        content = json.loads(req.data)["content"]
        self.attempts.append(content)
        outcome = self.plan.pop(0) if self.plan else None
        if outcome is not None:
            raise _http_error(outcome, retry_after="0.01" if outcome == 429 else None)
        self.delivered.append(content)
        return _Ok()


def _product_ids(post):
    return {int(i) for i in re.findall(r"/product/(\d+)\)", post)}


def _run(cfg):
    assert cli.main(["--config", str(cfg), "check"]) == 0


def test_partial_429_then_502_records_only_delivered_and_next_run_posts_rest(env, monkeypatch, capsys):
    cfg, db = env
    # Real send_discord and post_discord. Posts 1-2 accepted; post 3 gets 429, its retry 502s.
    discord = FakeDiscord(plan=[None, None, 429, 502])
    sleeps = []
    monkeypatch.setattr(notify.urllib.request, "urlopen", discord)
    monkeypatch.setattr(notify.time, "sleep", sleeps.append)

    _run(cfg)

    # Run 1: a real multi-chunk send, failing at a later chunk.
    assert len(discord.attempts) == 4
    assert sleeps == [0.01]                       # one Retry-After wait before the 502
    assert all(len(text) <= notify.DISCORD_LIMIT for text in discord.attempts)
    assert len(discord.delivered) == 2            # chunks 1 and 2 were accepted
    delivered_ids = set().union(*(_product_ids(t) for t in discord.delivered))
    assert len(delivered_ids) > 1
    assert delivered_ids < set(range(1, N_CARDS + 1))  # partial: not everything went out
    assert f"Discord post failed after {len(delivered_ids)} of {N_CARDS} card(s)" in capsys.readouterr().err

    with Store(db) as s:
        marked = {pid for pid, _ in s.recently_alerted(date(2026, 11, 1), discord=True)}
    assert marked == delivered_ids               # undelivered movers are NOT marked as posted

    # Run 2: discord is healthy; the remaining movers post, the delivered ones do not repeat.
    _run(cfg)
    new_posts = discord.delivered[2:]
    assert new_posts
    assert all(len(text) <= notify.DISCORD_LIMIT for text in new_posts)
    posted_again = [pid for text in new_posts for pid in _product_ids(text)]
    assert sorted(posted_again) == sorted(set(range(1, N_CARDS + 1)) - delivered_ids)

    # Across both runs every card reached Discord exactly once.
    every = [pid for text in discord.delivered for pid in _product_ids(text)]
    assert sorted(every) == list(range(1, N_CARDS + 1))

    with Store(db) as s:
        assert {pid for pid, _ in s.recently_alerted(date(2026, 11, 1), discord=True)} == set(range(1, N_CARDS + 1))


def test_429_on_first_chunk_then_502_records_nothing_and_next_run_posts_all(env, monkeypatch):
    cfg, db = env
    discord = FakeDiscord(plan=[429, 502])        # header chunk: 429, retry: 502, no partial success
    monkeypatch.setattr(notify.urllib.request, "urlopen", discord)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)

    _run(cfg)
    assert discord.delivered == []
    with Store(db) as s:
        assert s.recently_alerted(date(2026, 11, 1), discord=True) == set()

    _run(cfg)
    every = [pid for text in discord.delivered for pid in _product_ids(text)]
    assert sorted(every) == list(range(1, N_CARDS + 1))

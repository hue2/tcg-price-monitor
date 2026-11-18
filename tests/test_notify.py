"""Discord message formatting, chunking and retry behaviour (no network)."""

import io
import json
import urllib.error

import pytest

from tcgwatch import notify
from tcgwatch.config import AlertRule, GameConfig
from tcgwatch.models import Card, Mover

GAME = GameConfig(key="op", name="One Piece", alert=AlertRule())


def mover(i: int, pct: float = 50.0) -> Mover:
    card = Card(product_id=i, game_key="op", group_id=1, group_name="Set A", name=f"Card {i}",
                number=f"A-{i}", rarity="R", url=f"https://example.test/{i}")
    return Mover(card=card, printing="Normal", price_field="market", baseline_date="2026-10-30",
                 current_date="2026-11-06", baseline_price=1.0, current_price=1.0 + pct / 100,
                 pct_change=pct)


def test_discord_line_links_name_and_shows_prices():
    line = notify._discord_line(mover(7, pct=42.5))
    assert line == "`+42.5%` $1.00 -> **$1.43** [Card 7](https://example.test/7) A-7 [R, Normal] - Set A"


def test_send_discord_with_no_movers_posts_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "post_discord", lambda *a, **k: calls.append(a))
    notify.send_discord("https://discord.test/hook", GAME, GAME.alert, [])
    assert calls == []


def test_send_discord_splits_long_output_and_keeps_every_card(monkeypatch):
    posted = []
    monkeypatch.setattr(notify, "post_discord", lambda url, content, **k: posted.append(content))
    movers = [mover(i) for i in range(300)]
    notify.send_discord("https://discord.test/hook", GAME, GAME.alert, movers)

    assert len(posted) > 1
    assert all(len(chunk) <= notify.DISCORD_LIMIT < 2000 for chunk in posted)
    body = "\n".join(posted)
    for m in movers:
        assert f"Card {m.card.product_id}]" in body  # each card appears exactly once
    assert body.count("Card ") == len(movers)


def test_send_discord_header_states_the_discord_threshold(monkeypatch):
    posted = []
    monkeypatch.setattr(notify, "post_discord", lambda url, content, **k: posted.append(content))
    rule = AlertRule(threshold_pct=30, discord_threshold_pct=75, window_days=7)
    notify.send_discord("https://discord.test/hook", GAME, rule, [mover(1, pct=80.0)])
    assert posted[0].startswith("**One Piece**: 1 card(s) moved +75% or more over 7d")


def _http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    err = urllib.error.HTTPError("https://discord.test", code, "x", headers, io.BytesIO(b""))
    err.close()  # the code under test never reads the body; an open one warns on GC
    return err


class _Ok:
    def close(self):
        pass


def test_post_discord_waits_out_rate_limit_then_succeeds(monkeypatch):
    attempts, sleeps = [], []

    def fake_urlopen(req, timeout):
        attempts.append(req)
        if len(attempts) == 1:
            raise _http_error(429, retry_after="2.5")
        return _Ok()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify.time, "sleep", sleeps.append)
    notify.post_discord("https://discord.test/hook", "hi")
    assert len(attempts) == 2
    assert sleeps == [2.5]


def test_post_discord_closes_error_when_retry_after_is_malformed(monkeypatch):
    err = urllib.error.HTTPError("https://discord.test", 429, "x", {"Retry-After": "soon"},
                                 io.BytesIO(b""))
    assert not err.fp.closed

    def fake_urlopen(req, timeout):
        raise err

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    with pytest.raises(ValueError):  # behavior unchanged: the parse error still propagates
        notify.post_discord("https://discord.test/hook", "hi")
    assert err.fp is None or err.fp.closed


def test_post_discord_gives_up_after_retries_on_429(monkeypatch):
    attempts = []

    def fake_urlopen(req, timeout):
        attempts.append(1)
        raise _http_error(429)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        notify.post_discord("https://discord.test/hook", "hi", retries=2)
    assert len(attempts) == 3  # first try + 2 retries


def test_post_discord_does_not_retry_other_http_errors(monkeypatch):
    attempts = []

    def fake_urlopen(req, timeout):
        attempts.append(1)
        raise _http_error(500)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        notify.post_discord("https://discord.test/hook", "hi")
    assert len(attempts) == 1


def test_post_discord_sends_no_mentions(monkeypatch):
    bodies = []

    def fake_urlopen(req, timeout):
        bodies.append(req.data)
        return _Ok()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    notify.post_discord("https://discord.test/hook", "@everyone hi")
    assert json.loads(bodies[0])["allowed_mentions"] == {"parse": []}


def _capture_chunks(monkeypatch, game, movers):
    posted = []
    monkeypatch.setattr(notify, "post_discord", lambda url, content, **k: posted.append(content))
    notify.send_discord("https://discord.test/hook", game, game.alert, movers)
    return posted


def test_discord_limit_is_1900_chars():
    assert notify.DISCORD_LIMIT == 1900


def test_chunk_boundary_at_1900_chars(monkeypatch):
    """A message of exactly 1900 chars stays in one post; one more char forces a split."""
    movers = [mover(1)]
    # Find a game-name length whose single-post message is exactly DISCORD_LIMIT long.
    monkeypatch.setattr(notify, "DISCORD_LIMIT", 10**9)
    pad = next(p for p in range(0, 3000)
               if len(_capture_chunks(monkeypatch, GameConfig(
                   key="op", name="N" * p, alert=AlertRule()), movers)[0]) == 1900)
    monkeypatch.setattr(notify, "DISCORD_LIMIT", 1900)

    exact = GameConfig(key="op", name="N" * pad, alert=AlertRule())
    one = _capture_chunks(monkeypatch, exact, movers)
    assert len(one) == 1 and len(one[0]) == 1900

    over = GameConfig(key="op", name="N" * (pad + 1), alert=AlertRule())
    two = _capture_chunks(monkeypatch, over, movers)
    assert len(two) == 2
    header, line = two
    assert header.endswith("or more over 7d") and "[Card 1]" not in header  # header alone, first post
    assert line.startswith("`+50.0%`") and "[Card 1]" in line
    assert len(header) <= 1900 and len(line) <= 1900


def test_split_never_leaves_a_chunk_over_the_limit_and_keeps_order(monkeypatch):
    game = GameConfig(key="op", name="One Piece", alert=AlertRule())
    movers = [mover(i) for i in range(1, 120)]
    posted = _capture_chunks(monkeypatch, game, movers)
    assert len(posted) > 1
    assert all(len(c) <= notify.DISCORD_LIMIT for c in posted)
    text = "\n".join(posted)
    positions = [text.index(f"[Card {i}]") for i in range(1, 120)]
    assert positions == sorted(positions)
    assert all(not c.startswith("\n") and not c.endswith("\n") for c in posted)


def test_send_discord_partial_failure_reports_delivered_movers(monkeypatch):
    movers = [mover(i, pct=90.0) for i in range(1, 4)]
    calls = []

    def flaky(url, content, **kw):
        calls.append(content)
        if len(calls) == 3:  # header, card 1 ok; card 2 fails
            raise OSError("502")

    monkeypatch.setattr(notify, "post_discord", flaky)
    monkeypatch.setattr(notify, "DISCORD_LIMIT", 1)
    with pytest.raises(notify.DiscordPartialError) as ei:
        notify.send_discord("https://discord.test/hook", GAME, GAME.alert, movers)
    assert ei.value.posted == movers[:1]


def test_send_discord_first_chunk_failure_is_plain_oserror(monkeypatch):
    def down(url, content, **kw):
        raise OSError("down")

    monkeypatch.setattr(notify, "post_discord", down)
    with pytest.raises(OSError) as ei:
        notify.send_discord("https://discord.test/hook", GAME, GAME.alert, [mover(1, pct=90.0)])
    assert not isinstance(ei.value, notify.DiscordPartialError)


def _mover(name, url):
    card = Card(1, "g", 1, "Set", name, "001", "Rare", url)
    return Mover(card, "Normal", "market", "a", "b", 1.0, 2.0, 100.0)


def test_discord_line_escapes_brackets_and_parens():
    line = notify._discord_line(_mover("Foo [Alt] (Promo)", "https://x.test/p"))
    assert "[Foo \\[Alt\\] \\(Promo\\)](https://x.test/p)" in line


def test_discord_line_encodes_paren_in_url():
    line = notify._discord_line(_mover("Foo", "https://x.test/a(b)"))
    assert "(https://x.test/a(b%29)" in line


def test_discord_line_no_url_is_plain():
    assert "Foo [Alt]" in notify._discord_line(_mover("Foo [Alt]", None))


# ---- B13: discord.format = embeds ----

def _with_card(m, **kw):
    import dataclasses
    return dataclasses.replace(m, card=dataclasses.replace(m.card, **kw))


def _capture_embeds(monkeypatch, movers, fail_on=None):
    sent = []

    def fake(url, content, *, retries=3, embeds=None):
        if fail_on is not None and len(sent) + 1 == fail_on:
            raise OSError("502")
        sent.append((content, embeds))

    monkeypatch.setattr(notify, "post_discord", fake)
    notify.send_discord("https://discord.test/hook", GAME, GAME.alert, movers, fmt="embeds")
    return sent


def test_embed_has_name_link_price_change_and_thumbnail(monkeypatch):
    m = _with_card(mover(1, pct=50.0), image_url="https://img.test/1.jpg")
    (content, embeds), = _capture_embeds(monkeypatch, [m])
    assert "One Piece" in content
    e, = embeds
    assert e["title"] == "Card 1 A-1" and e["url"] == "https://example.test/1"
    assert "+50.0%" in e["description"] and "$1.00 -> **$1.50**" in e["description"]
    assert e["thumbnail"] == {"url": "https://img.test/1.jpg"}


def test_embed_omits_image_and_url_when_absent(monkeypatch):
    m = _with_card(mover(1), url=None)
    (_, (e,)), = _capture_embeds(monkeypatch, [m])
    assert "thumbnail" not in e and "url" not in e


def test_embeds_max_ten_per_message(monkeypatch):
    sent = _capture_embeds(monkeypatch, [mover(i) for i in range(1, 26)])
    assert [len(e) for _, e in sent] == [10, 10, 5]
    assert sent[0][0] and sent[1][0] == ""  # header only on the first message


def test_embeds_respect_6000_char_total(monkeypatch):
    movers = [_with_card(mover(i), name="x" * 1200, group_name="g" * 3000) for i in range(1, 8)]
    sent = _capture_embeds(monkeypatch, movers)
    assert len(sent) > 1
    for _, embeds in sent:
        assert sum(len(e["title"]) + len(e["description"]) for e in embeds) <= 6000
        assert all(len(e["title"]) <= 256 for e in embeds)


def test_embeds_partial_failure_reports_only_delivered(monkeypatch):
    movers = [mover(i) for i in range(1, 26)]
    with pytest.raises(notify.DiscordPartialError) as ei:
        _capture_embeds(monkeypatch, movers, fail_on=2)
    assert ei.value.posted == movers[:10]


def test_embeds_first_message_failure_is_plain_oserror(monkeypatch):
    with pytest.raises(OSError) as ei:
        _capture_embeds(monkeypatch, [mover(1)], fail_on=1)
    assert not isinstance(ei.value, notify.DiscordPartialError)


def test_post_discord_embeds_payload_and_no_mentions(monkeypatch):
    seen = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=0: seen.append(json.loads(req.data)) or io.BytesIO(b""))
    notify.post_discord("https://discord.test/hook", "h", embeds=[{"title": "t", "description": "d"}])
    assert seen[0]["embeds"] and seen[0]["allowed_mentions"] == {"parse": []}


def test_text_format_payload_has_no_embeds_key(monkeypatch):
    seen = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=0: seen.append(json.loads(req.data)) or io.BytesIO(b""))
    notify.post_discord("https://discord.test/hook", "hi")
    assert seen[0] == {"content": "hi", "allowed_mentions": {"parse": []}}


def test_send_discord_truncates_oversized_card_line_and_header(monkeypatch):
    posted = []
    monkeypatch.setattr(notify, "post_discord", lambda url, content, **kw: posted.append(content))
    big = mover(1, pct=90.0)
    object.__setattr__(big.card, "name", "X" * 2000)
    game = GameConfig(key="op", name="G" * 2500, alert=AlertRule())
    notify.send_discord("https://discord.test/hook", game, game.alert, [big, mover(2, pct=90.0)])
    assert posted and all(len(c) <= 2000 for c in posted)
    assert "Card 2" in "\n".join(posted)
    assert "\u2026" in "\n".join(posted)


def test_fit_does_not_cut_markdown_link():
    line = "`+9%` " + "[" + "A\\(" * 900 + "](https://x.test/p) tail"
    out = notify._fit(line)
    assert len(out) <= 1999 and out.endswith("](https://x.test/p)\u2026")
    assert out.count("[") - out.count("\\[") == 1 and out.count("](") == 1


def test_embeds_format_also_truncates_header(monkeypatch):
    posted = []
    monkeypatch.setattr(notify, "post_discord", lambda url, content, **kw: posted.append(content))
    game = GameConfig(key="op", name="G" * 2500, alert=AlertRule())
    notify.send_discord("https://discord.test/hook", game, game.alert, [mover(1, pct=90.0)], fmt="embeds")
    assert posted and len(posted[0]) <= 2000 and posted[0].endswith("\u2026")

"""Report movers: console table, Discord webhook, JSON file."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

from .config import AlertRule, GameConfig
from .models import Mover, Unpaired
from .textsafe import term_safe

DISCORD_LIMIT = 1900  # packing target; Discord caps messages at 2000 chars
DISCORD_HARD_LIMIT = 2000


def _line(m: Mover) -> str:
    c = m.card
    num = f" {term_safe(c.number)}" if c.number else ""
    return (f"{m.pct_change:+7.1f}%  ${m.baseline_price:>8.2f} -> ${m.current_price:>8.2f}  "
            f"{term_safe(c.name)}{num} [{term_safe(c.rarity)}, {term_safe(m.printing)}] - {term_safe(c.group_name)}")


def describe_rule(rule: AlertRule) -> str:
    sign = {"up": "+", "down": "-", "both": "+/-"}[rule.direction]
    return (f"{sign}{rule.threshold_pct:g}% over {rule.window_days}d "
            f"(min price ${rule.min_price:.2f}, min change ${rule.min_abs_change:.2f})")


def print_console(game: GameConfig, rule: AlertRule, movers: list[Mover]) -> None:
    print(f"\n== {term_safe(game.name)}: {len(movers)} mover(s), rule {describe_rule(rule)}")
    for m in movers:
        print("  " + _line(m))


def _escape_link_text(text: str) -> str:
    """Backslash-escape characters that would end or break a Discord markdown link label."""
    for ch in ("\\", "[", "]", "(", ")"):
        text = text.replace(ch, "\\" + ch)
    return text


def _discord_line(m: Mover) -> str:
    c = m.card
    num = f" {c.number}" if c.number else ""
    name = (f"[{_escape_link_text(c.name)}]({c.url.replace(')', '%29')})"
            if c.url else c.name)
    return (f"`{m.pct_change:+.1f}%` ${m.baseline_price:.2f} -> **${m.current_price:.2f}** "
            f"{name}{num} [{c.rarity}, {m.printing}] - {c.group_name}")


_LINK_RE = re.compile(r"\[((?:\\.|[^\]\\])*)\]\(([^)]*)\)")


def _fit(text: str) -> str:
    """Truncate one line (with an ellipsis) so a post holding only it stays under Discord's cap.

    Never cuts a markdown link mid-way: if the cut lands inside ``[label](url)`` the label is
    shortened (not splitting an escape pair) so the whole link survives; if even that cannot
    fit, the link is dropped."""
    cap = DISCORD_HARD_LIMIT - 1
    if len(text) <= cap:
        return text
    room = cap - 1  # space for the ellipsis
    for m in _LINK_RE.finditer(text):
        if m.start() >= room:
            break
        if m.end() <= room:
            continue
        label, url = m.group(1), m.group(2)
        keep = room - m.start() - len(url) - 4
        if keep > 0:
            cut = label[:keep]
            if (len(cut) - len(cut.rstrip("\\"))) % 2:
                cut = cut[:-1]  # don't leave a dangling escape backslash
            return f"{text[:m.start()]}[{cut}]({url})\u2026"
        return text[:m.start()].rstrip() + "\u2026"
    return text[:room] + "\u2026"


EMBED_MAX_PER_MESSAGE = 10
EMBED_TOTAL_LIMIT = 6000  # title + description summed over a message's embeds
EMBED_TITLE_LIMIT = 256
EMBED_DESC_LIMIT = 4096


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def _discord_embed(m: Mover) -> dict:
    """One embed per card: title (name + number, linked), price change, thumbnail if any."""
    c = m.card
    num = f" {c.number}" if c.number else ""
    embed: dict = {
        "title": _clip(f"{c.name}{num}", EMBED_TITLE_LIMIT),
        "description": _clip(
            f"`{m.pct_change:+.1f}%` ${m.baseline_price:.2f} -> **${m.current_price:.2f}**\n"
            f"{c.rarity}, {m.printing} - {c.group_name}", EMBED_DESC_LIMIT),
        "color": 0x2ECC71 if m.pct_change >= 0 else 0xE74C3C,
    }
    if c.url:
        embed["url"] = c.url
    if c.image_url:
        embed["thumbnail"] = {"url": c.image_url}
    return embed


def _embed_size(e: dict) -> int:
    return len(e["title"]) + len(e["description"])


def post_discord(webhook_url: str, content: str, *, retries: int = 3,
                 embeds: list[dict] | None = None) -> None:
    """POST one message, waiting out Discord rate limits (HTTP 429)."""
    payload: dict = {"content": content, "allowed_mentions": {"parse": []}}
    if embeds:
        payload["embeds"] = embeds
    body = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            webhook_url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "tcg-price-watch"})
        try:
            urllib.request.urlopen(req, timeout=15).close()
            return
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == retries:
                raise
            try:
                wait = float(e.headers.get("Retry-After") or 1.0)
            finally:
                e.close()  # also when the header is malformed (ValueError still propagates)
            time.sleep(wait)


class DiscordPartialError(OSError):
    """A chunk failed after earlier ones were delivered; `posted` are the movers already sent."""

    def __init__(self, cause: BaseException, posted: list[Mover]):
        super().__init__(str(cause))
        self.posted = posted


def send_discord(webhook_url: str, game: GameConfig, rule: AlertRule, movers: list[Mover],
                 fmt: str = "text") -> None:
    """Post movers in chunks (fmt "text": lines; "embeds": one embed per card, <=10 per message). If a later chunk fails, raise DiscordPartialError listing the
    movers whose chunks were delivered so callers record only those."""
    if not movers:
        return
    sign = {"up": "+", "down": "-", "both": "+/-"}[rule.direction]
    header = (f"**{game.name}**: {len(movers)} card(s) moved {sign}{rule.effective_discord_threshold:g}% "
              f"or more over {rule.window_days}d")
    header = _fit(header)
    if fmt == "embeds":
        _send_embeds(webhook_url, header, movers)
        return
    chunks: list[tuple[str, list[Mover]]] = []
    current, members = header, []
    for m in movers:
        line = _fit(_discord_line(m))
        if len(current) + len(line) + 1 > DISCORD_LIMIT:
            chunks.append((current, members))
            current, members = "", []
        current += "\n" + line
        members.append(m)
    chunks.append((current, members))
    posted: list[Mover] = []
    for text, members in chunks:
        try:
            post_discord(webhook_url, text.strip())
        except OSError as e:
            if posted:
                raise DiscordPartialError(e, posted) from e
            raise
        posted.extend(members)


def _send_embeds(webhook_url: str, header: str, movers: list[Mover]) -> None:
    messages: list[tuple[list[dict], list[Mover]]] = []
    embeds: list[dict] = []
    members: list[Mover] = []
    size = 0
    for m in movers:
        e = _discord_embed(m)
        n = _embed_size(e)
        if embeds and (len(embeds) >= EMBED_MAX_PER_MESSAGE or size + n > EMBED_TOTAL_LIMIT):
            messages.append((embeds, members))
            embeds, members, size = [], [], 0
        embeds.append(e)
        members.append(m)
        size += n
    messages.append((embeds, members))
    posted: list[Mover] = []
    for i, (embeds, members) in enumerate(messages):
        try:
            post_discord(webhook_url, header if i == 0 else "", embeds=embeds)
        except OSError as e:
            if posted:
                raise DiscordPartialError(e, posted) from e
            raise
        posted.extend(members)


def print_watchlist(game: GameConfig, rule: AlertRule, movers: list[Mover], missing: list[Unpaired]) -> None:
    """Every watched card with its current price and change over the window (no filters)."""
    print(f"\n== {term_safe(game.name)}: Watchlist, {len(movers) + len(missing)} card(s), change over {rule.window_days}d")
    for m in movers:
        print("  " + _line(m))
    for u in missing:
        c = u.card
        num = f" {term_safe(c.number)}" if c.number else ""
        now = f"${u.current_price:>8.2f}" if u.current_price is not None else "     n/a"
        print(f"  {'n/a':>8}  no price {rule.window_days}d ago  now {now}  "
              f"{term_safe(c.name)}{num} [{term_safe(c.rarity)}, {term_safe(u.printing)}] - {term_safe(c.group_name)}")


def write_json(path: Path, movers: list[Mover], watchlist: list[Mover] | None = None,
               unpaired: list[Unpaired] | None = None) -> None:
    """Movers as a list; watched cards are marked `"watched": true` and added if not already movers.
    Watched cards with no baseline are included with null baseline/change fields."""
    path.parent.mkdir(parents=True, exist_ok=True)
    watch = {(m.card.product_id, m.printing) for m in watchlist or []}
    rows = [{**asdict(m), "watched": (m.card.product_id, m.printing) in watch} for m in movers]
    listed = {(m.card.product_id, m.printing) for m in movers}
    rows += [{**asdict(m), "watched": True} for m in watchlist or [] if (m.card.product_id, m.printing) not in listed]
    for u in unpaired or []:
        rows.append({"card": asdict(u.card), "printing": u.printing, "price_field": u.price_field,
                     "baseline_date": None, "current_date": u.current_date,
                     "baseline_price": None, "current_price": u.current_price,
                     "pct_change": None, "watched": True})
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

"""TCGCSV (https://tcgcsv.com): a free daily mirror of TCGplayer catalog and prices."""

from __future__ import annotations

import json
import re
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from ..config import GameConfig
from ..models import Card, PricePoint

BASE_URL = "https://tcgcsv.com"
# Entries inside a daily archive: {date}/{categoryId}/{groupId}/prices
_ARCHIVE_ENTRY = re.compile(r"^\d{4}-\d{2}-\d{2}/(\d+)/\d+/prices$")


class TcgCsvError(RuntimeError):
    pass


# Retry-After policy: a server-requested wait is clamped to MAX_RETRY_WAIT per retry, and the
# total time spent sleeping on retries for one request is bounded by RETRY_BUDGET. If the next
# wait would exceed the remaining budget we stop early and raise rather than retry too soon.
MAX_RETRY_WAIT = 60.0
RETRY_BUDGET = 120.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _retry_after(value: str | None) -> float | None:
    """Parse Retry-After (delta-seconds or HTTP-date) to seconds; None if absent/invalid."""
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - _now()).total_seconds())


def _price_point(r: dict[str, Any]) -> PricePoint:
    return PricePoint(product_id=r["productId"], printing=r["subTypeName"],
                      market=r.get("marketPrice"), low=r.get("lowPrice"),
                      mid=r.get("midPrice"), high=r.get("highPrice"))


class TcgCsvSource:
    def __init__(self, user_agent: str, request_delay_seconds: float = 0.2, timeout: float = 30.0,
                 max_attempts: int = 4, backoff_seconds: float = 1.0):
        self.user_agent = user_agent
        self.max_attempts = max(1, max_attempts)
        self.backoff = backoff_seconds
        self.delay = request_delay_seconds
        self.timeout = timeout
        self._categories: list[dict[str, Any]] | None = None

    def _get(self, path: str) -> bytes:
        req = urllib.request.Request(f"{BASE_URL}{path}", headers={"User-Agent": self.user_agent})
        spent = 0.0
        for attempt in range(1, self.max_attempts + 1):
            wait = self.backoff * 2 ** (attempt - 1)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                break
            except urllib.error.HTTPError as e:
                err: BaseException = e
                # 404 and other 4xx are final (archives rely on 404); only 429/5xx retry.
                if e.code != 429 and e.code < 500:
                    raise
                if attempt == self.max_attempts:
                    raise
                hinted = _retry_after(e.headers.get("Retry-After") if e.headers else None)
                if hinted is not None:
                    wait = min(hinted, MAX_RETRY_WAIT)
                e.close()  # retrying: release the response socket (err keeps code/headers)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                err = e
                if attempt == self.max_attempts:
                    raise
            # Budget applies to every retryable failure; re-raises the original exception.
            if spent + wait > RETRY_BUDGET:
                raise err
            spent += wait
            time.sleep(wait)
        time.sleep(self.delay)
        return body

    def _results(self, path: str) -> list[dict[str, Any]]:
        payload = json.loads(self._get(path))
        if not payload.get("success", False):
            raise TcgCsvError(f"{path}: {payload.get('errors')}")
        return payload["results"]

    def published_at(self) -> datetime:
        """Full timestamp from last-updated.txt; changes on every republish, even same-day."""
        return datetime.fromisoformat(self._get("/last-updated.txt").decode().strip())

    def snapshot_date(self) -> date:
        return self.published_at().date()

    def resolve_category(self, game: GameConfig) -> int:
        if game.category_id is not None:
            return game.category_id
        if self._categories is None:
            self._categories = self._results("/tcgplayer/categories")
        wanted = game.category_name.casefold()
        matches = [c for c in self._categories
                   if wanted in (c["name"].casefold(), (c.get("displayName") or "").casefold())]
        if not matches:
            raise TcgCsvError(f"no TCGplayer category named {game.category_name!r}")
        return matches[0]["categoryId"]

    def fetch_singles(self, game: GameConfig) -> Iterator[tuple[list[Card], list[PricePoint]]]:
        category = self.resolve_category(game)
        for group in self._results(f"/tcgplayer/{category}/groups"):
            gid = group["groupId"]
            cards = {}
            for p in self._results(f"/tcgplayer/{category}/{gid}/products"):
                ext = {e["name"]: e["value"] for e in p.get("extendedData", [])}
                if not ext.get("Rarity"):  # sealed product has no rarity
                    continue
                cards[p["productId"]] = Card(
                    product_id=p["productId"], game_key=game.key, group_id=gid,
                    group_name=group["name"], name=p["name"], number=ext.get("Number"),
                    rarity=ext["Rarity"], url=p.get("url"), image_url=p.get("imageUrl"),
                )
            if not cards:
                continue
            prices = [_price_point(r) for r in self._results(f"/tcgplayer/{category}/{gid}/prices")
                      if r["productId"] in cards]
            yield list(cards.values()), prices

    def archive_prices(self, day: date, category_ids: set[int]) -> dict[int, list[PricePoint]] | None:
        """Prices for a past day from TCGCSV's daily archive, per category. None if no archive.

        Archives hold prices only (no catalog), so callers match them to stored cards.
        """
        import py7zr  # only needed for backfill

        try:
            data = self._get(f"/archive/tcgplayer/prices-{day.isoformat()}.ppmd.7z")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                e.close()
                return None
            raise
        out: dict[int, list[PricePoint]] = {c: [] for c in category_ids}
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "prices.7z"
            archive.write_bytes(data)
            with py7zr.SevenZipFile(archive) as z:
                # Only extract names matching the strict pattern, so nothing lands outside tmp.
                targets = [n for n in z.getnames()
                           if (m := _ARCHIVE_ENTRY.match(n)) and int(m[1]) in category_ids]
                if targets:
                    z.extract(path=Path(tmp) / "x", targets=targets)
            for name in targets:
                payload = json.loads((Path(tmp) / "x" / name).read_bytes())
                out[int(_ARCHIVE_ENTRY.match(name)[1])].extend(
                    _price_point(r) for r in payload.get("results", []))
        return out

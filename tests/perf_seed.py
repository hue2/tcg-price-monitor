"""Synthetic large-database seeder for the slow performance benchmarks (PERF-0).

Builds a realistic DB shape: 3 games, `products_per_game` cards each with two printings
(Normal, Foil), and `days` daily price snapshots per printing. At the defaults that is
30,000 printings and 5.4 million price rows. Deterministic for a given seed.

Usage (standalone):  python tests/perf_seed.py OUTDIR [--products-per-game N] [--days N]
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

from tcgwatch.config import load_config
from tcgwatch.models import Card, PricePoint
from tcgwatch.store import Store

# (game key, display name, TCGCSV category id, rarity values)
GAMES = [
    ("op", "One Piece Card Game", 68, ("C", "UC", "R", "SR")),
    ("riftbound", "Riftbound", 89, ("Common", "Uncommon", "Rare")),
    ("pokemon", "Pokemon", 3, ("Common", "Uncommon", "Rare", "Holo Rare")),
]
PRINTINGS = ("Normal", "Foil")
SETS_PER_GAME = 40
END_DATE = date(2026, 11, 6)
SEED = 20261106
WATCH_COUNT = 300  # series placed on the watchlist, spread across the games (N+1 cost scales with this)

CONFIG_TEMPLATE = """\
database: {db}
retention_days: 0
alert:
  window: 7d
  threshold_pct: 30
  direction: both
  min_price: 0.50
  price_fields: [market, mid]
  max_fallback_spread: 10
notify:
  console: true
games:
{games}"""


def write_config(out_dir: Path, db_path: Path) -> Path:
    """Write a config.yaml for the seeded DB (no webhook, all three games enabled).

    `database` is written as an absolute path. load_config resolves a relative `database`
    against the config's own folder, so a relative `db_path` (e.g. OUTDIR/tcgwatch.db with a
    relative OUTDIR) would be doubled to OUTDIR/OUTDIR/tcgwatch.db and point at an empty DB.
    """
    games = "\n".join(f"  {key}:\n    name: {name}\n    enabled: true\n    source: tcgcsv\n"
                      f"    category_id: {cat}\n" for key, name, cat, _ in GAMES)
    path = out_dir / "config.yaml"
    # json.dumps gives a double-quoted YAML scalar, so any path characters stay literal.
    db_text = json.dumps(str(db_path.resolve()))
    path.write_text(CONFIG_TEMPLATE.format(db=db_text, games=games), encoding="utf-8")
    return path


def assert_config_targets(cfg_path: Path, db_path: Path) -> None:
    """Raise AssertionError unless the config loads and points at exactly `db_path`."""
    cfg = load_config(cfg_path)
    expected = db_path.resolve()
    assert cfg.database.resolve() == expected, (
        f"{cfg_path} targets {cfg.database.resolve()}, not the seeded {expected}")
    assert expected.exists(), f"seeded DB {expected} does not exist"


def seed(db_path: Path, *, products_per_game: int = 5000, days: int = 180, end: date = END_DATE,
         watch: int = WATCH_COUNT, seed: int = SEED) -> dict[str, int]:
    """Create and fill `db_path` (must not exist). Returns row counts for the report."""
    if db_path.exists():
        raise FileExistsError(f"refusing to overwrite {db_path}")
    rng = random.Random(seed)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"cards": 0, "printings": 0, "price_rows": 0, "watched": 0, "days": days}
    with Store(db_path) as store:
        store.conn.execute("PRAGMA synchronous=OFF")  # throwaway benchmark DB: durability not needed
        series: list[tuple[int, str, float]] = []  # (product_id, printing, starting price)
        for gi, (key, _name, _cat, rarities) in enumerate(GAMES):
            cards = []
            for i in range(products_per_game):
                pid = (gi + 1) * 1_000_000 + i
                group = gi * 1000 + i % SETS_PER_GAME
                cards.append(Card(pid, key, group, f"{key.title()} Set {i % SETS_PER_GAME:02d}",
                                  f"Card {i:05d}", f"{i % SETS_PER_GAME:02d}-{i:03d}",
                                  rng.choice(rarities), f"https://example.test/{key}/{pid}"))
                base = round(rng.uniform(0.2, 150.0), 2)
                for printing in PRINTINGS:
                    series.append((pid, printing, base * (1.8 if printing == "Foil" else 1.0)))
            store.upsert_cards(cards)
            counts["cards"] += len(cards)
        counts["printings"] = len(series)

        # Daily random walk per series; a few days/fields are NULL so fallbacks get exercised.
        prices = [start for _, _, start in series]
        for offset in range(days - 1, -1, -1):
            day = end - timedelta(days=offset)
            points = []
            for idx, (pid, printing, _) in enumerate(series):
                prices[idx] = max(0.01, round(prices[idx] * (1 + rng.gauss(0, 0.03)), 2))
                p = prices[idx]
                market = None if rng.random() < 0.03 else p
                mid = None if rng.random() < 0.02 else round(p * 1.02, 2)
                low = round(p * 0.9, 2) if rng.random() > 0.1 else None
                high = round(p * 1.3, 2)
                points.append(PricePoint(pid, printing, market, low, mid, high))
            store.upsert_prices(day, points)
            counts["price_rows"] += len(points)

        if watch > 0:
            step = max(1, len(series) // watch)
            for pid, printing, _ in series[::step][:watch]:
                counts["watched"] += store.watch(pid, printing)
        store.conn.execute("PRAGMA optimize")
        store.conn.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("outdir", type=Path, help="directory to create tcgwatch.db and config.yaml in")
    ap.add_argument("--products-per-game", type=int, default=5000)
    ap.add_argument("--days", type=int, default=180)
    args = ap.parse_args(argv)
    db = args.outdir / "tcgwatch.db"
    counts = seed(db, products_per_game=args.products_per_game, days=args.days)
    cfg = write_config(args.outdir, db)
    print(f"seeded {db} {counts}\nconfig {cfg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

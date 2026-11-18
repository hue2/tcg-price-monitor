"""SQLite persistence for cards, daily price snapshots, and the alert log."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .models import Card, Mover, Observation, PricePoint, Unpaired

BASELINE_GRACE_DAYS = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    product_id  INTEGER PRIMARY KEY,
    game_key    TEXT NOT NULL,
    group_id    INTEGER NOT NULL,
    group_name  TEXT NOT NULL,
    name        TEXT NOT NULL,
    number      TEXT,
    rarity      TEXT NOT NULL,
    url         TEXT,
    image_url   TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS cards_game ON cards(game_key);

CREATE TABLE IF NOT EXISTS prices (
    product_id    INTEGER NOT NULL REFERENCES cards(product_id),
    printing      TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    market REAL, low REAL, mid REAL, high REAL,
    PRIMARY KEY (product_id, printing, snapshot_date)
);
CREATE INDEX IF NOT EXISTS prices_date ON prices(snapshot_date);

CREATE TABLE IF NOT EXISTS alerts (
    product_id  INTEGER NOT NULL,
    printing    TEXT NOT NULL,
    alerted_on  TEXT NOT NULL,
    window_days INTEGER NOT NULL,
    price_field TEXT NOT NULL,
    baseline    REAL NOT NULL,
    current     REAL NOT NULL,
    pct_change  REAL NOT NULL,
    discord     INTEGER NOT NULL DEFAULT 0  -- 1 if posted to Discord
);
CREATE INDEX IF NOT EXISTS alerts_card ON alerts(product_id, printing, alerted_on);

CREATE TABLE IF NOT EXISTS watchlist (
    product_id INTEGER NOT NULL,
    printing   TEXT NOT NULL,
    added_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (product_id, printing)
);

-- Ops/UI status only (last success, healthcheck, retry decision). Never infer
-- data completeness from this table; use the prices table.
CREATE TABLE IF NOT EXISTS job_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    started      TEXT NOT NULL,
    finished     TEXT,
    ok           INTEGER,
    dataset_date TEXT,
    note         TEXT,
    game         TEXT
);
CREATE INDEX IF NOT EXISTS job_runs_kind ON job_runs(kind, started);
"""

BUSY_TIMEOUT_MS = 30000

# Columns added after v0.1, applied to existing databases on open.
MIGRATIONS = {
    ("alerts", "discord"): "ALTER TABLE alerts ADD COLUMN discord INTEGER NOT NULL DEFAULT 0",
    ("job_runs", "game"): "ALTER TABLE job_runs ADD COLUMN game TEXT",
}

_FIELDS = ("market", "low", "mid", "high")


def is_missing_db(exc: sqlite3.DatabaseError, path: Path | str) -> bool:
    """True only when the open failed AND the path truly does not exist (lstat says ENOENT).

    "unable to open database file" is also what an existing but unreadable file (chmod 000) or a
    directory raises, so the message alone proves nothing; any stat error other than not-found
    (e.g. permission denied on the parent) is conservatively treated as unreadable."""
    if "unable to open database file" not in str(exc).lower():
        return False
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _chunks(items: list, size: int = 300):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class Store:
    def __init__(self, path: Path | str, *, readonly: bool = False):
        memory = str(path) == ":memory:"
        if not memory and not readonly:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # An existing file we cannot write (chmod 0444, read-only mount) is opened
        # read-only: no WAL switch, no schema/migration writes, reads still work.
        # readonly=True ALWAYS opens mode=ro (never creates; a missing file raises sqlite3.OperationalError).
        self.readonly = not memory and (readonly or (Path(path).exists() and not os.access(path, os.W_OK)))
        if self.readonly:
            self.conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True,
                                        timeout=BUSY_TIMEOUT_MS / 1000)
        else:
            self.conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
        self.conn.row_factory = sqlite3.Row
        if self.readonly:
            self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            return
        if not memory:
            self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _has_column(self, table: str, column: str) -> bool:
        return any(r["name"] == column for r in self.conn.execute(f"PRAGMA table_info({table})"))

    def _migrate(self) -> None:
        for (table, column), sql in MIGRATIONS.items():
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(sql)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- writes -----------------------------------------------------------

    def write_snapshot(self, snapshot_date: date,
                       batches: Iterable[tuple[list[Card], list[PricePoint]]],
                       *, replace_game: str | None = None) -> tuple[int, int]:
        """Store a whole game's (cards, prices) batches in ONE transaction: all or nothing.

        Callers fetch everything first, so no write transaction is held across the network.
        With ``replace_game``, that game's existing prices for the day are deleted first in the
        same transaction, so the day ends up exactly as fetched (used by ``backfill --force``).
        Returns (cards, prices) stored.
        """
        n_cards = n_prices = 0
        with self.conn:  # commits on success, rolls back on any exception
            if replace_game is not None:
                self.conn.execute(
                    """DELETE FROM prices WHERE snapshot_date = ? AND product_id IN
                       (SELECT product_id FROM cards WHERE game_key = ?)""",
                    (snapshot_date.isoformat(), replace_game))
            for cards, prices in batches:
                self.upsert_cards(cards, commit=False)
                n_prices += self.upsert_prices(snapshot_date, prices, commit=False)
                n_cards += len(cards)
        return n_cards, n_prices

    # --- job_runs: ops status and scheduler workflow state (never data completeness) ---

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def job_start(self, kind: str, dataset_date: date | str | None = None, game: str | None = None,
                  note: str | None = None) -> int:
        """Insert an OPEN row (finished NULL) and commit it immediately; returns its id."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO job_runs (kind, started, dataset_date, game, note) VALUES (?, ?, ?, ?, ?)",
                (kind, self._now(), str(dataset_date) if dataset_date else None, game, note))
        return cur.lastrowid

    def job_finish(self, run_id: int, ok: bool, note: str | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE job_runs SET finished=?, ok=?, note=COALESCE(?, note) WHERE id=?",
                              (self._now(), int(ok), note, run_id))

    def job_record(self, kind: str, ok: bool, note: str | None = None,
                   dataset_date: date | str | None = None, game: str | None = None) -> int:
        """A row that is complete when written (tick rows)."""
        run_id = self.job_start(kind, dataset_date, game)
        self.job_finish(run_id, ok, note)
        return run_id

    def job_rows(self, kind: str, dataset_date: date | str, game: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM job_runs WHERE kind=? AND dataset_date=?"
        args: list = [kind, str(dataset_date)]
        if game is not None:
            sql += " AND game=?"
            args.append(game)
        return self.conn.execute(sql + " ORDER BY id", args).fetchall()

    def job_done(self, kind: str, dataset_date: date | str, game: str) -> bool:
        return any(r["ok"] == 1 for r in self.job_rows(kind, dataset_date, game))

    def job_ok_any(self, kind: str, game: str) -> bool:
        """Is there a closed ok=1 row of `kind` for `game` (any date)? Failed/open rows don't count."""
        return self.conn.execute("SELECT 1 FROM job_runs WHERE kind=? AND game=? AND ok=1 AND finished IS NOT NULL LIMIT 1",
                                 (kind, game)).fetchone() is not None

    def close_interrupted(self) -> list[sqlite3.Row]:
        """Close every OPEN row as interrupted. Only call while holding the job lock: then no live
        process can own an open row, so each one belongs to a crashed run."""
        rows = self.conn.execute("SELECT * FROM job_runs WHERE finished IS NULL").fetchall()
        for r in rows:
            self.job_finish(r["id"], False, "interrupted")
        return rows

    def last_job(self, kind: str, ok: bool | None = None) -> sqlite3.Row | None:
        sql, args = "SELECT * FROM job_runs WHERE kind=? AND finished IS NOT NULL", [kind]
        if ok is not None:
            sql += " AND ok=?"
            args.append(int(ok))
        return self.conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()

    def recent_jobs(self, kind: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        """job_runs rows, newest first (optionally one kind)."""
        sql, args = "SELECT * FROM job_runs", []
        if kind is not None:
            sql += " WHERE kind=?"
            args.append(kind)
        return self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit]).fetchall()

    def recent_alerts(self, game: str | None = None, since: date | None = None,
                      discord: bool | None = None, limit: int = 100) -> list[sqlite3.Row]:
        """alerts rows with card name/game, newest first (optionally one game, since a date, Discord-posted or not)."""
        sql = """SELECT a.rowid AS id, a.product_id, a.printing, a.alerted_on, a.window_days, a.price_field,
                        a.baseline, a.current, a.pct_change, a.discord,
                        c.game_key AS game, c.name, c.group_name, c.number, c.rarity, c.url
                 FROM alerts a LEFT JOIN cards c USING (product_id)"""
        where, args = [], []
        if game is not None:
            where.append("c.game_key = ?")
            args.append(game)
        if since is not None:
            where.append("a.alerted_on >= ?")
            args.append(since.isoformat())
        if discord is not None:
            where.append("a.discord = ?")
            args.append(int(discord))
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self.conn.execute(sql + " ORDER BY a.alerted_on DESC, a.rowid DESC LIMIT ?", [*args, limit]).fetchall()

    def upsert_cards(self, cards: Iterable[Card], *, commit: bool = True) -> None:
        self.conn.executemany(
            """INSERT INTO cards (product_id, game_key, group_id, group_name, name, number, rarity, url, image_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(product_id) DO UPDATE SET
                 game_key=excluded.game_key, group_id=excluded.group_id, group_name=excluded.group_name,
                 name=excluded.name, number=excluded.number, rarity=excluded.rarity, url=excluded.url,
                 image_url=excluded.image_url, updated_at=datetime('now')""",
            [(c.product_id, c.game_key, c.group_id, c.group_name, c.name, c.number, c.rarity, c.url, c.image_url)
             for c in cards],
        )
        if commit:
            self.conn.commit()

    def upsert_prices(self, snapshot_date: date, points: Iterable[PricePoint], *, commit: bool = True) -> int:
        rows = [(p.product_id, p.printing, snapshot_date.isoformat(), p.market, p.low, p.mid, p.high)
                for p in points]
        self.conn.executemany(
            """INSERT INTO prices (product_id, printing, snapshot_date, market, low, mid, high)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(product_id, printing, snapshot_date) DO UPDATE SET
                 market=excluded.market, low=excluded.low, mid=excluded.mid, high=excluded.high""",
            rows,
        )
        if commit:
            self.conn.commit()
        return len(rows)

    def record_alerts(self, movers: Iterable[Mover], alerted_on: date, window_days: int,
                      discord: bool = False) -> None:
        self.conn.executemany(
            """INSERT INTO alerts (product_id, printing, alerted_on, window_days, price_field,
                                   baseline, current, pct_change, discord)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(m.card.product_id, m.printing, alerted_on.isoformat(), window_days, m.price_field,
              m.baseline_price, m.current_price, m.pct_change, int(discord)) for m in movers],
        )
        self.conn.commit()

    # --- reads ------------------------------------------------------------

    def latest_snapshot_date(self, game_key: str) -> date | None:
        row = self.conn.execute(
            """SELECT MAX(p.snapshot_date) FROM prices p JOIN cards c USING (product_id)
               WHERE c.game_key = ?""", (game_key,)).fetchone()
        return date.fromisoformat(row[0]) if row[0] else None

    def has_prices(self, game_key: str, day: date) -> bool:
        row = self.conn.execute(
            """SELECT 1 FROM prices p JOIN cards c USING (product_id)
               WHERE c.game_key = ? AND p.snapshot_date = ? LIMIT 1""",
            (game_key, day.isoformat())).fetchone()
        return row is not None

    def product_ids(self, game_key: str) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT product_id FROM cards WHERE game_key = ?", (game_key,))}

    def snapshot_dates(self, game_key: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            """SELECT DISTINCT p.snapshot_date FROM prices p JOIN cards c USING (product_id)
               WHERE c.game_key = ? ORDER BY 1""", (game_key,))]

    def snapshot_days(self, game_key: str) -> int:
        row = self.conn.execute(
            """SELECT COUNT(DISTINCT p.snapshot_date) FROM prices p JOIN cards c USING (product_id)
               WHERE c.game_key = ?""", (game_key,)).fetchone()
        return row[0]

    def observations(self, game_key: str, window_days: int,
                     grace_days: int = BASELINE_GRACE_DAYS) -> Iterator[Observation]:
        """Pair each series' latest price with its price ~window_days earlier."""
        current = self.latest_snapshot_date(game_key)
        if current is None:
            return
        target = current - timedelta(days=window_days)
        earliest = target - timedelta(days=grace_days)
        cur_cols = ", ".join(f"cur.{f} AS cur_{f}" for f in _FIELDS)
        base_cols = ", ".join(f"base.{f} AS base_{f}" for f in _FIELDS)
        sql = f"""
            WITH pick AS (
                SELECT product_id, printing, MAX(snapshot_date) AS sd FROM prices
                WHERE snapshot_date BETWEEN :earliest AND :target
                GROUP BY product_id, printing
            )
            SELECT c.*, cur.printing, base.snapshot_date AS base_date, {cur_cols}, {base_cols}
            FROM cards c
            JOIN prices cur ON cur.product_id = c.product_id AND cur.snapshot_date = :current
            JOIN pick ON pick.product_id = cur.product_id AND pick.printing = cur.printing
            JOIN prices base ON base.product_id = pick.product_id AND base.printing = pick.printing
                            AND base.snapshot_date = pick.sd
            WHERE c.game_key = :game
        """
        params = {"game": game_key, "current": current.isoformat(),
                  "target": target.isoformat(), "earliest": earliest.isoformat()}
        for r in self.conn.execute(sql, params):
            card = Card(product_id=r["product_id"], game_key=r["game_key"], group_id=r["group_id"],
                        group_name=r["group_name"], name=r["name"], number=r["number"],
                        rarity=r["rarity"], url=r["url"], image_url=r["image_url"])
            yield Observation(
                card=card, printing=r["printing"],
                baseline_date=r["base_date"], current_date=current.isoformat(),
                baseline={f: r[f"base_{f}"] for f in _FIELDS},
                current={f: r[f"cur_{f}"] for f in _FIELDS},
            )

    def recently_alerted(self, since: date, *, discord: bool = False) -> set[tuple[int, str]]:
        """Series alerted on or after `since`; with discord=True, only those posted to Discord."""
        sql = "SELECT DISTINCT product_id, printing FROM alerts WHERE alerted_on >= ?"
        if discord:
            sql += " AND discord = 1"
        return {(r[0], r[1]) for r in self.conn.execute(sql, (since.isoformat(),))}

    def last_alerts(self, game_key: str) -> dict[tuple[int, str], tuple[str, bool]]:
        """Latest alert date per series, and whether any alert for it was posted to Discord."""
        # `discord` is a migrated column: a legacy DB opened read-only (no migration) may lack it, and
        # then no alert can have been posted to Discord.
        posted = "MAX(a.discord)" if self._has_column("alerts", "discord") else "0"
        rows = self.conn.execute(
            f"""SELECT a.product_id, a.printing, MAX(a.alerted_on), {posted}
               FROM alerts a JOIN cards c USING (product_id) WHERE c.game_key = ?
               GROUP BY a.product_id, a.printing""", (game_key,))
        return {(r[0], r[1]): (r[2], bool(r[3])) for r in rows}

    def sets(self, game_key: str) -> list[sqlite3.Row]:
        """Sets seen in snapshots for a game, newest group ID first, with single counts."""
        return self.conn.execute(
            """SELECT group_id, group_name, COUNT(*) AS singles FROM cards
               WHERE game_key = ? GROUP BY group_id, group_name ORDER BY group_id DESC""",
            (game_key,)).fetchall()

    def prune_game(self, game_key: str, cutoff: date, *, dry_run: bool = False,
                   alerts_cutoff: date | None = None) -> tuple[int, ...]:
        """Delete a game's price rows dated before `cutoff`, then its cards left with no prices that no
        watchlist or alert row references, in ONE transaction. Returns (prices, cards) removed (or, with
        dry_run, that would be). Watchlist is never touched. With `alerts_cutoff` the game's alerts rows
        with alerted_on before it are deleted too (first, so cards they alone referenced become orphans)
        and the result is (prices, cards, alerts)."""
        if self.readonly and not dry_run:
            raise sqlite3.OperationalError("database is read-only; cannot prune")
        c = cutoff.isoformat()
        ac = alerts_cutoff.isoformat() if alerts_cutoff else None
        mine = "product_id IN (SELECT product_id FROM cards WHERE game_key = ?)"
        # alerts older than alerts_cutoff are going away, so they no longer keep a card alive
        kept_alerts = "SELECT product_id FROM alerts" + (" WHERE alerted_on >= ?" if ac else "")
        orphan = f"""game_key = ? AND NOT EXISTS (SELECT 1 FROM prices p WHERE p.product_id = cards.product_id
                                                  AND p.snapshot_date >= ?)
                    AND product_id NOT IN (SELECT product_id FROM watchlist)
                    AND product_id NOT IN ({kept_alerts})"""
        oargs = (game_key, c, *([ac] if ac else []))
        if dry_run:
            n_prices = self.conn.execute(f"SELECT COUNT(*) FROM prices WHERE snapshot_date < ? AND {mine}",
                                         (c, game_key)).fetchone()[0]
            n_cards = self.conn.execute(f"SELECT COUNT(*) FROM cards WHERE {orphan}", oargs).fetchone()[0]
            if ac is None:
                return n_prices, n_cards
            n_alerts = self.conn.execute(f"SELECT COUNT(*) FROM alerts WHERE alerted_on < ? AND {mine}",
                                         (ac, game_key)).fetchone()[0]
            return n_prices, n_cards, n_alerts
        with self.conn:
            n_prices = self.conn.execute(f"DELETE FROM prices WHERE snapshot_date < ? AND {mine}",
                                         (c, game_key)).rowcount
            n_alerts = (self.conn.execute(f"DELETE FROM alerts WHERE alerted_on < ? AND {mine}",
                                          (ac, game_key)).rowcount if ac else 0)
            n_cards = self.conn.execute(f"DELETE FROM cards WHERE {orphan}", oargs).rowcount
        return (n_prices, n_cards) if ac is None else (n_prices, n_cards, n_alerts)

    def prune_job_runs(self, cutoffs: dict[str, date], *, dry_run: bool = False,
                       live_from: dict[str, date] | None = None) -> int:
        """Delete finished job_runs rows started before their cutoff. `cutoffs` maps each game being
        pruned to its cutoff; only that game's rows are touched (rows of unlisted games are never
        deleted), and game-less rows (schedule ticks) use the earliest cutoff. Never deleted: open rows;
        the newest row per (kind, game) and the newest ok=1 row per (kind, game) (the one-shot `repair`
        marker, health); and any row whose dataset_date is at or after `live_from[game]` (the game's
        current dataset, whose failed/interrupted `check` guard the scheduler still consults)."""
        if self.readonly and not dry_run:
            raise sqlite3.OperationalError("database is read-only; cannot prune")
        if not cutoffs:
            return 0
        if dry_run and "game" not in {r[1] for r in self.conn.execute("PRAGMA table_info(job_runs)")}:
            return 0  # pre-`game` schema (a read-only preview can't migrate): legacy rows are never planned for deletion
        live = live_from or {}
        clauses, args = [], []
        for g, c in cutoffs.items():
            clauses.append("(game = ? AND started < ? AND (dataset_date IS NULL OR dataset_date < ?))")
            args += [g, c.isoformat(), live[g].isoformat() if g in live else "9999-12-31"]
        clauses.append("(game IS NULL AND started < ?)")
        args.append(min(cutoffs.values()).isoformat())
        where = ("finished IS NOT NULL AND (" + " OR ".join(clauses) + ") "
                 "AND id NOT IN (SELECT MAX(id) FROM job_runs GROUP BY kind, game) "
                 "AND id NOT IN (SELECT MAX(id) FROM job_runs WHERE ok = 1 GROUP BY kind, game)")
        if dry_run:
            return self.conn.execute(f"SELECT COUNT(*) FROM job_runs WHERE {where}", args).fetchone()[0]
        with self.conn:
            return self.conn.execute(f"DELETE FROM job_runs WHERE {where}", args).rowcount

    def compact(self, *, vacuum: bool = False) -> None:
        """PRAGMA optimize, and with `vacuum` a full VACUUM to return freed pages to the OS. VACUUM
        rewrites the file and needs exclusive access: only call it while holding the job lock."""
        if self.readonly:
            raise sqlite3.OperationalError("database is read-only; cannot compact")
        self.conn.commit()
        if vacuum:
            self.conn.execute("VACUUM")
        self.conn.execute("PRAGMA optimize")

    # --- watchlist and history ---------------------------------------------

    def watch(self, product_id: int, printing: str) -> bool:
        """Add a series to the watchlist; False if the card or that printing has no stored prices."""
        if not self.conn.execute("SELECT 1 FROM cards WHERE product_id = ?", (product_id,)).fetchone():
            return False
        if not self.conn.execute("SELECT 1 FROM prices WHERE product_id = ? AND printing = ?",
                                 (product_id, printing)).fetchone():
            return False
        self.conn.execute("INSERT OR IGNORE INTO watchlist (product_id, printing) VALUES (?, ?)",
                          (product_id, printing))
        self.conn.commit()
        return True

    def unwatch(self, product_id: int, printing: str) -> bool:
        """Remove a series from the watchlist; False if it wasn't on it."""
        cur = self.conn.execute("DELETE FROM watchlist WHERE product_id = ? AND printing = ?", (product_id, printing))
        self.conn.commit()
        return cur.rowcount > 0

    def watched(self, game_key: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT c.*, w.printing, w.added_at FROM watchlist w JOIN cards c USING (product_id)
               WHERE c.game_key = ? ORDER BY w.added_at, c.name""", (game_key,)).fetchall()

    def watched_current(self, game_key: str, fields: tuple[str, ...]) -> list[Unpaired]:
        """Every watched series with its current effective price (first of `fields` that is set),
        independent of whether a baseline exists."""
        current = self.latest_snapshot_date(game_key)
        watched = self.watched(game_key)
        rows: dict[tuple[int, str], sqlite3.Row] = {}
        if current is not None:
            for chunk in _chunks([(r["product_id"], r["printing"]) for r in watched]):
                values = ",".join(["(?,?)"] * len(chunk))
                for row in self.conn.execute(
                        f"WITH s(pid, pr) AS (VALUES {values}) SELECT p.* FROM s JOIN prices p "
                        "ON p.product_id = s.pid AND p.printing = s.pr WHERE p.snapshot_date = ?",
                        [x for k in chunk for x in k] + [current.isoformat()]):
                    rows[(row["product_id"], row["printing"])] = row
        out = []
        for r in watched:
            card = Card(product_id=r["product_id"], game_key=r["game_key"], group_id=r["group_id"],
                        group_name=r["group_name"], name=r["name"], number=r["number"],
                        rarity=r["rarity"], url=r["url"], image_url=r["image_url"])
            row = rows.get((r["product_id"], r["printing"]))
            field = price = None
            if row is not None:
                for f in fields:
                    if row[f] and row[f] > 0:
                        field, price = f, row[f]
                        break
            out.append(Unpaired(card, r["printing"], field, current.isoformat() if price else None, price))
        return out

    def history_many(self, series: list[tuple[int, str]],
                     days: int | None = None) -> dict[tuple[int, str], list[dict]]:
        """`history` for many series in a few queries (no N+1). Every requested series has a key,
        with an empty list when it has no prices."""
        out: dict[tuple[int, str], list[dict]] = {k: [] for k in series}
        for chunk in _chunks(list(out)):
            values = ",".join(["(?,?)"] * len(chunk))
            args: list = [x for k in chunk for x in k]
            sql = (f"WITH s(pid, pr) AS (VALUES {values}), "
                   "m AS (SELECT s.pid, s.pr, MAX(p.snapshot_date) md FROM s JOIN prices p "
                   "ON p.product_id = s.pid AND p.printing = s.pr GROUP BY s.pid, s.pr) "
                   "SELECT p.product_id, p.printing, p.snapshot_date, p.market, p.low, p.mid, p.high "
                   "FROM m JOIN prices p ON p.product_id = m.pid AND p.printing = m.pr")
            if days:
                sql += " WHERE p.snapshot_date >= date(m.md, ?)"
                args.append(f"-{int(days)} days")
            sql += " ORDER BY p.product_id, p.printing, p.snapshot_date"
            for r in self.conn.execute(sql, args):
                out[(r[0], r[1])].append({"date": r[2], **{f: r[i + 3] for i, f in enumerate(_FIELDS)}})
        return out

    def history(self, product_id: int, printing: str, days: int | None = None) -> list[dict]:
        """Daily prices for one series, oldest first; `days` limits to the last N days of stored data."""
        sql = "SELECT snapshot_date, market, low, mid, high FROM prices WHERE product_id = ? AND printing = ?"
        args: list = [product_id, printing]
        if days:
            sql += " AND snapshot_date >= (SELECT date(MAX(snapshot_date), ?) FROM prices WHERE product_id = ? AND printing = ?)"
            args += [f"-{int(days)} days", product_id, printing]
        sql += " ORDER BY snapshot_date"
        return [{"date": r[0], **{f: r[i + 1] for i, f in enumerate(_FIELDS)}}
                for r in self.conn.execute(sql, args)]

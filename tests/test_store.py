from datetime import date

import pytest

from tcgwatch.config import AlertRule
from tcgwatch.models import Card, PricePoint
from tcgwatch.movement import find_movers
from tcgwatch.store import Store

CARDS = [
    Card(1, "op", 10, "Set A", "Spiker", "OP01-001", "R"),
    Card(2, "op", 10, "Set A", "Flat", "OP01-002", "C"),
    Card(3, "rb", 20, "Set B", "Other game", "001", "Rare"),
]


def pp(pid, market, printing="Normal"):
    return PricePoint(pid, printing, market, None, None, None)


@pytest.fixture
def s():
    s = Store(":memory:")
    s.upsert_cards(CARDS)
    s.upsert_prices(date(2026, 10, 30), [pp(1, 2.0), pp(2, 1.0), pp(1, 5.0, "Foil"), pp(3, 1.0)])
    s.upsert_prices(date(2026, 11, 3), [pp(1, 2.5), pp(2, 1.0)])
    s.upsert_prices(date(2026, 11, 6), [pp(1, 3.0), pp(2, 1.05), pp(1, 5.0, "Foil"), pp(3, 9.0)])
    yield s
    s.close()


def test_observations_pair_current_with_window_baseline(s):
    obs = {(o.card.product_id, o.printing): o for o in s.observations("op", window_days=7)}
    assert set(obs) == {(1, "Normal"), (2, "Normal"), (1, "Foil")}
    assert obs[(1, "Normal")].baseline_date == "2026-10-30"
    assert obs[(1, "Normal")].baseline["market"] == 2.0
    assert obs[(1, "Normal")].current["market"] == 3.0


def test_baseline_outside_grace_is_ignored(s):
    # 2-day window targets 11-04 (no snapshot) -> falls back to 11-03, within the 2-day grace
    assert {o.baseline_date for o in s.observations("op", window_days=2)} == {"2026-11-03"}
    # 30-day window has no snapshot in range
    assert list(s.observations("op", window_days=30)) == []


def test_end_to_end_movers_and_cooldown(s):
    movers = find_movers(s.observations("op", 7), AlertRule())
    assert [(m.card.name, m.printing) for m in movers] == [("Spiker", "Normal")]
    s.record_alerts(movers, date(2026, 11, 6), 7)
    assert s.recently_alerted(date(2026, 11, 1)) == {(1, "Normal")}


def test_snapshot_is_idempotent(s):
    s.upsert_prices(date(2026, 11, 6), [pp(1, 4.0)])
    assert s.snapshot_days("op") == 3
    obs = {(o.card.product_id, o.printing): o for o in s.observations("op", 7)}
    assert obs[(1, "Normal")].current["market"] == 4.0


def test_empty_store_has_no_snapshot_and_no_observations(tmp_path):
    with Store(tmp_path / "e.db") as s:
        assert s.latest_snapshot_date("op") is None
        assert list(s.observations("op", 7)) == []
        assert s.last_alerts("op") == {}


def test_discord_alert_filter_and_last_alert(tmp_path):
    from tcgwatch.models import Mover

    card = Card(1, "op", 100, "Set A", "Spike", "A-1", "C")
    m = Mover(card, "Normal", "market", "2026-10-30", "2026-11-06", 1.0, 2.0, 100.0)
    with Store(tmp_path / "a.db") as s:
        s.upsert_cards([card])
        s.record_alerts([m], date(2026, 11, 1), 7, discord=False)
        assert s.recently_alerted(date(2026, 10, 31)) == {(1, "Normal")}
        assert s.recently_alerted(date(2026, 10, 31), discord=True) == set()
        s.record_alerts([m], date(2026, 11, 6), 7, discord=True)
        assert s.recently_alerted(date(2026, 11, 6), discord=True) == {(1, "Normal")}
        assert s.recently_alerted(date(2026, 11, 7)) == set()  # before/after since is respected
        assert s.last_alerts("op") == {(1, "Normal"): ("2026-11-06", True)}


def test_file_db_uses_wal_and_busy_timeout(tmp_path):
    with Store(tmp_path / "sub" / "t.db") as s:
        assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert s.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_memory_db_skips_wal():
    with Store(":memory:") as s:
        assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory"


def test_job_runs_table_created_on_existing_db(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE watchlist (product_id INTEGER NOT NULL, printing TEXT NOT NULL, "
              "added_at TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY (product_id, printing))")
    c.commit(); c.close()
    with Store(db) as s:
        cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(job_runs)")}
        assert cols == {"id", "kind", "started", "finished", "ok", "dataset_date", "note", "game"}
        s.conn.execute("INSERT INTO job_runs (kind, started, ok) VALUES ('snapshot', '2026-11-06T20:30:00Z', 1)")
        s.conn.commit()
    with Store(db) as s:  # reopen is idempotent
        assert s.conn.execute("SELECT count(*) FROM job_runs").fetchone()[0] == 1


@pytest.mark.parametrize("journal", ["wal", "delete"])
def test_readonly_file_db_still_readable(tmp_path, journal):
    """A chmod-0444 DB opens read-only (no WAL switch, no migrations) in either journal mode."""
    import sqlite3
    db = tmp_path / "ro.db"
    with Store(db) as s:
        s.conn.execute("INSERT INTO watchlist(product_id, printing) VALUES (123, 'Normal')")
        s.conn.commit()
    if journal == "delete":  # e.g. a DB made by an older version or copied from a read-only mount
        c = sqlite3.connect(db)
        assert c.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        c.close()
    db.chmod(0o444)
    try:
        with Store(db) as s:
            assert s.readonly
            assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == journal  # not switched
            assert s.conn.execute("SELECT product_id FROM watchlist").fetchone()["product_id"] == 123
    finally:
        db.chmod(0o644)


def test_job_runs_game_column_added_to_c1_era_table(tmp_path):
    import sqlite3
    db = tmp_path / "c1.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE job_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
              "started TEXT NOT NULL, finished TEXT, ok INTEGER, dataset_date TEXT, note TEXT)")
    c.commit(); c.close()
    with Store(db) as s:
        rid = s.job_start("check", date(2026, 11, 6), "op")
        assert s.job_rows("check", date(2026, 11, 6), "op")[0]["finished"] is None
        s.job_finish(rid, True)
        assert s.job_done("check", date(2026, 11, 6), "op")
        assert not s.job_done("check", date(2026, 11, 6), "rb")


def test_close_interrupted_closes_only_open_rows(tmp_path):
    with Store(tmp_path / "t.db") as s:
        done = s.job_record("snapshot", True, dataset_date="2026-11-06", game="op")
        open_id = s.job_start("check", "2026-11-06", "op")
        closed = s.close_interrupted()
        assert [r["id"] for r in closed] == [open_id]
        rows = {r["id"]: r for r in s.conn.execute("SELECT * FROM job_runs")}
        assert rows[open_id]["ok"] == 0 and rows[open_id]["note"] == "interrupted"
        assert rows[done]["ok"] == 1


def test_history_many_matches_history_per_series(s):
    series = [(1, "Normal"), (1, "Foil"), (2, "Normal"), (2, "Foil"), (3, "Normal")]
    for days in (None, 3, 7):
        many = s.history_many(series, days)
        assert set(many) == set(series)
        for pid, printing in series:
            assert many[(pid, printing)] == s.history(pid, printing, days)
    assert many[(2, "Foil")] == []
    assert s.history_many([]) == {}


def test_history_many_chunks_large_requests(s, monkeypatch):
    import tcgwatch.store as store_mod
    monkeypatch.setattr(store_mod, "_chunks", lambda items, size=1: (items[i:i + 1] for i in range(len(items))))
    series = [(1, "Normal"), (2, "Normal"), (1, "Foil")]
    assert s.history_many(series, 7) == {k: s.history(*k, 7) for k in series}


def test_watched_current_batched_values_and_missing(s):
    s.watch(1, "Normal")
    s.watch(1, "Foil")
    s.conn.execute("INSERT INTO watchlist (product_id, printing) VALUES (2, 'Foil')")  # never priced
    s.conn.commit()
    got = {(u.card.product_id, u.printing): u for u in s.watched_current("op", ("market",))}
    assert got[(1, "Normal")].current_price == 3.0 and got[(1, "Normal")].current_date == "2026-11-06"
    assert got[(1, "Foil")].current_price == 5.0
    assert got[(2, "Foil")].current_price is None and got[(2, "Foil")].current_date is None


@pytest.fixture
def mem():
    """In-memory Store, closed after the test."""
    s = Store(":memory:")
    yield s
    s.close()


@pytest.mark.parametrize("count", [301, 650])
def test_history_many_real_chunks_match_per_series(mem, count):
    """TEST-8: a real >300-series request (no `_chunks` patch), so it spans the 300-item chunk boundary.
    Series with gaps, Foil rows and an unknown series must all come back exactly as `history` returns them."""
    assert count > 300  # the default chunk size; the regression is meaningless below it
    s = mem
    s.upsert_cards([Card(pid, "op", 10, "Set A", f"Card {pid}", f"A-{pid}", "C") for pid in range(1, count + 1)])
    s.upsert_prices(date(2026, 10, 30), [pp(pid, float(pid)) for pid in range(1, count + 1)])
    s.upsert_prices(date(2026, 11, 3), [pp(pid, pid * 1.5) for pid in range(1, count + 1) if pid % 7])
    s.upsert_prices(date(2026, 11, 6), [pp(pid, pid * 2.0) for pid in range(1, count + 1)]
                    + [pp(pid, 1.0 + pid % 3, "Foil") for pid in range(5, count + 1, 5)])
    series = ([(pid, "Normal") for pid in range(1, count + 1)]
              + [(pid, "Foil") for pid in range(5, count + 1, 5)] + [(count + 1, "Normal")])
    for days in (None, 3, 7):
        many = s.history_many(series, days)
        assert list(many) == series
        for key in series:
            assert many[key] == s.history(*key, days), (key, days)
    assert many[(count + 1, "Normal")] == []
    assert many[(300, "Normal")] and many[(301, "Normal")]  # both sides of the first chunk boundary

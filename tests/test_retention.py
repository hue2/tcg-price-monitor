"""RET-1: price-history retention (retention_days, Store.prune_game, prune_history, `tcgwatch prune`)."""

import os
import stat
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tcgwatch import cli
from tcgwatch.config import ConfigError, parse_config
from tcgwatch.models import Card, PricePoint
from tcgwatch.scheduler import prune_history
from tcgwatch.store import Store

LATEST = date(2026, 11, 7)


@pytest.fixture
def mem():
    """In-memory Store, closed after the test (avoids unclosed-database ResourceWarnings)."""
    s = Store(":memory:")
    yield s
    s.close()


def make_cfg(tmp_path, **extra):
    return parse_config({"database": str(tmp_path / "t.db"),
                         "games": {"op": {"name": "OP", "category_id": 68}}, **extra}, base_dir=tmp_path)


def seed(store, days_back):
    """Card 1 priced on each given day-offset; card 2 only on the oldest (orphans once pruned)."""
    store.upsert_cards([Card(1, "op", 1, "S", "Keep", "1", "R"), Card(2, "op", 1, "S", "Old", "2", "R"),
                        Card(3, "op", 1, "S", "Watched", "3", "R"), Card(4, "op", 1, "S", "Alerted", "4", "R"),
                        Card(9, "rb", 2, "S", "Other", "9", "R")])
    for d in days_back:
        day = LATEST - timedelta(days=d)
        pts = [PricePoint(1, "Normal", 1.0, None, None, None)]
        if d >= 150:
            pts += [PricePoint(p, "Normal", 1.0, None, None, None) for p in (2, 3, 4, 9)]
        store.upsert_prices(day, pts)
    store.conn.execute("INSERT INTO watchlist (product_id, printing) VALUES (3, 'Normal')")
    store.conn.execute("INSERT INTO alerts VALUES (4,'Normal','2026-01-01',7,'market',1,2,100,0)")
    store.conn.commit()


def count(store, table):
    return store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- config ---------------------------------------------------------------

def test_default_is_180_and_on(tmp_path):
    assert make_cfg(tmp_path).retention_days == 180


@pytest.mark.parametrize("value", [0, None])
def test_zero_or_null_disables(tmp_path, value):
    assert make_cfg(tmp_path, retention_days=value).retention_days is None


@pytest.mark.parametrize("bad", [-1, "180", 1.5, True, [1]])
def test_invalid_retention_rejected(tmp_path, bad):
    with pytest.raises(ConfigError, match="retention_days"):
        make_cfg(tmp_path, retention_days=bad)


def test_effective_retention_is_floored_by_window_and_catch_up(tmp_path):
    cfg = make_cfg(tmp_path, retention_days=5, alert={"window": "30d"})
    g = cfg.games["op"]
    assert cfg.retention_days_for(g) == 32  # window 30 + grace 2 beats retention 5 and catch-up 30
    cfg = make_cfg(tmp_path, retention_days=3, schedule={"catch_up_days": 60})
    assert cfg.retention_days_for(cfg.games["op"]) == 60
    assert make_cfg(tmp_path, retention_days=400).retention_days_for(cfg.games["op"]) == 400
    assert make_cfg(tmp_path, retention_days=0).retention_days_for(cfg.games["op"]) is None


# --- store ----------------------------------------------------------------

def test_boundary_day_is_kept_and_day_before_is_pruned(mem):
    s = mem
    seed(s, [180, 181, 182, 0])
    cutoff = LATEST - timedelta(days=180)
    s.prune_game("op", cutoff)
    dates = {r[0] for r in s.conn.execute("SELECT DISTINCT snapshot_date FROM prices WHERE product_id=1")}
    assert dates == {cutoff.isoformat(), LATEST.isoformat()}


def test_dry_run_reports_same_counts_and_changes_nothing(mem):
    s = mem
    seed(s, [200, 0])
    before = (count(s, "prices"), count(s, "cards"))
    cutoff = LATEST - timedelta(days=180)
    dry = s.prune_game("op", cutoff, dry_run=True)
    assert (count(s, "prices"), count(s, "cards")) == before
    assert s.prune_game("op", cutoff) == dry == (4, 1)  # card 2 only; 3 watched, 4 alerted, 1 still priced


def test_watchlist_alerts_and_other_games_survive(mem):
    s = mem
    seed(s, [200, 0])
    s.prune_game("op", LATEST - timedelta(days=180))
    assert count(s, "watchlist") == 1 and count(s, "alerts") == 1
    ids = {r[0] for r in s.conn.execute("SELECT product_id FROM cards")}
    assert ids == {1, 3, 4, 9}
    assert s.conn.execute("SELECT COUNT(*) FROM prices WHERE product_id=9").fetchone()[0] == 1  # rb untouched


def test_prune_is_one_transaction(mem):
    s = mem
    seed(s, [200, 0])
    before = count(s, "prices")
    real = s.conn

    class Boom:
        def __init__(self): self.n = 0
        def __enter__(self): return real.__enter__()
        def __exit__(self, *a): return real.__exit__(*a)
        def execute(self, sql, *a):
            if sql.lstrip().startswith("DELETE FROM cards"):
                raise RuntimeError("boom")
            return real.execute(sql, *a)

    s.conn = Boom()
    try:
        with pytest.raises(RuntimeError):
            s.prune_game("op", LATEST - timedelta(days=180))
    finally:
        s.conn = real  # restore before the fixture closes the real connection
    assert count(s, "prices") == before  # price delete rolled back with the failed card delete


def test_readonly_store_refuses(tmp_path):
    path = tmp_path / "ro.db"
    with Store(path) as s:
        seed(s, [200, 0])
    with Store(path, readonly=True) as s:
        with pytest.raises(Exception, match="read-only"):
            s.prune_game("op", LATEST)
        assert s.prune_game("op", LATEST - timedelta(days=180), dry_run=True) == (4, 1)


# --- prune_history ----------------------------------------------------------

def test_prune_history_uses_latest_stored_day_and_protects_windows(mem, tmp_path):
    cfg = make_cfg(tmp_path, retention_days=3)
    s = mem
    seed(s, [200, 40, 5, 0])
    p, c, notes, *_ = prune_history(s, cfg, list(cfg.games.values()))
    # floor = max(3, 7+2, catch_up 7) = 9 days: only the 200d and 40d rows go
    dates = sorted(r[0] for r in s.conn.execute("SELECT DISTINCT snapshot_date FROM prices WHERE product_id=1"))
    assert dates == [(LATEST - timedelta(days=5)).isoformat(), LATEST.isoformat()]
    assert p == 4 + 1 and "op:" in notes[0]


def test_prune_history_noop_when_disabled_or_empty(mem, tmp_path):
    s = mem
    assert prune_history(s, make_cfg(tmp_path), list(make_cfg(tmp_path).games.values()))[:2] == (0, 0)
    cfg = make_cfg(tmp_path, retention_days=None)
    seed(s, [500, 0])
    assert prune_history(s, cfg, list(cfg.games.values()))[:2] == (0, 0)
    assert count(s, "prices") > 0


# --- CLI ------------------------------------------------------------------

@pytest.fixture
def dbcfg(tmp_path):
    cfg = make_cfg(tmp_path)
    with Store(cfg.database) as s:
        seed(s, [200, 0])
    return cfg


def run_cli(cfg, *argv):
    args = cli.build_parser().parse_args(["prune", *argv])
    return cli.cmd_prune(cfg, args)


def test_cli_dry_run_then_real(dbcfg, capsys):
    assert run_cli(dbcfg, "--dry-run") == 0
    assert "dry run: 4 prices, 2 cards, 1 alerts, 0 job runs" in capsys.readouterr().out
    with Store(dbcfg.database) as s:
        assert count(s, "prices") == 6 and count(s, "job_runs") == 0
    assert run_cli(dbcfg) == 0
    assert "pruned 4 prices, 2 cards, 1 alerts, 0 job runs" in capsys.readouterr().out
    with Store(dbcfg.database) as s:
        row = s.last_job("prune", ok=True)
        assert row["note"] == "removed 4 prices, 2 cards, 1 alerts, 0 job runs" and count(s, "prices") == 2


def test_cli_disabled(tmp_path, capsys):
    assert run_cli(make_cfg(tmp_path, retention_days=0)) == 0
    assert "nothing to prune" in capsys.readouterr().out


def test_cli_dry_run_on_readonly_db_and_real_refuses(dbcfg, capsys):
    os.chmod(dbcfg.database, stat.S_IRUSR)
    try:
        if os.access(dbcfg.database, os.W_OK):
            pytest.skip("running as a user that ignores file modes")
        assert run_cli(dbcfg, "--dry-run") == 0
        assert run_cli(dbcfg) == 1
        assert "read-only" in capsys.readouterr().err
    finally:
        os.chmod(dbcfg.database, stat.S_IRUSR | stat.S_IWUSR)


def test_cli_failure_records_failed_row(dbcfg, monkeypatch):
    import tcgwatch.scheduler as sch
    monkeypatch.setattr(sch, "prune_history", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        run_cli(dbcfg)
    with Store(dbcfg.database) as s:
        assert s.last_job("prune")["ok"] == 0


# --- FIX-RET-1: effective (CLI-overridden) window floors the prune --------------

def test_scheduled_window_override_protects_its_baseline(tmp_path, monkeypatch):
    """`schedule --window 365d` with retention 180: the D-365 baseline must survive the prune stage."""
    import tcgwatch.cli as c
    cfg = make_cfg(tmp_path, retention_days=180)
    captured = {}
    monkeypatch.setattr(c.Scheduler, "run_forever", lambda self: captured.setdefault("s", self))
    monkeypatch.setattr(c, "build_source", lambda *a: object())
    args = c.build_parser().parse_args(["schedule", "--window", "365d"])
    assert c.cmd_schedule(cfg, args) == 0
    sched, game = captured["s"], cfg.games["op"]
    with Store(cfg.database) as s:
        seed(s, [365, 364, 0])
        before = count(s, "prices")
        sched._prune(s, game)  # the same call the daily tick makes after a snapshot
        sched._prune(s, game)  # second tick
        assert count(s, "prices") == before
        assert s.conn.execute("SELECT COUNT(*) FROM prices WHERE snapshot_date=?",
                              ((LATEST - timedelta(days=365)).isoformat(),)).fetchone()[0] > 0


def test_without_override_old_baseline_is_pruned(tmp_path, monkeypatch):
    import tcgwatch.cli as c
    cfg = make_cfg(tmp_path, retention_days=180)
    captured = {}
    monkeypatch.setattr(c.Scheduler, "run_forever", lambda self: captured.setdefault("s", self))
    assert c.cmd_schedule(cfg, c.build_parser().parse_args(["schedule"])) == 0
    with Store(cfg.database) as s:
        seed(s, [365, 0])
        captured["s"]._prune(s, cfg.games["op"])
        assert count(s, "prices") == 2  # card 1 today + the other game (untouched)


def test_invalid_schedule_window_fails_before_any_side_effect(tmp_path, monkeypatch):
    import tcgwatch.cli as c
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(c.Scheduler, "run_forever", lambda self: pytest.fail("must not start"))
    with pytest.raises(ConfigError):
        c.cmd_schedule(cfg, c.build_parser().parse_args(["schedule", "--window", "bogus"]))
    assert not cfg.database.exists()


def test_retention_floor_uses_larger_effective_window(tmp_path):
    cfg = make_cfg(tmp_path, retention_days=10)
    g = cfg.games["op"]
    assert cfg.retention_days_for(g, 365) > 365
    assert cfg.retention_days_for(g, 1) == cfg.retention_days_for(g)


def test_dry_run_missing_db_creates_nothing(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    cfg = cfg.__class__(**{**cfg.__dict__, "database": tmp_path / "nested" / "x.db"})
    assert run_cli(cfg, "--dry-run") == 0
    assert "dry run: 0 prices, 0 cards" in capsys.readouterr().out
    assert not (tmp_path / "nested").exists()


def _old_schema_db(path):
    """A DB as an older release left it: pre-watchlist table set, rollback journal."""
    import sqlite3
    from tcgwatch.store import SCHEMA
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO cards (product_id, game_key, group_id, group_name, name, number, rarity) "
                "VALUES (1,'op',1,'S','Card','1','R')")
    con.execute("INSERT INTO prices VALUES (1,'Normal','2020-01-01',1,1,1,1)")  # old enough to be a prune candidate
    con.execute("DROP TABLE watchlist")
    con.execute("PRAGMA journal_mode=DELETE")
    con.commit(); con.close()


def test_dry_run_old_schema_explains_recovery_and_changes_nothing(tmp_path, capsys):
    import hashlib, sqlite3
    cfg = make_cfg(tmp_path)
    db = cfg.database
    _old_schema_db(db)
    digest = lambda: hashlib.sha256(db.read_bytes()).hexdigest()
    before = digest()
    assert run_cli(cfg, "--dry-run") == 1
    cap = capsys.readouterr()
    assert "no such table" in cap.err and "read-only" in cap.err and "tcgwatch games" in cap.err
    assert "delete" not in cap.err.lower() and "recreate" not in cap.err.lower() and "nothing stored" not in cap.out
    assert digest() == before
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert "watchlist" not in [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    con.close()
    assert not list(tmp_path.glob("*-wal")) and not list(tmp_path.glob("*-shm")) and not list(tmp_path.glob("*-journal"))
    # the advertised recovery works: one writable open migrates, then the dry run succeeds
    Store(db).close()
    assert run_cli(cfg, "--dry-run") == 0


def test_dry_run_non_missing_error_is_nonzero(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    cfg.database.write_bytes(b"not a sqlite database at all" * 20)
    assert run_cli(cfg, "--dry-run") == 1
    cap = capsys.readouterr()
    assert "cannot read" in cap.err and "nothing stored" not in cap.out
    assert "migrate" not in cap.err and "hint" not in cap.err  # corruption keeps its own diagnostics


def test_schedule_two_real_ticks_365d_window_keeps_baseline(tmp_path):
    """Production cmd_schedule, two ticks: --window 365d must keep the oldest baseline through each prune stage."""
    from datetime import datetime, timezone
    from unittest.mock import patch
    from tcgwatch.scheduler import Scheduler
    D = date(2026, 11, 7)
    card = Card(1, "op", 1, "Set", "Card", "1", "R")
    point = PricePoint(1, "Normal", 2, None, None, None)
    cfg = make_cfg(tmp_path)
    with Store(cfg.database) as s:
        for back in (365, 364):
            s.write_snapshot(D - timedelta(days=back), [([card], [point])])
    state = {"day": D}
    observed = []

    class Source:
        def snapshot_date(self):
            return state["day"]

        def fetch_singles(self, game):
            yield [card], [point]

    real_check = cli._check

    def checked(c, args, **kw):
        rule = cli._rule_overrides(args, c.games["op"].alert)
        with Store(cfg.database) as s:
            observed.append(len(list(s.observations("op", rule.window_days))))
        return real_check(c, args, **kw)

    def run_two(self):
        self.now = lambda: datetime.combine(state["day"], datetime.min.time(), tzinfo=timezone.utc).replace(hour=21)
        self.sleep = lambda _: None
        self.tick(startup=True)
        state["day"] = D + timedelta(days=1)
        self.tick()

    args = cli.build_parser().parse_args(["schedule", "--window", "365d"])
    with patch.object(cli, "build_source", return_value=Source()), patch.object(cli, "_backfill", return_value=0), \
            patch.object(cli, "_repair_game", return_value=None), patch.object(cli, "_check", side_effect=checked), \
            patch.object(Scheduler, "run_forever", run_two):
        cli.cmd_schedule(cfg, args)
    assert observed == [1, 1]
    with Store(cfg.database) as s:
        assert s.snapshot_dates("op") == [(D - timedelta(days=365)).isoformat(), (D - timedelta(days=364)).isoformat(),
                                          D.isoformat(), (D + timedelta(days=1)).isoformat()]
        assert s.conn.execute("SELECT count(*) FROM job_runs WHERE kind='prune' AND ok=1").fetchone()[0] == 2


# --- RET-3: alerts + job_runs ----------------------------------------------

def _alert(s, pid, day, discord=0):
    s.conn.execute("INSERT INTO alerts VALUES (?,?,?,7,'market',1,2,100,?)", (pid, "Normal", day, discord))


def _job(s, kind, started, finished=True):
    s.conn.execute("INSERT INTO job_runs (kind, started, finished, ok) VALUES (?,?,?,1)",
                   (kind, started, started if finished else None))


def test_prune_history_deletes_old_alerts_but_keeps_cooldown_and_job_newest(mem, tmp_path):
    cfg = make_cfg(tmp_path, alert={"window": 7, "cooldown": 400})
    s = mem
    seed(s, [200, 0])
    _alert(s, 1, (LATEST - timedelta(days=300)).isoformat())   # outside retention AND cooldown: goes
    _alert(s, 1, (LATEST - timedelta(days=380)).isoformat())   # goes
    _alert(s, 1, (LATEST - timedelta(days=399)).isoformat())   # inside the 400d cooldown, still kept
    _alert(s, 1, (LATEST - timedelta(days=10)).isoformat())    # recent: kept
    s.conn.commit()
    r = prune_history(s, cfg, list(cfg.games.values()), dry_run=True)
    assert r.alerts == 0 and count(s, "alerts") == 5           # cooldown 400d protects all but none older than 400
    cfg = make_cfg(tmp_path, alert={"window": 7, "cooldown": 30})
    dry = prune_history(s, cfg, list(cfg.games.values()), dry_run=True)
    assert count(s, "alerts") == 5 and dry.alerts == 4         # seeded 2026-01-01 + 300d + 380d + 399d go
    real = prune_history(s, cfg, list(cfg.games.values()))
    assert real.alerts == dry.alerts == 4 and count(s, "alerts") == 1
    assert real.cards == dry.cards == 2                        # card 4 lost its only alert and is now orphaned


def test_alert_inside_cooldown_survives_short_retention(mem, tmp_path):
    cfg = make_cfg(tmp_path, retention_days=10, alert={"window": 1, "cooldown": 60})
    s = mem
    seed(s, [100, 0])
    _alert(s, 1, (LATEST - timedelta(days=50)).isoformat())
    _alert(s, 1, (LATEST - timedelta(days=61)).isoformat())
    s.conn.commit()
    assert prune_history(s, cfg, list(cfg.games.values())).alerts == 2  # seeded Jan alert + the 61d one
    assert [r[0] for r in s.conn.execute("SELECT alerted_on FROM alerts")] == [(LATEST - timedelta(days=50)).isoformat()]


def test_job_runs_keep_newest_per_kind_and_open_rows(mem, tmp_path):
    cfg = make_cfg(tmp_path)
    s = mem
    seed(s, [0])
    old = (LATEST - timedelta(days=400)).isoformat() + "T00:00:00+00:00"
    for kind in ("schedule", "schedule", "snapshot"):
        _job(s, kind, old)
    _job(s, "check", old, finished=False)                      # crashed/open row: left for close_interrupted
    _job(s, "schedule", LATEST.isoformat() + "T00:00:00+00:00")
    s.conn.commit()
    dry = prune_history(s, cfg, list(cfg.games.values()), dry_run=True)
    assert dry.job_runs == 2 and count(s, "job_runs") == 5     # both old schedule rows (newer one exists); lone snapshot kept
    assert prune_history(s, cfg, list(cfg.games.values())).job_runs == 2
    assert s.last_job("snapshot") is not None and s.last_job("schedule") is not None
    assert count(s, "job_runs") == 3


def test_job_runs_newest_old_row_survives(mem):
    s = mem
    _job(s, "schedule", "2020-01-01T00:00:00+00:00")
    s.conn.commit()
    assert s.prune_job_runs({"op": LATEST}) == 0 and s.last_job("schedule") is not None


def test_store_compact_and_readonly_guards(tmp_path):
    db = tmp_path / "c.db"
    with Store(db) as s:
        s.compact(vacuum=True)
    with Store(db, readonly=True) as s:
        with pytest.raises(Exception, match="read-only"):
            s.compact()
        with pytest.raises(Exception, match="read-only"):
            s.prune_job_runs({"op": LATEST})


def test_cli_prune_reports_and_vacuums(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    with Store(cfg.database) as s:
        seed(s, [200, 0])
        _job(s, "schedule", "2020-01-01T00:00:00+00:00")
        _job(s, "schedule", "2020-01-02T00:00:00+00:00")
        s.conn.commit()
    ns = cli.build_parser().parse_args(["prune", "--dry-run"])
    assert cli.cmd_prune(cfg, ns) == 0
    out = capsys.readouterr().out
    assert "1 alerts" in out and "1 job runs would be removed" in out
    ns = cli.build_parser().parse_args(["prune"])
    assert cli.cmd_prune(cfg, ns) == 0
    assert "1 alerts, 1 job runs" in capsys.readouterr().out


# --- FIX-RET-3: two-game job_runs safety through the real Scheduler._workflow ---------------

OLD = LATEST - timedelta(days=400)


def _two_cfg(tmp_path):
    return parse_config({"database": str(tmp_path / "t.db"), "games": {
        "op": {"name": "OP", "category_id": 68}, "rb": {"name": "RB", "category_id": 80}}}, base_dir=tmp_path)


def _prices(s, game, pid, day):
    s.upsert_cards([Card(pid, game, 1, "S", "C", "1", "R")])
    s.upsert_prices(day, [PricePoint(pid, "Normal", 1, None, None, None)])


def _old(s, kind, game, ok=True, day=OLD):
    rid = s.job_record(kind, ok, dataset_date=day, game=game)
    t = OLD.isoformat() + "T00:00:00+00:00"
    s.conn.execute("UPDATE job_runs SET started=?, finished=? WHERE id=?", (t, t, rid))
    s.conn.commit()
    return rid


def _sched(cfg, calls, repair=None):
    from tcgwatch.scheduler import Scheduler
    return Scheduler(cfg, snapshot_fn=lambda *a: None, backfill_fn=lambda *a: None,
                     check_fn=lambda g: calls.append(g),
                     source_for=lambda g: SimpleNamespace(snapshot_date=lambda: OLD), repair_fn=repair,
                     now=lambda: datetime(2026, 11, 7, 23, tzinfo=timezone.utc), retry_delays=())


def test_single_game_prune_keeps_stalled_game_failed_check_guard(tmp_path):
    cfg = _two_cfg(tmp_path)
    with Store(cfg.database) as s:
        _prices(s, "op", 1, LATEST)
        for i in range(cfg.catch_up_days + 1):
            _prices(s, "rb", 2, OLD - timedelta(days=i))
        _old(s, "check", "rb", ok=False)
        s.job_record("check", True, game="op", dataset_date=LATEST)
        calls = []
        sc = _sched(cfg, calls)
        ok, _, _ = sc._workflow(s, cfg.games["rb"], OLD)
        assert not ok and calls == []
        prune_history(s, cfg, [cfg.games["op"]])
        assert len(s.job_rows("check", OLD, "rb")) == 1
        ok, _, _ = sc._workflow(s, cfg.games["rb"], OLD)
        assert not ok and calls == []                      # still not auto-retried


def test_all_games_prune_keeps_each_games_live_failed_check(tmp_path):
    cfg = _two_cfg(tmp_path)
    with Store(cfg.database) as s:
        _prices(s, "op", 1, LATEST)
        _prices(s, "rb", 2, OLD)
        _old(s, "check", "rb", ok=False)
        _old(s, "check", "rb", ok=False)                   # two failed attempts on the live dataset
        s.job_record("check", True, game="op", dataset_date=LATEST)
        prune_history(s, cfg, list(cfg.games.values()))
        assert len(s.job_rows("check", OLD, "rb")) == 2


def test_prune_leaves_unselected_games_old_rows_alone(tmp_path):
    cfg = _two_cfg(tmp_path)
    with Store(cfg.database) as s:
        _prices(s, "op", 1, LATEST)
        _old(s, "snapshot", "rb", day=OLD - timedelta(days=5))
        _old(s, "snapshot", "rb", day=OLD - timedelta(days=4))
        n = count(s, "job_runs")
        prune_history(s, cfg, [cfg.games["op"]])
        assert count(s, "job_runs") == n


def test_completed_repair_markers_survive_for_every_game(tmp_path):
    cfg = _two_cfg(tmp_path)
    with Store(cfg.database) as s:
        for game, pid in (("op", 1), ("rb", 2)):
            for i in range(cfg.catch_up_days + 1):
                _prices(s, game, pid, LATEST - timedelta(days=i))
            _old(s, "repair", game)
            s.job_record("check", True, game=game, dataset_date=LATEST)
        _old(s, "repair", "op", ok=False)                  # newer failed/open-ish row must not displace the ok marker
        prune_history(s, cfg, list(cfg.games.values()))
        assert s.job_ok_any("repair", "op") and s.job_ok_any("repair", "rb")
        repairs = []
        _sched(cfg, [], repair=lambda g, n: repairs.append(g))._workflow(s, cfg.games["op"], LATEST)
        assert repairs == []


def test_dry_run_job_runs_lacking_game_column_previews_and_changes_nothing(tmp_path, capsys):
    import hashlib, sqlite3
    cfg = make_cfg(tmp_path)
    db = cfg.database
    with Store(db) as store:
        seed(store, [0, 200])  # current prices: non-empty cutoffs must reach the job_runs schema check
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("INSERT INTO job_runs (kind, started, finished, ok) VALUES ('run','2020-01-01T00:00:00','2020-01-01T00:01:00',1)")
    con.execute("INSERT INTO job_runs (kind, started, finished, ok) VALUES ('run','2020-01-02T00:00:00','2020-01-02T00:01:00',0)")
    con.execute("DROP INDEX IF EXISTS job_runs_game")
    con.execute("ALTER TABLE job_runs DROP COLUMN game")
    con.commit(); con.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    assert run_cli(cfg, "--dry-run") == 0
    cap = capsys.readouterr()
    assert "0 job runs would be removed" in cap.out and "no such column" not in cap.err
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not list(tmp_path.glob("*-wal")) and not list(tmp_path.glob("*-journal"))
    Store(db).close()  # a writable open migrates; the dry run still works
    assert run_cli(cfg, "--dry-run") == 0

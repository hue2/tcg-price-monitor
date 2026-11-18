"""`tcgwatch schedule`: expected/remote/stored dates, late-publish window, resumable workflow state."""

import sqlite3
import threading
from dataclasses import replace
from unittest.mock import patch
from datetime import date, datetime, time, timedelta, timezone

import pytest

from tcgwatch import cli
from tcgwatch.config import ConfigError, parse_config
from tcgwatch.joblock import job_lock
from tcgwatch.models import Card, PricePoint
from tcgwatch.scheduler import Scheduler, expected_date, next_fire
from tcgwatch.store import Store

UTC = timezone.utc
D = date(2026, 11, 6)
D1 = D - timedelta(days=1)


def at(day, hh, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC)


class Clock:
    def __init__(self, start):
        self.t, self.sleeps = start, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)


class Remote:
    """TCGCSV stand-in: the dataset date flips when the fake clock passes each publication instant."""

    def __init__(self, clock, publications):
        self.clock, self.publications = clock, publications  # [(instant, dataset date)]
        self.fetches = 0

    def snapshot_date(self):
        done = [d for t, d in self.publications if t <= self.clock.t]
        if not done:
            raise OSError("remote down")
        return max(done)

    def fetch_singles(self, game):
        self.fetches += 1
        d = self.snapshot_date()
        yield [Card(1, game.key, 10, "Set", "Card", "1", "R")], [PricePoint(1, "Normal", 1.0 + d.day, None, None, None)]


@pytest.fixture
def env(tmp_path):
    cfg = parse_config({"database": str(tmp_path / "t.db"),
                        "games": {"op": {"name": "One Piece", "category_id": 68}}}, base_dir=tmp_path)

    class Env:
        pass

    e = Env()
    e.cfg, e.checks, e.backfills, e.check_error = cfg, [], [], None
    e.clock = Clock(at(D, 20, 30))
    e.remote = Remote(e.clock, [(at(D1, 20, 0), D1)])

    def check(key):
        # R3: the attempt boundary is already committed (open row) before anything is posted.
        with Store(cfg.database) as s:
            assert [r["finished"] for r in s.job_rows("check", e.remote.snapshot_date(), key)] == [None]
        e.checks.append(key)
        if e.check_error:
            raise e.check_error

    def sched(**kw):
        return Scheduler(cfg, snapshot_fn=cli._snapshot_game,
                         backfill_fn=lambda key, days: e.backfills.append((key, days)),
                         check_fn=check, source_for=lambda g: e.remote, now=e.clock.now,
                         sleep=e.clock.sleep, lock_wait=0, retry_delays=(1.0, 2.0), **kw)

    e.sched = sched
    e.store = lambda: Store(cfg.database)
    return e


def store_day(env, day):
    with env.store() as s:
        s.write_snapshot(day, [([Card(1, "op", 10, "Set", "Card", "1", "R")],
                                [PricePoint(1, "Normal", 1.0, None, None, None)])])


def done_day(env, day):
    """A day whose whole workflow already ran (as an earlier tick would have left it)."""
    store_day(env, day)
    with env.store() as s:
        s.job_record("snapshot", True, dataset_date=day, game="op")
        s.job_record("check", True, dataset_date=day, game="op")


def ticks(env):
    with env.store() as s:
        return [dict(r) for r in s.conn.execute("SELECT * FROM job_runs WHERE kind='schedule' ORDER BY id")]


def rows(env, kind):
    with env.store() as s:
        return [dict(r) for r in s.conn.execute("SELECT * FROM job_runs WHERE kind=? ORDER BY id", (kind,))]


# --- pure date logic ---------------------------------------------------------

def test_expected_date_flips_at_the_wall_clock_time():
    assert expected_date(at(D, 20, 29), time(20, 30)) == D1
    assert expected_date(at(D, 20, 30), time(20, 30)) == D
    assert expected_date(at(D, 0, 0), time(20, 30)) == D1


def test_expected_date_converts_to_utc():
    est = datetime(2026, 11, 6, 16, 30, tzinfo=timezone(timedelta(hours=-4)))  # 20:30Z
    assert expected_date(est, time(20, 30)) == D


def test_next_fire_is_strictly_after_now():
    assert next_fire(at(D, 10), time(20, 30)) == at(D, 20, 30)
    assert next_fire(at(D, 20, 30), time(20, 30)) == at(D + timedelta(days=1), 20, 30)


# --- acceptance: late publish ----------------------------------------------------

def test_late_publish_is_picked_up_at_2040_not_next_day(env):
    done_day(env, D1)  # stored D-1, remote still D-1 at 20:30
    env.remote.publications.append((at(D, 20, 40), D))
    assert env.sched().tick() is True
    assert env.clock.t == at(D, 20, 40)
    assert env.clock.sleeps == [600.0]
    assert env.remote.fetches == 1 and env.checks == ["op"]
    with env.store() as s:
        assert s.has_prices("op", D)
        assert s.job_done("check", D, "op")
    assert ticks(env)[-1]["ok"] == 1 and ticks(env)[-1]["dataset_date"] == str(D)


def test_window_expiry_records_a_stale_failure(env):
    done_day(env, D1)
    assert env.sched().tick() is False
    assert env.clock.t == at(D, 23, 30)  # polled every 10 min until at + 3h, no further
    assert set(env.clock.sleeps) == {600.0} and len(env.clock.sleeps) == 18
    t = ticks(env)[-1]
    assert t["ok"] == 0 and f"stale: remote {D1}, expected {D}, window expired" in t["note"]
    assert env.checks == [] and env.remote.fetches == 0


def test_window_expiry_then_next_startup_catches_up_the_late_dataset(env):
    done_day(env, D1)
    env.sched().tick()
    env.remote.publications.append((at(D, 23, 50), D))
    env.clock.t = at(D + timedelta(days=1), 9)  # next morning, before the new deadline
    assert env.sched().tick(startup=True) is True
    assert env.checks == ["op"] and ticks(env)[-1]["dataset_date"] == str(D)


def test_pre_deadline_startup_with_remote_older_than_yesterday_still_fails_stale(env):
    env.clock.t = at(D, 10)  # E = D-1; remote is D-2
    env.remote.publications = [(at(D1 - timedelta(days=1), 20), D1 - timedelta(days=1))]
    assert env.sched().tick(startup=True) is False
    assert env.clock.sleeps == []  # window for D-1 long gone: no polling
    t = ticks(env)[-1]
    assert t["ok"] == 0 and f"stale: remote {D1 - timedelta(days=1)}, expected {D1}" in t["note"]


def test_pre_deadline_startup_with_yesterday_complete_is_a_quiet_ok(env):
    env.clock.t = at(D, 10)
    done_day(env, D1)
    assert env.sched().tick(startup=True) is True
    assert env.clock.sleeps == [] and env.checks == []
    assert ticks(env)[-1]["ok"] == 1


def test_startup_after_publish_but_before_deadline_processes_today_immediately(env):
    env.clock.t = at(D, 20, 10)
    env.remote.publications.append((at(D, 20, 0), D))
    assert env.sched().tick(startup=True) is True
    assert env.clock.sleeps == [] and env.checks == ["op"]
    assert env.clock.t == at(D, 20, 10)


# --- acceptance: workflow state ---------------------------------------------------

def test_snapshot_only_dataset_without_check_row_gets_its_check(env):
    env.remote.publications.append((at(D, 20, 0), D))
    store_day(env, D)  # user ran `snapshot` by hand: data complete, no workflow rows
    assert env.sched().tick() is True
    assert env.checks == ["op"] and env.remote.fetches == 0


def test_crash_between_snapshot_and_check_resumes_with_the_check(env):
    env.remote.publications.append((at(D, 20, 0), D))
    store_day(env, D)
    with env.store() as s:
        s.job_record("snapshot", True, dataset_date=D, game="op")  # no check row at all
    env.sched().tick(startup=True)
    assert env.checks == ["op"]


def test_crash_during_posting_closes_the_open_row_and_never_reposts(env):
    env.remote.publications.append((at(D, 20, 0), D))
    store_day(env, D)
    with env.store() as s:
        s.job_start("check", D, "op")  # process died with the row open
    assert env.sched().tick(startup=True) is False
    assert env.checks == []
    chk = rows(env, "check")
    assert [(r["ok"], r["note"]) for r in chk] == [(0, "interrupted")]
    assert "not auto-retried" in ticks(env)[-1]["note"]
    assert env.sched().tick(startup=True) is False and env.checks == []  # still failed, no re-post
    assert ticks(env)[-1]["ok"] == 0


def test_fully_done_dataset_is_a_noop_ok_row(env):
    env.remote.publications.append((at(D, 20, 0), D))
    sched = env.sched()
    assert sched.tick() is True and env.checks == ["op"]
    with env.store() as s:  # make the catch-up window fully satisfied
        for i in range(1, env.cfg.catch_up_days + 1):
            s.upsert_prices(D - timedelta(days=i), [PricePoint(1, "Normal", 1.0, None, None, None)])
    env.backfills.clear()
    assert sched.tick() is True
    assert env.checks == ["op"] and env.backfills == []
    assert ticks(env)[-1]["ok"] == 1 and ticks(env)[-1]["note"] == "nothing to do"


# --- retries (R3) ------------------------------------------------------------------

def test_snapshot_retried_then_succeeds(env):
    env.remote.publications.append((at(D, 20, 0), D))
    real = env.remote.fetch_singles
    calls = {"n": 0}

    def flaky(game):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("boom")
        yield from real(game)

    env.remote.fetch_singles = flaky
    assert env.sched().tick() is True
    assert calls["n"] == 3 and env.clock.sleeps == [1.0, 2.0]
    assert [r["ok"] for r in rows(env, "snapshot")] == [1]


def test_snapshot_failure_after_retries_fails_the_tick_and_skips_check(env):
    env.remote.publications.append((at(D, 20, 0), D))

    def down(game):
        raise OSError("down")
        yield

    env.remote.fetch_singles = down
    assert env.sched().tick() is False
    assert env.checks == [] and [r["ok"] for r in rows(env, "snapshot")] == [0]
    assert "failed after 3 attempt" in rows(env, "snapshot")[0]["note"]


def test_check_is_never_retried_and_failure_is_not_rerun_next_tick(env):
    env.remote.publications.append((at(D, 20, 0), D))
    env.check_error = RuntimeError("discord exploded")
    sched = env.sched()
    assert sched.tick() is False
    assert env.checks == ["op"] and env.clock.sleeps == []
    assert [r["ok"] for r in rows(env, "check")] == [0]
    sched.tick()
    assert env.checks == ["op"]  # a human re-runs `tcgwatch check`
    assert "not auto-retried" in ticks(env)[-1]["note"]


# --- catch-up -----------------------------------------------------------------------

def test_catch_up_backfills_missing_days_without_posting(env):
    env.remote.publications.append((at(D, 20, 0), D))
    assert env.sched().tick() is True
    assert env.backfills == [("op", 7)]
    assert env.checks == ["op"]  # exactly one check, for the latest day only
    assert rows(env, "catchup")[0]["ok"] == 1


def test_catch_up_failure_does_not_block_the_check(env):
    env.remote.publications.append((at(D, 20, 0), D))
    sched = env.sched()

    def bad(key, days):
        raise OSError("archive down")

    sched.backfill_fn = bad
    assert sched.tick() is False
    assert env.checks == ["op"] and [r["ok"] for r in rows(env, "catchup")] == [0]


# --- locking -----------------------------------------------------------------------------

def test_busy_lock_skips_the_tick_with_an_ok_row(env):
    env.remote.publications.append((at(D, 20, 0), D))
    ready, release = threading.Event(), threading.Event()

    # in-process locks are re-entrant per thread, so hold it from another thread
    def holder():
        with job_lock(env.cfg.database, "backfill"):
            ready.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    ready.wait(5)
    try:
        assert env.sched().tick() is True
    finally:
        release.set(); t.join(5)
    assert env.checks == [] and ticks(env)[-1]["note"].startswith("skipped: busy")
    assert ticks(env)[-1]["ok"] == 1


# --- loop ------------------------------------------------------------------------------------

def test_run_forever_ticks_at_startup_then_each_day_at_the_wall_clock_time(env):
    env.clock.t = at(D, 10)
    env.remote.publications += [(at(D, 20, 0), D), (at(D + timedelta(days=1), 20, 0), D + timedelta(days=1))]
    sched = env.sched()
    seen = []
    real = sched.tick

    def tick(startup=False):
        seen.append((env.clock.t, startup))
        if len(seen) == 3:
            raise KeyboardInterrupt
        return real(startup=startup)

    sched.tick = tick
    with pytest.raises(KeyboardInterrupt):
        sched.run_forever()
    assert seen == [(at(D, 10), True), (at(D, 20, 30), False), (at(D + timedelta(days=1), 20, 30), False)]


# --- crash survival -------------------------------------------------------------------------

def test_tick_crash_leaves_a_failed_schedule_row_and_the_loop_keeps_running(env, capsys):
    env.clock.t = at(D, 10)
    sched = env.sched()
    calls = []

    def tick(startup=False):
        calls.append(startup)
        if len(calls) == 1:
            raise ValueError("boom")
        if len(calls) == 2:
            raise KeyboardInterrupt  # stops the endless loop after the survived crash
        return True

    sched.tick = tick
    with pytest.raises(KeyboardInterrupt):
        sched.run_forever()
    assert calls == [True, False]  # the startup crash did not end the loop
    crashed = ticks(env)
    assert [(r["ok"], r["note"]) for r in crashed] == [(0, "crashed: ValueError: boom")]
    err = capsys.readouterr().err
    assert "Traceback" in err and "ValueError: boom" in err


def test_crash_row_write_failing_too_prints_both_tracebacks_and_propagates_nothing(env, capsys):
    sched = env.sched()

    def tick(startup=False):
        raise ValueError("boom")

    class BrokenStore:
        def __init__(self, *a, **k):
            raise sqlite3.OperationalError("disk gone")

    sched.tick = tick
    with patch("tcgwatch.scheduler.Store", BrokenStore):
        sched._guarded_tick(startup=False)  # must not raise
    err = capsys.readouterr().err
    assert err.count("Traceback") >= 2  # the inner one chains the outer, so 3 headers appear
    assert "ValueError: boom" in err and "OperationalError: disk gone" in err
    assert ticks(env) == []


def test_remote_date_source_error_is_logged_and_treated_as_not_published(env, capsys):
    class Down:
        def snapshot_date(self):
            raise RuntimeError("tcgcsv 503")

    sched = env.sched()
    sched.source_for = lambda g: Down()
    games = env.cfg.selected_games(None)
    assert sched._remote_dates(games) == {"op": None}
    out = capsys.readouterr().out
    assert "One Piece: cannot read remote date: tcgcsv 503" in out


# --- config / cli -----------------------------------------------------------------------------

def test_schedule_config_defaults_and_validation(tmp_path):
    cfg = parse_config({}, base_dir=tmp_path)
    assert cfg.at == time(20, 30) and cfg.late_window == timedelta(hours=3)
    assert cfg.check_every is None and cfg.catch_up_days == 7
    for bad in ({"at": "25:00"}, {"at": "8pm"}, {"late_window": "4h"}, {"catch_up_days": 0},
                {"catch_up_days": 91}, {"catch_up_days": "7"}):
        with pytest.raises(ConfigError):
            parse_config({"schedule": bad}, base_dir=tmp_path)
    assert parse_config({"schedule": {"at": "6:05"}}, base_dir=tmp_path).at == time(6, 5)


def test_catch_up_days_default_follows_largest_alert_window_capped_at_30(tmp_path):
    big = parse_config({"alert": {"window": "2w"}}, base_dir=tmp_path)
    assert big.catch_up_days == 14
    game = parse_config({"games": {"g": {"category_id": 1, "alert": {"window": "10w"}}}}, base_dir=tmp_path)
    assert game.catch_up_days == 30
    explicit = parse_config({"schedule": {"catch_up_days": 45}}, base_dir=tmp_path)
    assert explicit.catch_up_days == 45


def test_watch_is_an_alias_and_check_every_warns(tmp_path, monkeypatch, capsys):
    import argparse
    assert cli.cmd_watch is cli.cmd_schedule
    cfg = parse_config({"schedule": {"check_every": "6h"}, "database": str(tmp_path / "t.db")}, base_dir=tmp_path)

    class Stop(Exception):
        pass

    monkeypatch.setattr(cli, "cmd_run", lambda c, a: 0)
    monkeypatch.setattr(cli.time, "sleep", lambda s: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        cli.cmd_schedule(cfg, argparse.Namespace())
    assert "check_every is deprecated" in capsys.readouterr().err


def test_schedule_and_watch_subcommands_parse():
    p = cli.build_parser()
    assert p.parse_args(["schedule"]).func is cli.cmd_schedule
    assert p.parse_args(["watch"]).func is cli.cmd_schedule


# --- FIX-C5 regressions (R-C5 blockers) ----------------------------------------------

def test_failed_check_keeps_ticks_failed_on_every_tick_and_restart(env):
    env.remote.publications.append((at(D, 20, 0), D))
    env.check_error = RuntimeError("post failed")
    s = env.sched()
    assert [s.tick(), s.tick(), env.sched().tick(startup=True)] == [False, False, False]
    assert env.checks == ["op"] and [t["ok"] for t in ticks(env)] == [0, 0, 0]


def _real_check_env(env):
    env.remote.publications.append((at(D, 20, 0), D))
    store_day(env, D)
    with env.store() as st:
        st.upsert_prices(D - timedelta(days=7), [PricePoint(1, "Normal", 0.1, None, None, None)])
    cfg = replace(env.cfg, discord_webhook_url="https://invalid.example/test")
    s = env.sched()
    s.check_fn = lambda key: cli._check(cfg, cli._scheduled_check_args(cli.build_parser().parse_args(["watch"]), key),
                                        strict=True)
    return s


def test_real_total_discord_failure_closes_check_and_tick_failed_without_repost(env):
    s = _real_check_env(env)
    with patch.object(cli, "send_discord", side_effect=OSError("down")) as send:
        assert s.tick() is False
        assert s.tick() is False
        assert send.call_count == 1
    assert [r["ok"] for r in rows(env, "check")] == [0]
    assert "Discord delivery failed" in rows(env, "check")[0]["note"]


def test_real_partial_discord_failure_records_delivered_prefix_and_fails(env):
    s = _real_check_env(env)
    with patch.object(cli, "send_discord", side_effect=cli.DiscordPartialError("cut", [])):
        assert s.tick() is False
    assert [r["ok"] for r in rows(env, "check")] == [0]


def test_publication_advancing_after_the_gate_keys_check_under_the_stored_day(env):
    env.clock.t = at(D, 20, 10)
    env.remote.publications = [(at(D1, 20), D1)]
    s = env.sched()
    checked = []

    def advancing_snapshot(store, source, game):
        env.remote.publications.append((env.clock.t, D))
        cli._snapshot_game(store, source, game)

    s.snapshot_fn = advancing_snapshot
    def check(key):
        with env.store() as st:
            checked.append(st.latest_snapshot_date(key))

    s.check_fn = check
    assert s.tick() is True and s.tick() is True
    assert checked == [D]
    assert [r["dataset_date"] for r in rows(env, "check")] == [str(D)]


def test_publication_advancing_while_waiting_for_the_lock_is_not_rechecked(env):
    store_day(env, D1)
    env.remote.publications = [(at(D1, 20), D1), (at(D, 20, 45), D)]
    env.clock.t = at(D, 20, 40)
    s = env.sched()
    real_remote_dates = s._remote_dates
    s._remote_dates = lambda games: (lambda r: (setattr(env.clock, "t", at(D, 20, 50)), r)[1])(real_remote_dates(games))
    assert s.tick() is True
    assert s.tick() is True
    assert env.checks == ["op"]


def _cfg_two_games(env):
    games = {**env.cfg.games, "other": replace(env.cfg.games["op"], key="other")}
    return replace(env.cfg, games=games)


def test_cmd_schedule_passes_game_selection_and_check_overrides(env, monkeypatch):
    cfg = _cfg_two_games(env)
    args = cli.build_parser().parse_args(["watch", "--game", "op", "--threshold", "99", "--quiet"])
    seen = {}

    class Capture:
        def __init__(self, cfg, **kw):
            seen["cfg"], seen["kw"] = cfg, kw

        def run_forever(self):
            pass

    monkeypatch.setattr(cli, "Scheduler", Capture)
    cli.cmd_schedule(cfg, args)
    assert seen["kw"]["game_keys"] == ["op"]
    got = []
    monkeypatch.setattr(cli, "_check", lambda c, a, strict=False: got.append((a.game, a.threshold, a.quiet, strict)))
    seen["kw"]["check_fn"]("op")
    assert got == [(["op"], 99.0, True, True)]
    s = Scheduler(cfg, game_keys=["op"], snapshot_fn=None, backfill_fn=None, check_fn=None, source_for=None)
    assert [g.key for g in cfg.selected_games(s.game_keys)] == ["op"]


def test_cmd_schedule_rejects_preview_before_any_work(env):
    args = cli.build_parser().parse_args(["watch", "--preview"])
    with pytest.raises(ConfigError, match="--preview"):
        cli.cmd_schedule(env.cfg, args)


@pytest.mark.parametrize("fail_after", [False, True])
def test_publication_advancing_during_real_catch_up_keys_check_under_the_stored_day(env, fail_after):
    from unittest.mock import patch
    from tcgwatch import cli
    env.clock.t = at(D, 20, 10)
    env.remote.publications = [(at(D1, 20), D1)]
    env.remote.resolve_category = lambda game: game.category_id
    env.remote.archive_prices = lambda day, categories: None
    s, checked = env.sched(), []

    def advancing_backfill(key, days):  # production _backfill snapshots the newly published day itself
        env.remote.publications.append((env.clock.t, D))
        with patch.object(cli, "build_source", return_value=env.remote):
            cli._backfill(env.cfg, cli.argparse.Namespace(game=[key], days=days, force=False))
        if fail_after:
            raise RuntimeError("archive died after the snapshot")

    def check(key):
        with env.store() as st:
            checked.append(st.latest_snapshot_date(key))

    s.backfill_fn, s.check_fn = advancing_backfill, check
    s.retry_delays = ()
    results = [s.tick(), s.tick()]
    assert results == [not fail_after] * 2
    assert checked == [D]
    assert [r["dataset_date"] for r in rows(env, "check")] == [str(D)]


def test_health_command(tmp_path, capsys):
    from datetime import datetime, timedelta, timezone
    from tcgwatch import cli
    from tcgwatch.config import parse_config
    from tcgwatch.store import Store
    cfg = parse_config({"database": str(tmp_path / "h.db")}, base_dir=tmp_path)
    args = cli.build_parser().parse_args(["health"])
    assert cli.cmd_health(cfg, args) == 1 and not cfg.database.exists()  # no DB: unhealthy, not created
    with Store(cfg.database) as s:
        assert cli.cmd_health(cfg, args) == 1  # no rows
        s.job_record("schedule", False, "crashed")
        assert cli.cmd_health(cfg, args) == 1  # only failed
        rid = s.job_record("schedule", True, "skipped: busy")
    assert cli.cmd_health(cfg, args) == 0
    def age(h):
        old = (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat(timespec="seconds")
        with Store(cfg.database) as s, s.conn:
            s.conn.execute("UPDATE job_runs SET finished=? WHERE id=?", (old, rid))
    age(27)  # supported 3h late publication: still healthy under the derived 28h default
    assert cli.cmd_health(cfg, args) == 0
    age(29)
    assert cli.cmd_health(cfg, args) == 1
    assert cli.cmd_health(cfg, cli.build_parser().parse_args(["health", "--max-age", "26h"])) == 1  # override
    assert cli.cmd_health(cfg, cli.build_parser().parse_args(["health", "--max-age", "2d"])) == 0

def _repair_sched(env, fn):
    s = env.sched(repair_fn=fn)
    return s


def test_repair_runs_once_per_game_and_is_recorded(env):
    env.remote.publications.append((at(D, 20, 0), D))
    calls = []
    sched = _repair_sched(env, lambda key, days: calls.append((key, days)))
    assert sched.tick() is True
    assert calls == [("op", 7)]
    r = rows(env, "repair")
    assert [(x["ok"], x["game"]) for x in r] == [(1, "op")] and r[0]["finished"]
    assert sched.tick() is True
    assert calls == [("op", 7)]  # closed ok=1 row suppresses it


def test_failed_or_interrupted_repair_is_retried_next_tick(env):
    env.remote.publications.append((at(D, 20, 0), D))
    calls = []

    def bad(key, days):
        calls.append(key)
        raise RuntimeError("backfill --force exited 1")

    sched = _repair_sched(env, bad)
    assert sched.tick() is False
    assert env.checks == ["op"]  # normal workflow still ran
    with env.store() as s:
        s.job_start("repair", D, "op")  # an interrupted attempt
    sched.repair_fn = lambda key, days: calls.append("ok")
    sched.tick()
    assert calls == ["op", "ok"]
    assert [x["ok"] for x in rows(env, "repair")] == [0, 0, 1]
    sched.tick()
    assert calls == ["op", "ok"]


@pytest.mark.parametrize("catch_up,expect", [(30, 30), (31, 30), (90, 30), (7, 7)])
def test_production_repair_caps_days_at_force_limit(tmp_path, monkeypatch, catch_up, expect):
    from tcgwatch import cli
    from tcgwatch.config import parse_config
    cfg = parse_config({"database": str(tmp_path / "r.db"), "games": {"op": {"category_id": 68}},
                        "schedule": {"catch_up_days": catch_up}}, base_dir=tmp_path)
    seen = []
    real = cli._backfill
    monkeypatch.setattr(cli, "_backfill", lambda c, a: (seen.append(a.days), real(c, a))[1])
    monkeypatch.setattr(cli, "build_source", lambda *a, **k: object())  # no archive: returns 0 after bounds check
    cli._repair_game(cfg, "op", cfg.catch_up_days)  # real _repair_game/_backfill: no ConfigError
    assert seen == [expect]


# --- RET-1: prune stage -----------------------------------------------------

def test_prune_runs_after_a_successful_snapshot_and_records_a_row(env):
    old = D1 - timedelta(days=400)
    store_day(env, old)
    env.sched().tick(startup=True)
    pr = rows(env, "prune")
    assert len(pr) == 1 and pr[0]["ok"] == 1 and pr[0]["game"] == "op"
    assert "removed 1 prices" in pr[0]["note"]
    with env.store() as s:
        assert not s.has_prices("op", old) and s.has_prices("op", D1)


def test_prune_skipped_when_nothing_was_snapshotted(env):
    done_day(env, D1)
    env.sched().tick(startup=True)
    assert rows(env, "prune") == []


def test_prune_off_when_retention_disabled(env):
    object.__setattr__(env.cfg, "retention_days", None)
    store_day(env, D1 - timedelta(days=400))
    env.sched().tick(startup=True)
    assert rows(env, "prune") == []

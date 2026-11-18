"""CLI error paths and small commands (games, sets, discord-test, check messages, schedule wiring).

Deterministic: no network (sources and Discord are stubbed), no real sleeping.
"""

import argparse
import runpy
import sys
from dataclasses import replace

import pytest
import yaml

from tcgwatch import cli
from tcgwatch.config import ConfigError, parse_config
from tcgwatch.joblock import JobLockedError


def _args(*argv):
    return cli.build_parser().parse_args(list(argv))


# --- _rule_overrides -------------------------------------------------------

def test_rule_overrides_apply_each_flag(example_cfg):
    rule = example_cfg.games["one_piece"].alert
    out = cli._rule_overrides(
        _args("check", "--threshold", "12.5", "--direction", "down", "--min-price", "3",
              "--discord-threshold", "40"),
        rule)
    assert out.threshold_pct == 12.5
    assert out.direction == "down"
    assert out.min_price == 3
    assert out.discord_threshold_pct == 40


def test_rule_overrides_without_flags_returns_the_same_rule(example_cfg):
    rule = example_cfg.games["one_piece"].alert
    assert cli._rule_overrides(_args("check"), rule) is rule


def test_rule_overrides_window_sets_days(example_cfg):
    rule = example_cfg.games["one_piece"].alert
    assert cli._rule_overrides(_args("check", "--window", "2w"), rule).window_days == 14


def test_rule_overrides_invalid_direction_is_rejected_by_validation(example_cfg):
    # argparse restricts --direction choices; a bad value from code must still fail validation.
    rule = example_cfg.games["one_piece"].alert
    args = _args("check")
    args.direction = "sideways"
    with pytest.raises(ConfigError):
        cli._rule_overrides(args, rule)


# --- _fmt_sets / cmd_games / cmd_sets -------------------------------------

def test_fmt_sets_renders_all_when_empty_and_list_otherwise():
    assert cli._fmt_sets(()) == "all"
    assert cli._fmt_sets((1, "Base")) == "1, Base"


def test_games_lists_each_configured_game(example_cfg, capsys):
    assert cli.cmd_games(example_cfg, _args("games")) == 0
    out = capsys.readouterr().out
    for g in example_cfg.games.values():
        assert g.key in out and g.name in out
    assert "discord (off)" in out  # example config has no webhook
    assert "history: 0 day(s)" in out


def test_games_reports_discord_on_when_a_webhook_is_set(example_cfg, capsys):
    cfg = replace(example_cfg, discord_webhook_url="https://example.invalid/hook")
    cli.cmd_games(cfg, _args("games"))
    assert "discord (on)" in capsys.readouterr().out


class _SetsStore:
    rows = []

    def __init__(self, *_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sets(self, key):
        return list(self.rows)


def test_sets_says_none_yet_when_no_snapshots(example_cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Store", _SetsStore)
    monkeypatch.setattr(_SetsStore, "rows", [])
    assert cli.cmd_sets(example_cfg, _args("sets")) == 0
    out = capsys.readouterr().out
    assert "0 set(s)" in out and "run `tcgwatch snapshot` first" in out


def test_sets_marks_checked_and_filtered_sets(example_cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Store", _SetsStore)
    monkeypatch.setattr(_SetsStore, "rows", [
        {"group_id": 10, "group_name": "Booster Alpha", "singles": 5},
        {"group_id": 11, "group_name": "Promo Junk", "singles": 2},
    ])
    cfg = replace(example_cfg, games={k: replace(g, exclude_sets=(11,)) if k == "one_piece" else g
                                      for k, g in example_cfg.games.items()})
    cli.cmd_sets(cfg, _args("sets", "--game", "one_piece"))
    out = capsys.readouterr().out
    assert "2 set(s)" in out
    assert "+     10  Booster Alpha  (5 singles)" in out
    assert "-     11  Promo Junk  (2 singles)" in out


# --- cmd_discord_test -----------------------------------------------------

def test_discord_test_without_webhook_fails_with_hint(example_cfg, capsys):
    assert cli.cmd_discord_test(example_cfg, _args("discord-test")) == 1
    assert "no Discord webhook configured" in capsys.readouterr().err


def test_discord_test_posts_once_when_configured(example_cfg, monkeypatch, capsys):
    posted = []
    monkeypatch.setattr(cli, "post_discord", lambda url, text: posted.append((url, text)))
    cfg = replace(example_cfg, discord_webhook_url="https://example.invalid/hook")
    assert cli.cmd_discord_test(cfg, _args("discord-test")) == 0
    assert posted and posted[0][0] == "https://example.invalid/hook"
    assert "works" in posted[0][1]
    assert "posted test message to Discord" in capsys.readouterr().out


# --- backfill / repair bounds --------------------------------------------

def test_backfill_rejects_zero_days(example_cfg):
    with pytest.raises(ConfigError, match="--days must be at least 1"):
        cli._backfill(example_cfg, argparse.Namespace(game=[], days=0, force=False))


def test_repair_game_caps_days_at_the_force_limit(example_cfg, monkeypatch):
    seen = {}

    def fake_backfill(cfg, args):
        seen.update(game=args.game, days=args.days, force=args.force)
        return 0

    monkeypatch.setattr(cli, "_backfill", fake_backfill)
    cli._repair_game(example_cfg, "one_piece", days=cli.MAX_FORCE_DAYS + 50)
    assert seen == {"game": ["one_piece"], "days": cli.MAX_FORCE_DAYS, "force": True}


def test_repair_game_passes_short_catch_up_through_unchanged(example_cfg, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_backfill", lambda cfg, args: seen.update(days=args.days) or 0)
    cli._repair_game(example_cfg, "one_piece", days=7)
    assert seen["days"] == 7


def test_repair_game_raises_when_backfill_exits_nonzero(example_cfg, monkeypatch):
    monkeypatch.setattr(cli, "_backfill", lambda cfg, args: 4)
    with pytest.raises(RuntimeError, match="backfill --force exited 4"):
        cli._repair_game(example_cfg, "one_piece", days=3)


# --- _check messages (stubbed store) --------------------------------------

class _EmptyStore:
    """A store with one snapshot day but nothing to compare in the window."""

    def __init__(self, *_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def latest_snapshot_date(self, key):
        return None

    def snapshot_days(self, key):
        return 1

    def observations(self, key, days):
        return iter(())

    def watched(self, key):
        return []

    def watched_current(self, key, fields):
        return []


class _SnapshotStore(_EmptyStore):
    def latest_snapshot_date(self, key):
        return "2026-01-01"


def test_check_says_no_snapshots_yet(example_cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Store", _EmptyStore)
    rc = cli._check(example_cfg, _args("check", "--game", "one_piece"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no snapshots yet - run `tcgwatch snapshot` first" in out


def test_check_says_nothing_to_compare_for_the_window(example_cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Store", _SnapshotStore)
    rc = cli._check(example_cfg, _args("check", "--game", "one_piece", "--window", "7d"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing to compare for a 7d window (1 snapshot day(s) stored" in out


# --- interval loop: a locked DB skips the tick ----------------------------

def test_interval_loop_skips_a_tick_when_the_db_is_locked(tmp_path, monkeypatch, capsys):
    cfg = parse_config(
        {"schedule": {"check_every": "1h"}, "database": str(tmp_path / "t.db")}, base_dir=tmp_path)

    def locked(c, a):
        raise JobLockedError("check", {"pid": 1})

    class Stop(Exception):
        pass

    def stop_sleep(seconds):
        raise Stop()

    monkeypatch.setattr(cli, "cmd_run", locked)
    monkeypatch.setattr(cli.time, "sleep", stop_sleep)
    with pytest.raises(Stop):
        cli.cmd_watch(cfg, argparse.Namespace())
    assert "skipping tick:" in capsys.readouterr().out


# --- schedule wiring: one source instance per source name -----------------

def test_schedule_builds_each_source_once_and_reuses_it(example_cfg, monkeypatch):
    built = []
    monkeypatch.setattr(cli, "build_source", lambda name, cfg: built.append(name) or object())
    captured = {}

    class FakeScheduler:
        def __init__(self, cfg, **kwargs):
            captured.update(kwargs)

        def run_forever(self):
            return None

    monkeypatch.setattr(cli, "Scheduler", FakeScheduler)
    cfg = replace(example_cfg, check_every=None)
    assert cli.cmd_schedule(cfg, _args("schedule")) == 0

    game = type("G", (), {"source": "tcgcsv"})()
    first = captured["source_for"](game)
    second = captured["source_for"](game)
    assert first is second
    assert built == ["tcgcsv"]


def test_schedule_refuses_preview(example_cfg):
    with pytest.raises(ConfigError, match="--preview is not supported"):
        cli.cmd_schedule(replace(example_cfg, check_every=None), _args("schedule", "--preview"))


# --- main() entry point ---------------------------------------------------

def test_module_entry_point_exits_with_main_result(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"database": str(tmp_path / "t.db")}))
    monkeypatch.setattr(sys, "argv", ["tcgwatch", "--config", str(path), "discord-test"])
    with pytest.raises(SystemExit) as e:
        runpy.run_module("tcgwatch", run_name="__main__")
    assert e.value.code == 1  # no webhook configured in this temp config

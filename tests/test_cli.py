import contextlib

import pytest

from tcgwatch import cli
from tcgwatch.config import ConfigError


class _Cfg:
    def selected_games(self, keys):
        return [type("G", (), {"name": "Game", "source": "x"})()]

    database = ":memory:"
    lock_wait = 0


class _Store:
    def __init__(self, *_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("quiet,expect", [(False, True), (True, False)])
def test_snapshot_quiet(monkeypatch, capsys, quiet, expect):
    class Src:
        def snapshot_date(self):
            return "2026-01-01"

        def fetch_singles(self, game):
            return iter(())

    class S(_Store):
        def write_snapshot(self, snap, batches, replace_game=None):
            return 0, 0

    monkeypatch.setattr(cli, "Store", S)
    monkeypatch.setattr(cli, "job_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(cli, "build_source", lambda *a: Src())
    args = cli.build_parser().parse_args(["snapshot"] + [])
    args.quiet = quiet
    cli.cmd_snapshot(_Cfg(), args)
    assert ("stored 0 prices" in capsys.readouterr().out) is expect


def test_run_quiet_flag_reaches_snapshot(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_snapshot_game", lambda s, src, g, *, replace=False, quiet=False: seen.update(q=quiet))
    monkeypatch.setattr(cli, "Store", _Store)
    monkeypatch.setattr(cli, "build_source", lambda *a: None)
    monkeypatch.setattr(cli, "cmd_check", lambda c, a: 0)
    args = cli.build_parser().parse_args(["run", "--quiet"])
    cli.cmd_run(_Cfg(), args)
    assert seen["q"] is True


def test_window_hours_message(example_cfg):
    args = cli.build_parser().parse_args(["check", "--window", "12h"])
    with pytest.raises(ConfigError) as e:
        cli._rule_overrides(args, example_cfg.games["one_piece"].alert)
    msg = str(e.value)
    assert "--window" in msg and "d (days)" in msg and "w (weeks)" in msg


def test_window_garbage_message(example_cfg):
    args = cli.build_parser().parse_args(["check", "--window", "abc"])
    with pytest.raises(ConfigError, match="--window.*d \\(days\\) or w \\(weeks\\)"):
        cli._rule_overrides(args, example_cfg.games["one_piece"].alert)


def test_snapshot_game_quiet_is_keyword_only():
    with pytest.raises(TypeError):
        cli._snapshot_game(None, None, None, True)

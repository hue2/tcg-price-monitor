"""`tcgwatch watch` loop: cadence, crash tolerance, Ctrl+C (no network, no real sleeping)."""

import argparse
from datetime import timedelta

import pytest

from tcgwatch import cli
from tcgwatch.config import parse_config


@pytest.fixture
def cfg(tmp_path):
    return parse_config({"schedule": {"check_every": "6h"}, "database": str(tmp_path / "t.db")},
                        base_dir=tmp_path)


class Stop(Exception):
    """Raised from the fake sleep to end the otherwise endless loop."""


def _loop(monkeypatch, cfg, runs_behaviour, max_sleeps):
    """Run cmd_watch with cmd_run scripted by runs_behaviour (a list; None = succeed)."""
    calls, sleeps = [], []
    script = iter(runs_behaviour)

    def fake_run(c, args):
        calls.append(c)
        action = next(script, None)
        if isinstance(action, BaseException):
            raise action
        return 0

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= max_sleeps:
            raise Stop()

    monkeypatch.setattr(cli, "cmd_run", fake_run)
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        cli.cmd_watch(cfg, argparse.Namespace())
    return calls, sleeps


def test_watch_runs_then_sleeps_check_every_between_runs(cfg, monkeypatch, capsys):
    calls, sleeps = _loop(monkeypatch, cfg, [], max_sleeps=3)
    assert len(calls) == 3
    assert sleeps == [timedelta(hours=6).total_seconds()] * 3
    assert "watching every 6:00:00" in capsys.readouterr().out


def test_watch_survives_a_failing_run_and_reports_it(cfg, monkeypatch, capsys):
    calls, sleeps = _loop(monkeypatch, cfg, [OSError("tcgcsv down"), None], max_sleeps=2)
    assert len(calls) == 2  # the second run still happened
    assert sleeps == [21600.0, 21600.0]  # the failed run still waits a full interval
    err = capsys.readouterr().err
    assert "OSError: tcgcsv down" in err and "Traceback" in err


def test_watch_survives_repeated_failures(cfg, monkeypatch):
    errors = [RuntimeError(f"boom {i}") for i in range(4)]
    calls, _ = _loop(monkeypatch, cfg, errors, max_sleeps=4)
    assert len(calls) == 4


def test_keyboard_interrupt_stops_the_watch_loop(cfg, monkeypatch):
    """Ctrl+C is not an Exception, so the crash guard must let it through."""
    monkeypatch.setattr(cli, "cmd_run", lambda c, a: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(cli.time, "sleep", lambda s: pytest.fail("slept after Ctrl+C"))
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_watch(cfg, argparse.Namespace())


def test_watch_command_exits_130_on_ctrl_c(cfg, tmp_path, monkeypatch):
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"schedule": {"check_every": "1h"}, "database": str(tmp_path / "t.db")}))
    monkeypatch.setattr(cli, "cmd_run", lambda c, a: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert cli.main(["--config", str(path), "watch"]) == 130

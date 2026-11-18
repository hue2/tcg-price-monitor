"""REL-2: a game's snapshot is all-or-nothing, and a dataset change mid-fetch is refetched."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from tcgwatch import cli
from tcgwatch.models import Card, PricePoint
from tcgwatch.store import Store

DAY = date(2026, 11, 6)
GAME = SimpleNamespace(key="op", name="One Piece", category_id=68)


def _set(gid: int):
    return [Card(gid, "op", gid, f"Set {gid}", f"Card {gid}", "1", "C")], \
        [PricePoint(gid, "Normal", float(gid), None, None, None)]


class Source:
    def __init__(self, dates, fail_after=None, n_sets=3):
        self.dates = list(dates)
        self.fail_after = fail_after
        self.n_sets = n_sets
        self.fetches = 0

    def snapshot_date(self):
        return self.dates.pop(0) if len(self.dates) > 1 else self.dates[0]

    def fetch_singles(self, game):
        self.fetches += 1
        for i in range(1, self.n_sets + 1):
            if self.fail_after is not None and i > self.fail_after:
                raise OSError("network dropped mid-run")
            yield _set(i)


def test_mid_run_failure_stores_nothing():
    with Store(":memory:") as s:
        with pytest.raises(OSError):
            cli._snapshot_game(s, Source([DAY], fail_after=2), GAME)
        assert s.latest_snapshot_date("op") is None
        assert s.product_ids("op") == set()


def test_mid_run_failure_keeps_previous_day_as_latest():
    prev = date(2026, 11, 5)
    with Store(":memory:") as s:
        cli._snapshot_game(s, Source([prev]), GAME)
        with pytest.raises(OSError):
            cli._snapshot_game(s, Source([DAY], fail_after=1), GAME)
        assert s.latest_snapshot_date("op") == prev
        assert not s.has_prices("op", DAY)


def test_write_failure_rolls_back_whole_game():
    bad = ([Card(9, "op", 9, "S", "C", "1", "C")], [PricePoint(9, "Normal", None, None, None, None)])
    with Store(":memory:") as s:
        s.conn.execute("CREATE TRIGGER boom BEFORE INSERT ON prices WHEN NEW.product_id = 9 "
                       "BEGIN SELECT RAISE(ABORT, 'boom'); END")
        with pytest.raises(Exception, match="boom"):
            s.write_snapshot(DAY, [_set(1), _set(2), bad])
        assert s.latest_snapshot_date("op") is None and s.product_ids("op") == set()


def test_complete_snapshot_stores_all_sets(capsys):
    with Store(":memory:") as s:
        cli._snapshot_game(s, Source([DAY]), GAME)
        assert s.product_ids("op") == {1, 2, 3}
        assert s.latest_snapshot_date("op") == DAY
    assert "stored 3 prices for 3 singles" in capsys.readouterr().out


def test_dataset_change_during_fetch_refetches():
    new = date(2026, 11, 7)
    src = Source([DAY, new, new])  # before, after (changed) -> retry: before, after
    with Store(":memory:") as s:
        cli._snapshot_game(s, src, GAME)
        assert src.fetches == 2
        assert s.snapshot_dates("op") == [new.isoformat()]


def test_dataset_keeps_changing_gives_up_without_writing():
    days = [date(2026, 11, d) for d in range(1, 12)]
    with Store(":memory:") as s:
        with pytest.raises(cli.SnapshotError):
            cli._snapshot_game(s, Source(days), GAME)
        assert s.latest_snapshot_date("op") is None


class TimestampSource(Source):
    """Like the real TcgCsvSource: exposes the full last-updated.txt timestamp."""

    def published_at(self):
        return self.dates.pop(0) if len(self.dates) > 1 else self.dates[0]

    def snapshot_date(self):
        raise AssertionError("stability check must use published_at, not the date")


def _ts(hh, mm):
    return datetime(2026, 11, 6, hh, mm, tzinfo=timezone.utc)


def test_same_day_republish_during_fetch_refetches():
    # 20:00 -> 20:05 on the same date must not be accepted as stable (R-REL-2).
    src = TimestampSource([_ts(20, 0), _ts(20, 5), _ts(20, 5)])
    with Store(":memory:") as s:
        cli._snapshot_game(s, src, GAME)
        assert src.fetches == 2
        assert s.snapshot_dates("op") == [DAY.isoformat()]  # key is still just the date


def test_same_day_republish_keeps_changing_gives_up_without_writing():
    stamps = [_ts(20, m) for m in range(0, 30)]
    src = TimestampSource(stamps)
    with Store(":memory:") as s:
        with pytest.raises(cli.SnapshotError):
            cli._snapshot_game(s, src, GAME)
        assert src.fetches == cli.SNAPSHOT_ATTEMPTS
        assert s.latest_snapshot_date("op") is None and s.product_ids("op") == set()


def test_tcgcsv_source_compares_full_last_updated_token():
    from tcgwatch.sources.tcgcsv import TcgCsvSource

    class Fake(TcgCsvSource):
        def __init__(self):
            super().__init__("test")
            self.stamps = [b"2026-11-06T20:00:00+00:00", b"2026-11-06T20:05:00+00:00",
                           b"2026-11-06T20:05:00+00:00", b"2026-11-06T20:05:00+00:00"]
            self.fetches = 0

        def _get(self, path):
            assert path == "/last-updated.txt"
            return self.stamps.pop(0)

        def fetch_singles(self, game):
            self.fetches += 1
            yield _set(1)

    src = Fake()
    with Store(":memory:") as s:
        cli._snapshot_game(s, src, GAME)
        assert src.fetches == 2
        assert s.snapshot_dates("op") == [DAY.isoformat()]

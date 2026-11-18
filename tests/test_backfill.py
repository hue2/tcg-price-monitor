from datetime import date

import pytest
import yaml

from tcgwatch import cli
from tcgwatch.models import Card, PricePoint
from tcgwatch.store import Store

TODAY = date(2026, 11, 6)


class FakeSource:
    def __init__(self):
        self.archive_days = []

    def snapshot_date(self):
        return TODAY

    def resolve_category(self, game):
        return game.category_id

    def fetch_singles(self, game):
        yield [Card(1, game.key, 10, "Set", "Card", "1", "C")], [PricePoint(1, "Normal", 2.0, None, None, None)]

    def archive_prices(self, day, categories):
        self.archive_days.append(day)
        if day == date(2026, 11, 2):
            return None  # missing archive
        # product 999 isn't in the catalog and must be ignored
        return {c: [PricePoint(1, "Normal", 1.0, None, None, None),
                    PricePoint(999, "Normal", 5.0, None, None, None)] for c in categories}


def test_backfill_fills_days_once(tmp_path, monkeypatch, capsys):
    db = tmp_path / "t.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"database": str(db), "games": {"op": {"category_id": 68}}}))
    fake = FakeSource()
    monkeypatch.setattr(cli, "build_source", lambda name, c: fake)

    assert cli.main(["--config", str(cfg), "backfill", "--days", "7"]) == 0
    with Store(db) as s:
        dates = s.snapshot_dates("op")
        assert dates[0] == "2026-10-30" and dates[-1] == "2026-11-06"
        assert "2026-11-02" not in dates and len(dates) == 7
        assert s.product_ids("op") == {1}
    assert "no archive available" in capsys.readouterr().out

    fake.archive_days.clear()
    cli.main(["--config", str(cfg), "backfill", "--days", "7"])
    assert fake.archive_days == [date(2026, 11, 2)]  # only the missing day is retried


def _partial_db(tmp_path):
    """A DB as a pre-atomic version could leave it: day 11-05 holds a stale price for
    product 1, a bogus row for product 2 that the archive no longer has, and misses product 3."""
    db = tmp_path / "t.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"database": str(db), "games": {"op": {"category_id": 68}}}))
    with Store(db) as s:
        s.upsert_cards([Card(i, "op", 10, "Set", f"Card {i}", str(i), "C") for i in (1, 2, 3)])
        s.upsert_prices(date(2026, 11, 5), [PricePoint(1, "Normal", 9.0, None, None, None),
                                            PricePoint(2, "Normal", 9.0, None, None, None)])
        s.upsert_prices(TODAY, [PricePoint(1, "Normal", 9.0, None, None, None)])
    return db, cfg


class RepairSource(FakeSource):
    def fetch_singles(self, game):
        yield ([Card(i, game.key, 10, "Set", f"Card {i}", str(i), "C") for i in (1, 2, 3)],
               [PricePoint(i, "Normal", 3.0, None, None, None) for i in (1, 2, 3)])

    def archive_prices(self, day, categories):
        self.archive_days.append(day)
        if day == date(2026, 11, 4):
            return {c: [] for c in categories}  # archive without this game's prices
        if day == date(2026, 11, 3):
            return None
        return {c: [PricePoint(1, "Normal", 1.0, None, None, None),
                    PricePoint(3, "Normal", 1.5, None, None, None)] for c in categories}


def _day(db, day):
    with Store(db) as s:
        return dict(s.conn.execute("SELECT product_id, market FROM prices WHERE snapshot_date = ?",
                                   (day.isoformat(),)).fetchall())


def test_force_rewrites_partial_days_atomically(tmp_path, monkeypatch, capsys):
    db, cfg = _partial_db(tmp_path)
    with Store(db) as s:
        s.upsert_prices(date(2026, 11, 4), [PricePoint(1, "Normal", 7.0, None, None, None)])
    fake = RepairSource()
    monkeypatch.setattr(cli, "build_source", lambda name, c: fake)
    sent = []
    monkeypatch.setattr(cli, "send_discord", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(cli, "post_discord", lambda *a, **k: sent.append(a))

    # Without --force, stored (partial) days are skipped.
    assert cli.main(["--config", str(cfg), "backfill", "--days", "3"]) == 0
    assert _day(db, date(2026, 11, 5)) == {1: 9.0, 2: 9.0}
    assert _day(db, TODAY) == {1: 9.0}

    assert cli.main(["--config", str(cfg), "backfill", "--days", "3", "--force"]) == 0
    assert _day(db, TODAY) == {1: 3.0, 2: 3.0, 3: 3.0}  # live snapshot re-fetched
    assert _day(db, date(2026, 11, 5)) == {1: 1.0, 3: 1.5}  # replaced exactly, stale row 2 gone
    assert _day(db, date(2026, 11, 4)) == {1: 7.0}  # empty archive never wipes a day
    out = capsys.readouterr().out
    assert "kept (archive has no prices)" in out and "no archive available" in out
    assert sent == []  # no Discord side effects


def test_force_failure_mid_write_keeps_old_day(tmp_path, monkeypatch):
    db, cfg = _partial_db(tmp_path)
    fake = RepairSource()
    monkeypatch.setattr(cli, "build_source", lambda name, c: fake)
    real = Store.upsert_prices

    def boom(self, day, points, *, commit=True):
        if day == date(2026, 11, 5):
            raise RuntimeError("disk full")
        return real(self, day, points, commit=commit)

    monkeypatch.setattr(Store, "upsert_prices", boom)
    try:
        cli.main(["--config", str(cfg), "backfill", "--days", "1", "--force"])
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the write to fail")
    assert _day(db, date(2026, 11, 5)) == {1: 9.0, 2: 9.0}  # delete rolled back with the insert


def test_force_is_bounded(tmp_path, monkeypatch):
    _, cfg = _partial_db(tmp_path)
    fake = RepairSource()
    monkeypatch.setattr(cli, "build_source", lambda name, c: fake)
    assert cli.main(["--config", str(cfg), "backfill", "--force", "--days", str(cli.MAX_FORCE_DAYS + 1)]) != 0
    assert fake.archive_days == []
    assert cli.main(["--config", str(cfg), "backfill", "--force"]) == 0
    assert len(fake.archive_days) == 7  # default window


def test_force_holds_job_lock(tmp_path, monkeypatch):
    _, cfg = _partial_db(tmp_path)
    kinds = []
    real = cli.job_lock

    def spy(db, kind, wait):
        kinds.append(kind)
        return real(db, kind, wait)

    monkeypatch.setattr(cli, "job_lock", spy)
    monkeypatch.setattr(cli, "build_source", lambda name, c: RepairSource())
    assert cli.main(["--config", str(cfg), "backfill", "--days", "1", "--force"]) == 0
    assert kinds == ["backfill"]


class EmptyLiveSource(RepairSource):
    """A stable live response without price points; archives are missing."""

    def __init__(self, batches):
        super().__init__()
        self.batches = batches

    def fetch_singles(self, game):
        yield from self.batches

    def archive_prices(self, day, categories):
        self.archive_days.append(day)
        return None


@pytest.mark.parametrize("batches", [
    [],  # empty iterator (no groups)
    [([Card(1, "op", 10, "Set", "Card 1", "1", "C")], [])],  # cards, no prices
    [([], []), ([], [])],  # fully empty batches
], ids=["empty-iterator", "cards-no-prices", "empty-batches"])
def test_force_never_replaces_latest_day_with_nothing(tmp_path, monkeypatch, capsys, batches):
    db, cfg = _partial_db(tmp_path)
    monkeypatch.setattr(cli, "build_source", lambda name, c: EmptyLiveSource(batches))
    assert cli.main(["--config", str(cfg), "backfill", "--days", "1", "--force"]) == 0
    assert _day(db, TODAY) == {1: 9.0}  # stored latest-day prices preserved
    assert _day(db, date(2026, 11, 5)) == {1: 9.0, 2: 9.0}
    assert "kept stored prices" in capsys.readouterr().out

from datetime import date

import pytest

from tcgwatch import cli
from tcgwatch.joblock import job_lock
from tcgwatch.models import Card, PricePoint
from tcgwatch.store import Store
from tcgwatch.watchlist import MAX_PRODUCT_ID, validate_watch

CONFIG = """\
database: {db}
games:
  op:
    name: One Piece
    category_id: 68
"""


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "t.db"
    with Store(db) as s:
        s.upsert_cards([Card(1, "op", 100, "Set A", "Spike", "A-1", "C")])
        s.upsert_prices(date(2026, 11, 6), [PricePoint(1, "Normal", 4.0, None, None, None)])
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    return db, ["--config", str(cfg), "watchlist"]


def test_add_list_remove(env, capsys):
    db, base = env
    assert cli.main(base + ["list"]) == 0
    assert "empty" in capsys.readouterr().out
    assert cli.main(base + ["add", "1", "Normal"]) == 0
    assert cli.main(base + ["add", "1", "Normal"]) == 0  # idempotent
    capsys.readouterr()
    assert cli.main(base + ["list", "--game", "op"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "Spike" in out and "Normal" in out
    assert cli.main(base + ["remove", "1", "Normal"]) == 0
    assert cli.main(base + ["remove", "1", "Normal"]) == 1  # not on the list
    assert "not on the watchlist" in capsys.readouterr().err


def test_add_unknown_and_invalid(env, capsys):
    _, base = env
    assert cli.main(base + ["add", "999", "Normal"]) == 1
    assert "no prices" in capsys.readouterr().err
    assert cli.main(base + ["add", "1", "Foil"]) == 1
    assert cli.main(base + ["add", "1", " "]) == 2
    assert cli.main(base + ["add", "0", "Normal"]) == 2
    assert cli.main(base + ["add", str(MAX_PRODUCT_ID + 1), "Normal"]) == 2
    assert "out of range" in capsys.readouterr().err


def test_writes_take_job_lock(env, capsys):
    db, base = env
    with job_lock(db, "snapshot"):
        import threading
        res = []
        t = threading.Thread(target=lambda: res.append(cli.main(base + ["add", "1", "Normal"])))
        t.start()
        t.join()
    assert res == [3]
    assert "busy" in capsys.readouterr().err


def test_list_without_database(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=(tmp_path / "none.db").as_posix()), encoding="utf-8")
    assert cli.main(["--config", str(cfg), "watchlist", "list"]) == 0
    assert "empty" in capsys.readouterr().out
    assert not (tmp_path / "none.db").exists()


def test_validate_watch():
    assert validate_watch(1, "Normal") is None
    assert validate_watch(True, "Normal") and validate_watch("1", "Normal") and validate_watch(1, None)


def test_list_corrupt_database_is_an_error_not_empty(env, capsys):
    db, base = env
    db.write_bytes(b"this is not a sqlite database" * 100)
    assert cli.main(base + ["list"]) == 1
    cap = capsys.readouterr()
    assert "cannot read watchlist" in cap.err and "empty" not in cap.out


def test_list_missing_table_is_an_error_not_empty(tmp_path, capsys):
    import sqlite3
    db = tmp_path / "bare.db"
    sqlite3.connect(db).close()  # valid sqlite file, no schema
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    assert cli.main(["--config", str(cfg), "watchlist", "list"]) == 1
    assert "no such table" in capsys.readouterr().err

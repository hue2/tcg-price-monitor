import sqlite3
from datetime import date, timedelta

import pytest

from tcgwatch import cli
from tcgwatch.models import Card, Mover, PricePoint
from tcgwatch.store import Store

CONFIG = "database: {db}\ngames:\n  op:\n    name: One Piece\n    category_id: 68\n"


def _mover(pid, name, game_cards):
    return Mover(card=game_cards[pid], printing="Normal", price_field="market", baseline_date="a", current_date="b", baseline_price=2.0,
                 current_price=3.0, pct_change=50.0)


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "t.db"
    cards = {1: Card(1, "op", 100, "Set A", "Spike", "A-1", "C"), 2: Card(2, "rb", 200, "Set B", "Jinx", "B-1", "R")}
    today = date.today()
    with Store(db) as s:
        s.upsert_cards(list(cards.values()))
        s.upsert_prices(today, [PricePoint(1, "Normal", 3.0, None, None, None)])
        s.record_alerts([_mover(1, "Spike", cards)], today - timedelta(days=40), 7, discord=True)
        s.record_alerts([_mover(2, "Jinx", cards)], today - timedelta(days=2), 7, discord=False)
        s.record_alerts([_mover(1, "Spike", cards)], today - timedelta(days=1), 7, discord=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    return db, ["--config", str(cfg), "alerts"]


def _rows(capsys):
    lines = capsys.readouterr().out.splitlines()
    return [ln.split("\t") for ln in lines[1:]]


def test_newest_first_and_columns(env, capsys):
    _, base = env
    assert cli.main(base) == 0
    rows = _rows(capsys)
    assert [r[2] for r in rows] == ["1", "2", "1"]
    assert rows[0][:5] == [(date.today() - timedelta(days=1)).isoformat(), "op", "1", "Normal", "Spike"]
    assert rows[0][5:] == ["7d", "2.00", "3.00", "+50.0", "no"] and rows[2][-1] == "yes"


def test_filters(env, capsys):
    _, base = env
    assert cli.main(base + ["--game", "rb"]) == 0 and [r[2] for r in _rows(capsys)] == ["2"]
    assert cli.main(base + ["--days", "7"]) == 0 and len(_rows(capsys)) == 2
    assert cli.main(base + ["--discord"]) == 0 and len(_rows(capsys)) == 1
    assert cli.main(base + ["--no-discord"]) == 0 and len(_rows(capsys)) == 2
    assert cli.main(base + ["--limit", "1"]) == 0 and len(_rows(capsys)) == 1


def test_empty_results(env, capsys):
    _, base = env
    assert cli.main(base + ["--game", "nope"]) == 0
    assert capsys.readouterr().out.strip() == "no alerts"


@pytest.mark.parametrize("flag,val", [("--days", "0"), ("--days", "3651"), ("--limit", "0"),
                                       ("--limit", "501"), ("--limit", "x"), ("--game", "Bad Key")])
def test_invalid_args_exit_2(env, capsys, flag, val):
    _, base = env
    with pytest.raises(SystemExit) as e:
        cli.main(base + [flag, val])
    assert e.value.code == 2


def test_range_edges_accepted(env):
    _, base = env
    assert cli.main(base + ["--days", "3650", "--limit", "500"]) == 0


def test_missing_db_creates_nothing(tmp_path, capsys):
    db = tmp_path / "none.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    assert cli.main(["--config", str(cfg), "alerts"]) == 0
    assert capsys.readouterr().out.strip() == "no database yet"
    assert list(tmp_path.glob("none.db*")) == []


def test_unreadable_db(env, capsys):
    db, base = env
    db.write_bytes(b"this is not a sqlite database" * 100)
    assert cli.main(base) == 1
    cap = capsys.readouterr()
    assert "cannot read alerts" in cap.err and "no alerts" not in cap.out


def test_missing_table_is_an_error(tmp_path, capsys):
    db = tmp_path / "bare.db"
    sqlite3.connect(db).close()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    assert cli.main(["--config", str(cfg), "alerts"]) == 1
    assert "cannot read alerts" in capsys.readouterr().err


def test_hostile_stored_text_is_escaped(tmp_path, capsys):
    from tcgwatch.textsafe import term_safe
    db = tmp_path / "h.db"
    card = Card(1, "op", 1, "S", "Bad\x1b[2J\x1b]52;c;YXR0YWNr\x07\rFORGED\nROW\x7f\x9b", "1", "R")
    with Store(db) as s:
        s.upsert_cards([card])
        s.record_alerts([Mover(card, "Normal\tFAKE ", "market", "a", "b", 2.0, 3.0, 50.0)], date.today(), 7, discord=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    assert cli.main(["--config", str(cfg), "alerts"]) == 0
    out = capsys.readouterr().out
    assert not any(ord(c) < 32 and c not in "\t\n" or 0x7f <= ord(c) <= 0x9f or c in "  " for c in out)
    lines = out.split("\n")[:-1]
    assert len(lines) == 2 and len(lines[1].split("\t")) == 10
    assert "\\x1b[2J" in out and "\\x07" in out and "\\x0d" in out and "\\x0a" in out and "\\x09" in lines[1]
    assert term_safe("plain é") == "plain é"


def test_term_safe_is_injective_for_escape_lookalikes():
    from tcgwatch.textsafe import term_safe
    pairs = [(chr(27), r"\x1b"), ("\n", r"\x0a"), ("\u2028", r"\u2028"), ("\\", "\\\\")]
    for real, literal in pairs:
        assert term_safe(real) != term_safe(literal)
    assert term_safe(chr(27)) == "\\x1b" and term_safe(r"\x1b") == "\\\\x1b"
    mixed = [chr(27) + r"\x1b", r"\x1b" + chr(27), "\\" + chr(27), chr(27) + "\\"]
    outs = [term_safe(m) for m in mixed]
    assert len(set(outs)) == len(outs)
    assert term_safe("a\\b") == "a\\\\b"

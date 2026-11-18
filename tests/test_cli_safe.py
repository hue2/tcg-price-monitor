"""Every CLI path that prints stored/upstream text escapes control characters (CLI-SAFE-2)."""
from datetime import date, timedelta
from pathlib import Path

import pytest

from tcgwatch import cli
from tcgwatch.models import Card, Mover, PricePoint
from tcgwatch.notify import print_console, print_watchlist
from tcgwatch.models import Unpaired
from tcgwatch.store import Store

EVIL = "Evil\x1b[2J\x1b]52;c;Zm9v\x07\r\n\tNAME"
EVIL_SET = "Set\x1b[31m \x9bX"
CONFIG = "database: {db}\ngames:\n  op:\n    name: One Piece\n    category_id: 68\n"


def _clean(out: str) -> None:
    assert not any(ch in out for ch in "\x1b\x07\r\x9b "), repr(out)
    assert "\\x1b" in out


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "t.db"
    card = Card(1, "op", 100, EVIL_SET, EVIL, "A-1", "C")
    with Store(db) as s:
        s.upsert_cards([card])
        s.upsert_prices(date.today(), [PricePoint(1, "Normal", 4.0, None, None, None)])
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    return card, ["--config", str(cfg)]


def test_sets_listing(env, capsys):
    _, base = env
    assert cli.main(base + ["sets"]) == 0
    out = capsys.readouterr().out
    _clean(out)


def test_watchlist_list(env, capsys):
    _, base = env
    assert cli.main(base + ["watchlist", "add", "1", "Normal"]) == 0
    capsys.readouterr()
    assert cli.main(base + ["watchlist", "list"]) == 0
    out = capsys.readouterr().out
    _clean(out)
    assert out.count("\n") == 1  # hostile CR/LF/TAB cannot forge rows
    assert len(out.rstrip("\n").split("\t")) == 6


def test_print_console_and_watchlist(env, capsys):
    card, _ = env
    from tcgwatch.config import AlertRule, GameConfig
    game = GameConfig(key="op", name="Game\x1b[H", category_id=68, alert=AlertRule())
    m = Mover(card=card, printing="Foil\x1b", price_field="market", baseline_date="a", current_date="b",
              baseline_price=2.0, current_price=3.0, pct_change=50.0)
    u = Unpaired(card=card, printing="Foil\x1b", current_price=1.0, price_field="market", current_date="b")
    print_console(game, AlertRule(), [m])
    print_watchlist(game, AlertRule(), [m], [u])
    out = capsys.readouterr().out
    _clean(out)
    assert "Game\\x1b[H" in out and "Foil\\x1b" in out


# --- CLI-SAFE-2 fix: config game keys, paths and error messages ---------------------------------

EVIL_KEY = "k\x1b[2J\x1b]52;c;Zm9v\x07\n\tX"


def _key_env(tmp_path, key=EVIL_KEY):
    import json
    db = tmp_path / "k.db"
    cfg = tmp_path / "c.json"  # JSON is valid YAML; avoids quoting a hostile key by hand
    cfg.write_text(json.dumps({"database": db.as_posix(), "retention_days": 30,
                               "games": {key: {"name": "G", "category_id": 1}}}), encoding="utf-8")
    old = date.today() - timedelta(days=400)
    with Store(db) as s:
        s.upsert_cards([Card(1, key, 100, "S", "N", "A-1", "C")])
        s.upsert_prices(old, [PricePoint(1, "Normal", 4.0, None, None, None)])
        s.upsert_prices(date.today(), [PricePoint(1, "Normal", 5.0, None, None, None)])
    return db, ["--config", str(cfg)]


def _no_raw(text: str) -> None:
    assert not any(ch in text for ch in "\x1b\x07\r\x9b"), repr(text)


def test_sets_hostile_game_key(tmp_path, capsys):
    _, base = _key_env(tmp_path)
    assert cli.main(base + ["sets"]) == 0
    out = capsys.readouterr().out
    _no_raw(out)
    assert "k\\x1b[2J" in out
    assert not any(l.startswith("]52") or l.startswith("\tX") for l in out.splitlines())


@pytest.mark.parametrize("dry", [True, False])
def test_prune_hostile_game_key(tmp_path, capsys, dry):
    _, base = _key_env(tmp_path)
    assert cli.main(base + ["prune"] + (["--dry-run"] if dry else [])) == 0
    out = capsys.readouterr().out
    _no_raw(out)
    assert "k\\x1b[2J" in out
    assert not any(l.startswith("]52") or l.startswith("\tX") for l in out.splitlines())


def test_backup_hostile_path(tmp_path, capsys):
    db, base = _key_env(tmp_path)
    dest = tmp_path / "b\x1b]52;c;Zm9v\x07\nx.db"
    assert cli.main(base + ["backup", str(dest)]) == 0
    cap = capsys.readouterr()
    _no_raw(cap.out + cap.err)
    assert "\\x1b" in cap.out and "\n" == cap.out[-1] and cap.out.count("\n") == 1


def test_health_hostile_db_path(tmp_path, capsys):
    import json
    cfg = tmp_path / "c.json"
    db = tmp_path / "m\x1b]52;c;Zm9v\x07\nx.db"
    cfg.write_text(json.dumps({"database": db.as_posix(), "games": {"a": {"name": "A", "category_id": 1}}}),
                   encoding="utf-8")
    assert cli.main(["--config", str(cfg), "health"]) == 1
    cap = capsys.readouterr()
    _no_raw(cap.out + cap.err)
    assert "\\x1b" in cap.err and cap.err.count("\n") == 1


def test_missing_config_path_error(tmp_path, capsys):
    rc = cli.main(["--config", str(tmp_path / "no\x1b]52;c;Zm9v\x07\nconf.yaml"), "games"])
    assert rc == 2
    err = capsys.readouterr().err
    _no_raw(err)
    assert "\\x1b" in err and err.count("\n") == 1


def test_interval_loop_hostile_lock_holder(tmp_path, capsys, monkeypatch):
    import json
    from tcgwatch.joblock import JobLockedError, holder_path, read_holder
    db, base = _key_env(tmp_path)
    cfgfile = base[1]
    conf = json.loads(Path(cfgfile).read_text(encoding="utf-8"))
    conf["schedule"] = {"check_every": "1h"}
    Path(cfgfile).write_text(json.dumps(conf), encoding="utf-8")
    evil = "k\x1b]52;c;Zm9v\x07\r\n\tX"
    class Stop(Exception):
        pass
    def stop(_):
        raise Stop
    monkeypatch.setattr(cli.time, "sleep", stop)
    # same-process locks don't contend (fcntl), so raise the real error built from the tampered sidecar
    holder_path(db).write_text(json.dumps({"kind": evil, "pid": evil, "started_at": evil}), encoding="utf-8")
    def locked(cfg, args):
        raise JobLockedError("run", read_holder(db))
    monkeypatch.setattr(cli, "cmd_run", locked)
    with pytest.raises(Stop):
        cli.main(base + ["schedule"])
    cap = capsys.readouterr()
    _no_raw(cap.out)
    assert "skipping tick" in cap.out and "\\x1b" in cap.out

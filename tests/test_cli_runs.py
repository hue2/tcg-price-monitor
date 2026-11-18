import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tcgwatch import cli
from tcgwatch.store import Store
from tcgwatch.web.app import create_app

CONFIG = "database: {db}\ngames:\n  op:\n    name: One Piece\n    category_id: 68\n"


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "t.db"
    with Store(db) as s:
        s.job_record("snapshot", True, "ok one", game="op")
        s.job_record("check", False, "boom", game="op")
        s.job_record("snapshot", True, "ok two")
        rid = s.job_start("backfill", game="op")  # open row: ok/finished NULL
        assert rid
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    return db, cfg, ["--config", str(cfg), "runs"]


def _rows(capsys):
    return [ln.split("\t") for ln in capsys.readouterr().out.splitlines()[1:]]


def test_table_newest_first(env, capsys):
    _, _, base = env
    assert cli.main(base) == 0
    rows = _rows(capsys)
    assert [r[1] for r in rows] == ["backfill", "snapshot", "check", "snapshot"]
    assert rows[0][5] == "?" and rows[1][5] == "yes" and rows[2][5] == "no"


def test_kind_and_limit(env, capsys):
    _, _, base = env
    assert cli.main(base + ["--kind", "snapshot"]) == 0 and [r[7] for r in _rows(capsys)] == ["ok two", "ok one"]
    assert cli.main(base + ["--limit", "1"]) == 0 and len(_rows(capsys)) == 1
    assert cli.main(base + ["--kind", "nope"]) == 0 and capsys.readouterr().out.strip() == "no job runs yet"


@pytest.mark.parametrize("args", [["--limit", "0"], ["--limit", "501"], ["--limit", "x"], ["--kind", "Bad Kind"],
                                  ["--kind", "1x"], ["--kind", "a" * 33]])
def test_invalid_args_exit_2(env, args):
    _, _, base = env
    with pytest.raises(SystemExit) as e:
        cli.main(base + args)
    assert e.value.code == 2


def test_limit_edges_accepted(env):
    _, _, base = env
    assert cli.main(base + ["--limit", "500"]) == 0 and cli.main(base + ["--limit", "1"]) == 0


def test_missing_db_creates_nothing(tmp_path, capsys):
    db = tmp_path / "sub" / "none.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    assert cli.main(["--config", str(cfg), "runs"]) == 0
    assert capsys.readouterr().out.strip() == "no job runs yet"
    assert cli.main(["--config", str(cfg), "runs", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not db.exists() and not db.parent.exists()


def test_empty_initialized_db(tmp_path, capsys):
    db = tmp_path / "e.db"
    Store(db).close()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    assert cli.main(["--config", str(cfg), "runs"]) == 0
    assert capsys.readouterr().out.strip() == "no job runs yet"


def test_unreadable_db_exits_1(tmp_path, capsys):
    db = tmp_path / "bad.db"
    db.write_bytes(b"not a database" * 100)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    assert cli.main(["--config", str(cfg), "runs"]) == 1
    assert "cannot read job runs" in capsys.readouterr().err


def test_hostile_text_escaped_in_table_raw_in_json(tmp_path, capsys):
    db = tmp_path / "h.db"
    evil = "x\x1b[2J\tFAKE\nrow\r\\"
    with Store(db) as s:
        s.job_record("snapshot", False, evil, game="op")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    assert cli.main(["--config", str(cfg), "runs"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out and len(out.splitlines()) == 2
    assert "x\\x1b[2J\\x09FAKE\\x0arow\\x0d\\\\" in out
    assert cli.main(["--config", str(cfg), "runs", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["note"] == evil


def test_json_matches_api(env, capsys):
    db, cfg, base = env
    for extra, qs in [([], ""), (["--kind", "snapshot"], "?kind=snapshot"), (["--limit", "2"], "?limit=2")]:
        assert cli.main(base + ["--json"] + extra) == 0
        cli_rows = json.loads(capsys.readouterr().out)
        api = TestClient(create_app(cfg), base_url="http://localhost")
        assert cli_rows == api.get("/api/job-runs" + qs).json() and cli_rows


def test_legacy_seven_column_table(tmp_path, capsys):
    """C1-era job_runs has no `game` column; readonly must neither crash nor migrate."""
    db = tmp_path / "old.db"
    with Store(db) as s:
        s.conn.execute("DROP TABLE job_runs")
        s.conn.execute("CREATE TABLE job_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
                       "started TEXT NOT NULL, finished TEXT, ok INTEGER, dataset_date TEXT, note TEXT)")
        s.conn.execute("INSERT INTO job_runs (kind, started, finished, ok, note) VALUES "
                       "('snapshot','2026-01-01T00:00:00','2026-01-01T00:01:00',1,'old')")
        s.conn.commit()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    before = db.read_bytes()
    assert cli.main(["--config", str(cfg), "runs"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1].split("\t")[1:3] == ["snapshot", "-"] and out[1].endswith("old")
    assert cli.main(["--config", str(cfg), "runs", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    api = TestClient(create_app(cfg), base_url="http://localhost").get("/api/job-runs")
    assert api.status_code == 200 and rows == api.json() and rows and "game" not in rows[0]
    assert db.read_bytes() == before

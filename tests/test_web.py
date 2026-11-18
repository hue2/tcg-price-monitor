import sqlite3
import re
from pathlib import Path
import os
import stat
from datetime import date

import pytest
from fastapi.testclient import TestClient

from tcgwatch.models import Card, PricePoint
from tcgwatch.store import Store
from tcgwatch.config import load_config
from tcgwatch.scheduler import health_check
from tcgwatch.web.app import WEBHOOK_MASK, create_app

HOOK = "https://discord.com/api/webhooks/123/secret"
CONFIG = f"""\
database: {{db}}
alert:
  window: 7d               # lookback comment
  threshold_pct: 30
notify:
  discord:
    webhook_url: "{HOOK}"
games:
  op:
    name: One Piece
    category_id: 68
    rarities: [C, R]       # regular prints
    exclude_name_patterns:
      - '\\(Manga\\)'
    exclude_sets: ['Promotion']
"""


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "t.db"
    with Store(db) as s:
        s.upsert_cards([
            Card(1, "op", 100, "Set A", "Spike", "A-1", "C"),
            Card(2, "op", 100, "Set A", "Flat", "A-2", "R"),
            Card(3, "op", 100, "Set A", "Nami (Manga)", "A-3", "SEC"),
            Card(4, "op", 200, "Promotion Cards", "Promo", "P-1", "C"),
        ])
        s.upsert_prices(date(2026, 10, 30), [PricePoint(i, "Normal", 2.0, None, None, None) for i in (1, 2, 3, 4)])
        s.upsert_prices(date(2026, 11, 6), [PricePoint(1, "Normal", 4.0, None, None, None),
                                            PricePoint(2, "Normal", 2.1, None, None, None),
                                            PricePoint(3, "Normal", 8.0, None, None, None),
                                            PricePoint(4, "Normal", 8.0, None, None, None)])
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    return TestClient(create_app(cfg), base_url="http://localhost"), cfg


def test_settings_hide_webhook(client):
    c, _ = client
    body = c.get("/api/settings").json()
    assert body["config"]["notify"]["discord"]["webhook_url"] == WEBHOOK_MASK
    assert HOOK not in c.get("/api/settings").text


def test_save_keeps_comments_and_webhook(client):
    c, path = client
    cfg = c.get("/api/settings").json()["config"]
    cfg["alert"]["threshold_pct"] = 45
    cfg["games"]["op"]["include_sets"] = [100, "Set"]
    assert c.put("/api/settings", json={"config": cfg}).status_code == 200
    text = path.read_text(encoding="utf-8")
    assert "# lookback comment" in text and "# regular prints" in text
    assert "threshold_pct: 45" in text
    assert HOOK in text
    assert "rarities: [C, R]" in text  # untouched flow list keeps its style


def test_save_rejects_invalid_config(client):
    c, path = client
    before = path.read_text(encoding="utf-8")
    cfg = c.get("/api/settings").json()["config"]
    cfg["alert"]["direction"] = "sideways"
    r = c.put("/api/settings", json={"config": cfg})
    assert r.status_code == 400 and "direction" in r.json()["detail"]
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("bad", ["lots", [], {}, True, "nan"])
def test_save_rejects_bad_max_fallback_spread(client, bad):
    c, path = client
    before = path.read_text(encoding="utf-8")
    cfg = c.get("/api/settings").json()["config"]
    cfg["alert"]["max_fallback_spread"] = bad
    r = c.put("/api/settings", json={"config": cfg})
    assert r.status_code == 400 and "max_fallback_spread" in r.json()["detail"]
    assert path.read_text(encoding="utf-8") == before


def test_cards_full_list(client):
    c, _ = client
    body = c.get("/api/cards/op").json()
    rows = {r["name"]: r for r in body["rows"]}
    assert set(rows) == {"Spike", "Flat", "Nami (Manga)", "Promo"}  # full list, not just movers
    assert rows["Spike"]["flagged"] and rows["Spike"]["discord"] and rows["Spike"]["pct"] == 100
    assert rows["Flat"]["in_scope"] and not rows["Flat"]["flagged"]
    assert not rows["Nami (Manga)"]["in_scope"] and not rows["Promo"]["in_scope"]
    # include higher rarities: manga card comes into scope, promo set stays out
    rows = {r["name"]: r for r in c.get("/api/cards/op?all_rarities=true").json()["rows"]}
    assert rows["Nami (Manga)"]["flagged"] and not rows["Promo"]["in_scope"]
    # one-off threshold override
    rows = {r["name"]: r for r in c.get("/api/cards/op?threshold=150").json()["rows"]}
    assert not rows["Spike"]["flagged"]


def test_cards_bad_override_is_400(client):
    c, _ = client
    assert c.get("/api/cards/op?direction=sideways").status_code == 400
    assert c.get("/api/cards/nope").status_code == 404


def test_sets_and_status(client):
    c, _ = client
    sets = {s["id"]: s for s in c.get("/api/sets/op").json()}
    assert sets[100]["checked"] and not sets[200]["checked"]
    st = c.get("/api/status").json()
    assert st["discord"] and st["games"][0]["days"] == 2


def test_status_missing_db_reports_no_data_and_creates_nothing(client):
    """READ-3: /api/status opens read-only; a fresh install gets zero-day games, no file or dir."""
    c, cfg = client
    db = _db_path(cfg)
    nested = db.parent / "fresh" / "dir" / "x.db"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace(db.as_posix(), nested.as_posix()), encoding="utf-8")
    st = c.get("/api/status").json()
    assert st["games"] and all(g["days"] == 0 and g["first"] is None and g["latest"] is None for g in st["games"])
    assert st["games"][0]["key"] == "op" and st["games"][0]["enabled"]
    assert st["auth_enabled"] is False and "job" in st and st["health"]["ok"] is False
    assert not nested.exists() and not nested.parent.exists()


def test_status_unreadable_db_is_sanitized_500(client, monkeypatch):
    import sqlite3
    c, cfg = client
    for exc in (sqlite3.OperationalError("no such table: private_t /private/internal/x.db"),
                sqlite3.DatabaseError("file is not a database /private/internal/x.db")):
        def boom(self, *a, **k):
            raise exc
        monkeypatch.setattr(Store, "snapshot_dates", boom)
        r = c.get("/api/status")
        assert r.status_code == 500 and r.json() == {"detail": "cannot read status: database unreadable"}
        assert "private" not in r.text
    monkeypatch.undo()
    _db_path(cfg).write_bytes(b"")
    r = c.get("/api/status")
    assert r.status_code == 500 and r.json() == {"detail": "cannot read status: database unreadable"}


def test_index_served(client):
    c, _ = client
    assert "TCG Price Watch" in c.get("/").text
    assert c.get("/static/app.js").status_code == 200


def test_save_keeps_list_indentation(client):
    test_client, cfg = client
    before = cfg.read_text(encoding="utf-8")
    body = test_client.get("/api/settings").json()
    body["config"]["alert"]["threshold_pct"] = 35
    assert test_client.put("/api/settings", json={"config": body["config"]}).status_code == 200
    after = cfg.read_text(encoding="utf-8")
    assert "      - '\\(Manga\\)'" in before
    assert "      - '\\(Manga\\)'" in after


@pytest.mark.parametrize("dash_col", [4, 6, 8])
def test_save_preserves_list_offset(client, dash_col):
    test_client, cfg = client
    text = cfg.read_text(encoding="utf-8")
    text = text.replace("      - '", " " * dash_col + "- '")
    cfg.write_text(text, encoding="utf-8")
    body = test_client.get("/api/settings").json()
    body["config"]["alert"]["threshold_pct"] = 35
    assert test_client.put("/api/settings", json={"config": body["config"]}).status_code == 200
    after = cfg.read_text(encoding="utf-8")
    assert " " * dash_col + "- '\\(Manga\\)'" in after


ANCHORED = """\
database: {db}
alert:
    window: 7d
    threshold_pct: 30
games:
    op:
        name: One Piece
        category_id: 68
        rarities: [C, R]
        exclude_name_patterns: &patterns
            - '\\(Manga\\)'
        exclude_sets: ['Promotion']
"""
BARE_DASH = """\
database: {db}
alert:
  window: 7d
  threshold_pct: 30
games:
  op:
    name: One Piece
    category_id: 68
    rarities: [C, R]
    exclude_name_patterns:
      -
      - '\\(Manga\\)'
"""
DECOY = """\
database: {db}
user_agent: |
  notes:
  - fake
alert:
  window: 7d
  threshold_pct: 30
games:
  op:
    name: One Piece
    category_id: 68
    rarities: [C, R]
    exclude_name_patterns:
        - '\\(Manga\\)'
"""
OFFSET0 = """\
database: {db}
alert:
  window: 7d
  threshold_pct: 30
games:
  op:
    name: One Piece
    category_id: 68
    rarities: [C, R]
    exclude_name_patterns:
    - '\\(Manga\\)'
"""


@pytest.mark.parametrize("template, expect", [
    (ANCHORED, ["            - '\\(Manga\\)'", "\n    window: 7d", "\n        name: One Piece"]),
    (BARE_DASH, ["\n      -", "\n      - '\\(Manga\\)'", "\n  window: 7d"]),
    (DECOY, ["        - '\\(Manga\\)'", "\n  window: 7d"]),
    (OFFSET0, ["\n    - '\\(Manga\\)'", "\n  window: 7d"]),
], ids=["anchored-offset4", "bare-dash", "block-scalar-decoy", "offset0"])
def test_save_round_trip_styles(client, template, expect):
    test_client, cfg = client
    db = cfg.read_text(encoding="utf-8").splitlines()[0].split(": ", 1)[1]
    cfg.write_text(template.format(db=db), encoding="utf-8")
    body = test_client.get("/api/settings").json()
    body["config"]["alert"]["threshold_pct"] = 35
    assert test_client.put("/api/settings", json={"config": body["config"]}).status_code == 200
    after = cfg.read_text(encoding="utf-8")
    for piece in expect:
        assert piece in after, (piece, after)


def test_history_and_watchlist(client):
    c, _ = client
    h = c.get("/api/history/1?printing=Normal").json()
    assert [p["date"] for p in h["points"]] == ["2026-10-30", "2026-11-06"]
    assert h["points"][1]["market"] == 4.0
    assert c.get("/api/history/1?printing=Normal&days=3").json()["points"][0]["date"] == "2026-11-06"
    assert c.get("/api/history/999?printing=Normal").status_code == 404

    assert c.get("/api/watchlist/op").json() == []
    assert c.put("/api/watchlist", json={"product_id": 1, "printing": "Normal"}).status_code == 200
    assert c.put("/api/watchlist", json={"product_id": 1, "printing": "Normal"}).status_code == 200  # idempotent
    assert c.put("/api/watchlist", json={"product_id": 999, "printing": "Normal"}).status_code == 404
    r = c.put("/api/watchlist", json={"product_id": 999, "printing": "Normal"})
    assert r.json()["detail"] == "no prices for this printing"
    assert c.put("/api/watchlist", json={"product_id": "1"}).status_code == 400
    assert c.put("/api/watchlist", json={"product_id": 1, "printing": " "}).status_code == 400
    assert c.delete("/api/watchlist/1?printing=").status_code == 400
    assert c.delete("/api/watchlist/1?printing=%20").status_code == 400
    wl = c.get("/api/watchlist/op").json()
    assert len(wl) == 1 and wl[0]["name"] == "Spike" and wl[0]["pct"] == 100 and wl[0]["cur"] == 4.0
    rows = {r["name"]: r for r in c.get("/api/cards/op").json()["rows"]}
    assert rows["Spike"]["watched"] and not rows["Flat"]["watched"]

    assert c.delete("/api/watchlist/1?printing=Normal").status_code == 200
    assert c.get("/api/watchlist/op").json() == []
    assert c.get("/api/watchlist/nope").status_code == 404


def test_watchlist_rejects_bad_ids_and_printings(client):
    c, _ = client
    huge = 2**100
    for bad in (True, 0, -1, huge):
        assert c.put("/api/watchlist", json={"product_id": bad, "printing": "Normal"}).status_code == 400
    for bad in ("", "  ", "Foil"):  # blank / no stored prices for that printing
        assert c.put("/api/watchlist", json={"product_id": 1, "printing": bad}).status_code in (400, 404)
    assert c.get("/api/watchlist/op").json() == []
    for url in (f"/api/watchlist/{huge}?printing=Normal", "/api/watchlist/0?printing=Normal"):
        assert c.delete(url).status_code == 422
    assert c.get(f"/api/history/{huge}?printing=Normal").status_code == 422
    assert c.get("/api/history/0?printing=Normal").status_code == 422


def test_watchlist_uses_price_field_fallback_like_cards(tmp_path):
    db = tmp_path / "t.db"
    with Store(db) as s:
        s.upsert_cards([Card(1, "op", 100, "Set A", "Sparse", "A-1", "C")])
        s.upsert_prices(date(2026, 10, 30), [PricePoint(1, "Normal", 2.0, None, 3.0, None)])
        s.upsert_prices(date(2026, 11, 6), [PricePoint(1, "Normal", None, None, 6.0, None)])
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()).replace("alert:\n", "alert:\n  price_fields: [market, mid]\n", 1),
                   encoding="utf-8")
    c = TestClient(create_app(cfg), base_url="http://localhost")
    assert c.put("/api/watchlist", json={"product_id": 1, "printing": "Normal"}).status_code == 200
    row = c.get("/api/watchlist/op").json()[0]
    card = c.get("/api/cards/op").json()["rows"][0]
    assert (row["field"], row["cur"], row["pct"]) == ("mid", 6.0, 100.0)
    assert (card["field"], card["cur"], card["pct"]) == ("mid", 6.0, 100.0)


def test_expanded_chart_uses_row_field():
    js = Path("src/tcgwatch/web/static/app.js").read_text(encoding="utf-8")
    assert 'data-field="${esc(r.field)}"' in js
    assert 'sparkline(h.points, trend.dataset.field || "market"' in js
def test_forged_host_rejected(client):
    c, _ = client
    assert c.get("/api/status", headers={"Host": "evil.example"}).status_code == 400
    assert c.get("/api/status", headers={"Host": "localhost:8765"}).status_code == 200


def test_cross_origin_mutation_rejected(client):
    c, _ = client
    for hdr in ({"Origin": "http://evil.example"}, {"Referer": "http://evil.example/x"}, {"Origin": "null"}):
        r = c.post("/api/jobs/run", json={}, headers=hdr)
        assert r.status_code == 403, hdr
    assert c.put("/api/settings", json={}, headers={"Origin": "http://evil.example"}).status_code == 403


def test_same_origin_mutation_allowed(client):
    c, _ = client
    r = c.put("/api/settings", json={}, headers={"Origin": "http://localhost"})
    assert r.status_code != 403
    r = c.put("/api/settings", json={})  # no Origin/Referer: non-browser client
    assert r.status_code != 403


def test_allowed_hosts_configurable(tmp_path, monkeypatch):
    from tcgwatch.web.app import allowed_hosts
    monkeypatch.setenv("TCGWATCH_ALLOWED_HOSTS", "tcg.lan, box")
    hosts = allowed_hosts("192.168.1.5", ["extra"])
    assert {"localhost", "127.0.0.1", "192.168.1.5", "tcg.lan", "box", "extra"} <= set(hosts)
    assert "0.0.0.0" not in allowed_hosts("0.0.0.0")


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "localhost:8765", "127.0.0.1:8765", "[::1]", "[::1]:8765"])
def test_host_variants_accepted(client, host):
    c, _ = client
    assert c.get("/api/status", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["localhost.evil.com", "localhost@evil.com", "[", "", "evil.example:8765", "[::2]"])
def test_host_variants_rejected(client, host):
    c, _ = client
    assert c.get("/api/status", headers={"Host": host}).status_code == 400


@pytest.mark.parametrize("host,origin", [
    ("localhost", "http://localhost"),
    ("LOCALHOST", "http://localhost"),
    ("localhost:80", "http://localhost"),
    ("localhost", "http://LocalHost:80"),
    ("[::1]:8765", "http://[::1]:8765"),
])
def test_same_origin_normalized(client, host, origin):
    c, _ = client
    r = c.post("/api/jobs/invalid", headers={"Host": host, "Origin": origin})
    assert r.status_code != 403, (host, origin)


@pytest.mark.parametrize("origin", [
    "https://localhost",        # scheme mismatch
    "//localhost",              # scheme-relative
    "http://localhost:8765",    # port mismatch
    "http://[",                 # malformed -> 403, not 500
    "http://localhost:abc",
    "ftp://localhost",
    "null",
])
def test_origin_rejected(client, origin):
    c, _ = client
    for hdr in ({"Origin": origin}, {"Referer": origin}):
        r = c.post("/api/jobs/invalid", headers=hdr)
        assert r.status_code == 403, hdr


def test_is_loopback_parses_addresses():
    from tcgwatch.web.app import is_loopback
    for h in ("127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost", "LOCALHOST"):
        assert is_loopback(h), h
    for h in ("0.0.0.0", "::", "127.evil.example", "localhost.evil.com", "192.168.1.5", "example.com"):
        assert not is_loopback(h), h


def test_cmd_web_warns_for_127_dns_name(monkeypatch, capsys, tmp_path):
    import argparse
    import sys
    import types
    from tcgwatch import cli
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **k: None))
    monkeypatch.setattr("tcgwatch.web.app.create_app", lambda *a, **k: None)
    ns = argparse.Namespace(host="127.evil.example", port=8765, config=tmp_path / "c.yaml", allowed_host=[])
    from tcgwatch.config import parse_config
    monkeypatch.delenv("TCGWATCH_WEB_TOKEN", raising=False)
    ns.insecure_no_auth = False
    assert cli.cmd_web(parse_config({}), ns) == 2  # a 127.* DNS name is not loopback
    assert "refusing" in capsys.readouterr().err
    ns.host = "127.0.0.1"
    cli.cmd_web(parse_config({}), ns)
    assert "refusing" not in capsys.readouterr().err

@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_save_preserves_file_mode(client):
    c, cfg = client
    cfg.chmod(0o640)
    body = c.get("/api/settings").json()["config"]
    body["alert"]["threshold_pct"] = 35
    assert c.put("/api/settings", json={"config": body}).status_code == 200
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o640
    assert not cfg.with_suffix(".yaml.tmp").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_save_new_file_defaults_to_0600(tmp_path):
    from tcgwatch.web.app import ConfigFile
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=(tmp_path / "t.db").as_posix()), encoding="utf-8")
    cf = ConfigFile(cfg)
    new = cf.plain()
    cfg.unlink()
    old_umask = os.umask(0o022)
    try:
        cf.doc = lambda: __import__("ruamel.yaml").yaml.comments.CommentedMap()
        cf.save(new)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="symlinks/POSIX modes")
def test_save_ignores_planted_symlink_at_old_tmp_name(client):
    c, cfg = client
    victim = cfg.parent / "victim.txt"
    victim.write_text("precious")
    victim.chmod(0o666)
    cfg.with_suffix(".yaml.tmp").symlink_to(victim)
    body = c.get("/api/settings").json()["config"]
    body["alert"]["threshold_pct"] = 36
    assert c.put("/api/settings", json={"config": body}).status_code == 200
    assert victim.read_text() == "precious"
    assert not cfg.is_symlink()
    assert list(cfg.parent.glob("*.tmp")) == [cfg.with_suffix(".yaml.tmp")]  # symlink untouched


def test_save_works_without_fchmod(client, monkeypatch):
    c, cfg = client
    monkeypatch.delattr(os, "fchmod", raising=False)
    body = c.get("/api/settings").json()["config"]
    body["alert"]["threshold_pct"] = 37
    assert c.put("/api/settings", json={"config": body}).status_code == 200
    assert "37" in cfg.read_text(encoding="utf-8")
    assert not [p for p in cfg.parent.glob("*.tmp") if not p.is_symlink()]




def test_job_runs_as_subprocess(client, monkeypatch):
    import time
    from tcgwatch.web import app as webapp

    c, cfg = client
    seen = {}
    real = webapp.subprocess.Popen

    def spy(argv, **kw):
        seen["argv"] = argv
        return real([argv[0], "-c", "print('hello from job')"], **kw)

    monkeypatch.setattr(webapp.subprocess, "Popen", spy)
    assert c.post("/api/jobs/snapshot").status_code == 200
    for _ in range(100):
        job = c.get("/api/jobs").json()
        if not job["running"]:
            break
        time.sleep(0.05)
    assert job["ok"] is True and "hello from job" in job["log"]
    assert seen["argv"][1:4] == ["-u", "-m", "tcgwatch"] and seen["argv"][-1] == "snapshot"


def test_job_409_reports_lock_holder(client):
    import subprocess
    import sys

    c, cfg = client
    db = next(l for l in cfg.read_text().splitlines() if l.startswith("database:")).split(": ", 1)[1]
    code = ("import sys,time;from tcgwatch.joblock import job_lock\n"
            "with job_lock(sys.argv[1],'watch'):\n print('go',flush=True);time.sleep(5)")
    p = subprocess.Popen([sys.executable, "-c", code, db], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "go"
        r = c.post("/api/jobs/snapshot")
    finally:
        p.kill()
        p.wait()
        p.stdout.close()
    assert r.status_code == 409
    assert "watch is already running" in r.json()["detail"] and "started" in r.json()["detail"]


def test_job_409_while_web_job_running(client, monkeypatch):
    import sys
    from tcgwatch.web import app as webapp

    c, _ = client
    real = webapp.subprocess.Popen
    monkeypatch.setattr(webapp.subprocess, "Popen",
                        lambda argv, **kw: real([sys.executable, "-c", "import time;time.sleep(1.5)"], **kw))
    assert c.post("/api/jobs/snapshot").status_code == 200
    r = c.post("/api/jobs/backfill")
    assert r.status_code == 409 and "snapshot is already running" in r.json()["detail"]


def _fake_child(monkeypatch, code):
    import sys
    from tcgwatch.web import app as webapp

    real = webapp.subprocess.Popen
    spawned = []

    def spy(argv, **kw):
        # keep the real "-u" flag so the unbuffered launch is what is under test
        p = real([argv[0], "-u", "-c", code], **kw)
        spawned.append(p)
        return p

    monkeypatch.setattr(webapp.subprocess, "Popen", spy)
    return spawned


def test_job_output_streams_before_exit(client, monkeypatch):
    import time

    c, _ = client
    spawned = _fake_child(monkeypatch, "import time;print('progress line');time.sleep(30)")
    try:
        assert c.post("/api/jobs/snapshot").status_code == 200
        for _ in range(100):
            if "progress line" in c.get("/api/jobs").json()["log"]:
                break
            time.sleep(0.05)
        job = c.get("/api/jobs").json()
        assert job["running"] and "progress line" in job["log"]
    finally:
        for p in spawned:
            p.kill()
            p.wait()


def test_shutdown_reaps_running_child(tmp_path, monkeypatch):
    import time
    from fastapi.testclient import TestClient

    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=(tmp_path / "t.db").as_posix()), encoding="utf-8")
    spawned = _fake_child(monkeypatch, "import time;print('up',flush=True);time.sleep(60)")
    try:
        with TestClient(create_app(cfg), base_url="http://localhost") as c:
            assert c.post("/api/jobs/snapshot").status_code == 200
            for _ in range(100):
                if "up" in c.get("/api/jobs").json()["log"]:
                    break
                time.sleep(0.05)
            assert spawned and spawned[0].poll() is None
        assert spawned[0].poll() is not None  # reaped by lifespan shutdown
    finally:
        for p in spawned:
            if p.poll() is None:
                p.kill()
                p.wait()


def test_shutdown_kills_child_ignoring_sigterm(monkeypatch):
    import signal
    import time
    from tcgwatch.web.app import JobRunner

    if not hasattr(signal, "SIGKILL"):
        pytest.skip("POSIX only")
    spawned = _fake_child(monkeypatch, "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                                       "print('up',flush=True);time.sleep(60)")
    from pathlib import Path
    jr = JobRunner(Path("c.yaml"))
    try:
        jr.start("snapshot")
        for _ in range(100):
            if "up" in jr.current()["log"]:
                break
            time.sleep(0.05)
        jr.shutdown(grace=0.5)
        assert spawned[0].poll() is not None
    finally:
        for p in spawned:
            if p.poll() is None:
                p.kill()
                p.wait()



def test_watchlist_delete_is_idempotent_and_printing_specific(client):
    c, _ = client
    assert c.put("/api/watchlist", json={"product_id": 1, "printing": "Normal"}).status_code == 200
    assert c.delete("/api/watchlist/1?printing=Foil").status_code == 200  # not watched: no-op
    assert c.delete("/api/watchlist/999?printing=Normal").status_code == 200  # unknown card: no-op
    rows = {r["name"]: r for r in c.get("/api/cards/op").json()["rows"]}
    assert rows["Spike"]["watched"]
    assert c.delete("/api/watchlist/1?printing=Normal").status_code == 200
    assert c.delete("/api/watchlist/1?printing=Normal").status_code == 200  # second delete: no-op
    assert c.get("/api/watchlist/op").json() == []
    rows = {r["name"]: r for r in c.get("/api/cards/op").json()["rows"]}
    assert not rows["Spike"]["watched"]


def test_watchlist_single_sample_has_price_but_no_change(client):
    c, _ = client
    c.put("/api/watchlist", json={"product_id": 1, "printing": "Normal"})
    row = c.get("/api/watchlist/op?days=1").json()[0]
    assert len(row["points"]) == 1
    assert (row["cur"], row["pct"]) == (4.0, None)


MERGE_ONLY = """\
database: {db}
alert: &base_alert
  threshold_pct: 30
games:
  op:
    name: One Piece
    category_id: 68
    alert:
      <<: *base_alert
"""
MERGE_PLUS = MERGE_ONLY.replace("      <<: *base_alert\n", "      window: 7d\n      <<: *base_alert\n")


@pytest.mark.parametrize("template", [MERGE_ONLY, MERGE_PLUS])
def test_settings_with_yaml_merge_keys(client, template):
    test_client, cfg = client
    db = cfg.parent / "t.db"
    cfg.write_text(template.format(db=db.as_posix()), encoding="utf-8")
    r = test_client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    body["config"]["alert"]["threshold_pct"] = 35
    assert test_client.put("/api/settings", json={"config": body["config"]}).status_code == 200
    assert test_client.get("/api/settings").status_code == 200


def test_job_runs_endpoint(client):
    c, cfg = client
    assert c.get("/api/job-runs").json() == []  # empty DB
    db = Path(re.search(r"database:\s*(.+)", cfg.read_text()).group(1).strip().strip("'\""))
    with Store(db) as s:
        s.job_record("snapshot", True, "a")
        s.job_record("schedule", False, "b")
        s.job_record("snapshot", False, "c")
    rows = c.get("/api/job-runs").json()
    assert [r["note"] for r in rows] == ["c", "b", "a"]  # newest first
    assert [r["note"] for r in c.get("/api/job-runs?kind=snapshot&limit=1").json()] == ["c"]
    for bad in ("limit=0", "limit=501", "limit=x", "kind=Bad%20kind", "kind="):
        assert c.get(f"/api/job-runs?{bad}").status_code == 422, bad


def test_job_runs_readonly_db_and_missing_db(client):
    c, cfg = client
    db = Path(re.search(r"database:\s*(.+)", cfg.read_text()).group(1).strip().strip("'\""))
    with Store(db) as s:
        s.job_record("snapshot", True, "a")
    db.chmod(0o444)
    try:
        assert [r["note"] for r in c.get("/api/job-runs").json()] == ["a"]
    finally:
        db.chmod(0o644)
    for f in db.parent.glob(db.name + "*"):
        f.unlink()
    assert c.get("/api/job-runs").json() == [] and not db.exists()  # not created


def test_status_health_matches_cli(client):
    c, cfg = client
    assert c.get("/api/status").json()["health"]["ok"] is False  # no schedule run yet
    db = Path(re.search(r"database:\s*(.+)", cfg.read_text()).group(1).strip().strip("'\""))
    with Store(db) as s:
        s.job_record("schedule", True, "tick")
    h = c.get("/api/status").json()["health"]
    assert h["ok"] is True and "last ok schedule run" in h["detail"]


def test_store_readonly_never_creates(tmp_path):
    import sqlite3
    import pytest
    from tcgwatch.store import Store
    db = tmp_path / "sub" / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        Store(db, readonly=True)
    assert not db.exists() and not db.parent.exists()


def test_health_missing_db_not_created_even_if_exists_lies(client, monkeypatch):
    import pathlib
    c, cfg = client
    db = Path(re.search(r"database:\s*(.+)", cfg.read_text()).group(1).strip().strip("'\""))
    for f in db.parent.glob(db.name + "*"):
        f.unlink()
    real = pathlib.Path.exists  # race: a preflight exists() said yes, then the file vanished
    monkeypatch.setattr(pathlib.Path, "exists", lambda self: True if self == db else real(self))
    assert c.get("/api/job-runs").json() == []
    from tcgwatch import scheduler
    from tcgwatch.config import load_config
    assert scheduler.health_check(load_config(cfg))[0] is False
    monkeypatch.undo()
    assert not db.exists()


def test_health_badge_and_runs_table_hooks(client):
    c, _ = client
    html = c.get("/").text
    for hook in ('id="health"', 'data-tab="runs"', 'id="tab-runs"', 'id="runs"'):
        assert hook in html, hook
    js = c.get("/static/app.js").text
    assert "st.health.ok" in js and "/api/job-runs?limit=20" in js
    for f in ("r.kind", "r.game", "r.started", "r.finished", "r.dataset_date", "r.note"):
        assert f"esc({f})" in js, f  # every row field escaped
    assert "run-failed" in c.get("/static/style.css").text


def test_runs_tab_failure_and_recovery_headless():
    import shutil, subprocess, pathlib
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    root = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([node, str(root / "tests/js/runs_failure.cjs"), str(root)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


def test_alerts_tab_hooks(client):
    c, _ = client
    html = c.get("/").text
    for hook in ('data-tab="alerts"', 'id="tab-alerts"', 'id="alertFilter"', 'id="alertsError"', 'id="alerts"'):
        assert hook in html, hook
    js = c.get("/static/app.js").text
    assert "/api/alerts?" in js
    for f in ("r.alerted_on", "r.rarity", "r.printing", "r.game", "r.window_days", "name"):
        assert f"esc({f})" in js, f


def test_alerts_tab_headless():
    import shutil, subprocess, pathlib
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    root = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([node, str(root / "tests/js/alerts_tab.cjs"), str(root)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


def test_stale_requests_never_overwrite_newer_headless():
    import shutil, subprocess, pathlib
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    root = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([node, str(root / "tests/js/stale_requests.cjs"), str(root)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


def test_auth_401_banner_headless():
    import shutil, subprocess, pathlib
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    root = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([node, str(root / "tests/js/auth_401.cjs"), str(root)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


def test_watchlist_ui_headless():
    import shutil, subprocess, pathlib
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    root = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([node, str(root / "tests/js/watchlist.cjs"), str(root)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


def test_cards_csv_headless():
    import shutil, subprocess, pathlib
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    root = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([node, str(root / "tests/js/cards_csv.cjs"), str(root)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


def test_status_exposes_auth_enabled(client, tmp_path, monkeypatch):
    """UX-2: /api/status tells the UI whether auth is on (Log out button)."""
    c, _ = client
    assert c.get("/api/status").json()["auth_enabled"] is False
    ca, _ = _authed(tmp_path, monkeypatch)
    ca.cookies.set("tcgwatch_token", "s3cret")
    assert ca.get("/api/status").json()["auth_enabled"] is True


def test_job_closes_stdout_pipe(client, monkeypatch):
    """BUG-7: the job's stdout pipe is closed after the run (no fd/ResourceWarning leak)."""
    import time
    from tcgwatch.web import app as webapp

    c, cfg = client
    procs = []
    real = webapp.subprocess.Popen

    def spy(argv, **kw):
        p = real([argv[0], "-c", "print('x')"], **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(webapp.subprocess, "Popen", spy)
    assert c.post("/api/jobs/snapshot").status_code == 200
    for _ in range(100):
        if not c.get("/api/jobs").json()["running"]:
            break
        time.sleep(0.05)
    assert procs and procs[0].stdout.closed


def test_job_runs_and_health_corrupt_db_is_not_masked(client):
    c, cfg = client
    db = Path(re.search(r"database:\s*(.+)", cfg.read_text()).group(1).strip().strip("'\""))
    for f in db.parent.glob(db.name + "*"):
        f.unlink()
    db.write_bytes(b"this is not a sqlite database" * 100)
    assert c.get("/api/job-runs").status_code == 500
    ok, detail = health_check(load_config(cfg))
    assert ok is False and "unreadable" in detail


def _db_path(cfg):
    db = Path(re.search(r"database:\s*(.+)", cfg.read_text()).group(1).strip().strip("'\""))
    for f in db.parent.glob(db.name + "*"):
        f.unlink()
    return db


def test_job_runs_500_body_is_fixed_and_leak_free(client, monkeypatch):
    import sqlite3
    c, _ = client
    for exc in (sqlite3.OperationalError("no such table: private_internal_table /private/internal/storage.db"),
                sqlite3.DatabaseError("file is not a database /private/internal/storage.db")):
        def boom(self, *a, **k):
            raise exc
        monkeypatch.setattr(Store, "recent_jobs", boom)
        r = c.get("/api/job-runs")
        assert r.status_code == 500
        assert r.json() == {"detail": "cannot read job runs: database unreadable"}
        assert "private" not in r.text


def test_job_runs_zero_byte_db_is_500_but_absent_and_initialized_are_empty(client):
    c, cfg = client
    db = _db_path(cfg)
    assert c.get("/api/job-runs").json() == []          # absent
    assert not db.exists()
    db.write_bytes(b"")                                  # zero-byte / no schema
    r = c.get("/api/job-runs")
    assert r.status_code == 500 and r.json() == {"detail": "cannot read job runs: database unreadable"}
    db.unlink()
    Store(db).close()                                    # initialized, no runs
    assert c.get("/api/job-runs").json() == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_unreadable_existing_db_is_not_missing(client, capsys):
    from tcgwatch import cli
    c, cfg = client
    db = _db_path(cfg)
    Store(db).close()
    db.chmod(0)
    try:
        r = c.get("/api/job-runs")
        assert r.status_code == 500 and "unreadable" in r.json()["detail"]
        ok, detail = health_check(load_config(cfg))
        assert ok is False and "unreadable" in detail and "no database" not in detail
        assert cli.main(["--config", str(cfg), "watchlist", "list"]) == 1
        assert "watchlist is empty" not in capsys.readouterr().out
    finally:
        db.chmod(0o600)


def _seed_alerts(db):
    from datetime import datetime, timedelta, timezone
    from tcgwatch.models import Card, Mover
    cards = {1: Card(product_id=1, game_key="pokemon", group_id=1, group_name="S", name="A", number="1",
                     rarity="R", url="u", image_url=None),
             2: Card(product_id=2, game_key="mtg", group_id=2, group_name="T", name="B", number="2",
                     rarity="R", url="u", image_url=None)}
    with Store(db) as s:
        s.upsert_cards(cards.values())
        today = datetime.now(timezone.utc).date()
        for pid, age, disc in ((1, 10, False), (2, 5, True), (1, 1, True)):
            m = Mover(card=cards[pid], printing="Normal", price_field="market", baseline_date="d", current_date="e",
                      baseline_price=1.0, current_price=2.0, pct_change=100.0)
            s.record_alerts([m], today - timedelta(days=age), 7, discord=disc)


def test_alerts_endpoint(client):
    c, cfg = client
    db = _db_path(cfg)
    assert c.get("/api/alerts").json() == [] and not db.exists()
    _seed_alerts(db)
    rows = c.get("/api/alerts").json()
    assert [r["product_id"] for r in rows] == [1, 2, 1] and rows[0]["name"] == "A" and rows[0]["game"] == "pokemon"
    assert [r["product_id"] for r in c.get("/api/alerts?game=mtg").json()] == [2]
    assert len(c.get("/api/alerts?days=7").json()) == 2
    assert len(c.get("/api/alerts?discord=true").json()) == 2
    assert [r["discord"] for r in c.get("/api/alerts?discord=false").json()] == [0]
    assert len(c.get("/api/alerts?limit=1").json()) == 1
    for bad in ("limit=0", "limit=501", "days=0", "days=x", "game=Bad%20g", "discord=maybe"):
        assert c.get(f"/api/alerts?{bad}").status_code == 422, bad


def test_alerts_readonly_and_error_body(client, monkeypatch):
    import sqlite3
    c, cfg = client
    db = _db_path(cfg)
    _seed_alerts(db)
    db.chmod(0o444)
    try:
        assert len(c.get("/api/alerts").json()) == 3
    finally:
        db.chmod(0o644)
    db.write_bytes(b"")
    r = c.get("/api/alerts")
    assert r.status_code == 500 and r.json() == {"detail": "cannot read alerts: database unreadable"}
    def boom(self, *a, **k):
        raise sqlite3.DatabaseError("file is not a database /private/x.db")
    monkeypatch.setattr(Store, "recent_alerts", boom)
    r = c.get("/api/alerts")
    assert r.status_code == 500 and "private" not in r.text

# --- AUTH-0: token guardrail -------------------------------------------------

def _authed(tmp_path, monkeypatch, token="s3cret"):
    monkeypatch.delenv("TCGWATCH_WEB_TOKEN", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: t.db\ngames: {}\n", encoding="utf-8")
    return TestClient(create_app(cfg, token=token), base_url="http://localhost"), cfg


def test_token_required_bearer_and_cookie(tmp_path, monkeypatch):
    c, _ = _authed(tmp_path, monkeypatch)
    assert c.get("/api/settings").status_code == 401
    assert c.get("/api/settings", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/settings", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.post("/login", data={"token": "bad"}, follow_redirects=False).status_code == 401
    r = c.post("/login", data={"token": "s3cret"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/api/settings").status_code == 200  # cookie jar now holds the token


def test_login_post_json_headers_and_no_url_token(tmp_path, monkeypatch):
    c, _ = _authed(tmp_path, monkeypatch)
    page = c.get("/login")
    assert page.status_code == 200 and "<form" in page.text
    assert page.headers["cache-control"] == "no-store" and page.headers["referrer-policy"] == "no-referrer"
    # a token in the URL is not a credential
    r = c.get("/login?token=s3cret", follow_redirects=False)
    assert r.status_code == 200 and "set-cookie" not in r.headers
    assert c.get("/api/settings").status_code == 401
    for bad in ({"token": 5}, {"token": ""}, [1]):
        assert c.post("/login", json=bad).status_code == 401
    assert c.post("/login", content=b"{not json", headers={"content-type": "application/json"}).status_code == 401
    assert c.put("/login").status_code == 405
    r = c.post("/login", json={"token": "s3cret"}, follow_redirects=False)
    assert r.status_code == 303 and "location" in r.headers and "s3cret" not in r.headers["location"]
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]
    assert c.get("/api/settings").status_code == 200
    bad = c.post("/login", data={"token": "x"})
    assert bad.headers["cache-control"] == "no-store"


def test_login_post_same_origin_and_host(tmp_path, monkeypatch):
    c, _ = _authed(tmp_path, monkeypatch)
    ok = {"token": "s3cret"}
    assert c.post("/login", data=ok, headers={"origin": "http://evil.example"}).status_code == 403
    assert c.post("/login", data=ok, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert c.post("/login", data=ok, headers={"host": "evil.example"}).status_code == 400
    assert c.post("/login", data=ok, headers={"origin": "http://localhost"}, follow_redirects=False).status_code == 303


def test_login_token_never_in_request_urls_or_logs(tmp_path, monkeypatch, caplog):
    import logging
    c, _ = _authed(tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG)
    r = c.post("/login", data={"token": "s3cret"}, follow_redirects=False)
    assert "s3cret" not in str(r.request.url) and "s3cret" not in r.headers["location"]
    assert "s3cret" not in caplog.text


def test_web_health_sends_token_never_prints_it(monkeypatch, capsys):
    import argparse, io, urllib.error, urllib.request
    from tcgwatch import cli
    from tcgwatch.config import parse_config
    seen = []

    def fake(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization")))
        return io.BytesIO(b"{}")
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    ns = argparse.Namespace(port=8765, timeout=1.0)
    monkeypatch.delenv("TCGWATCH_WEB_TOKEN", raising=False)
    # config token
    assert cli.cmd_web_health(parse_config({"web": {"auth_token": "cfgtok"}}), ns) == 0
    # env token wins
    monkeypatch.setenv("TCGWATCH_WEB_TOKEN", "envtok")
    assert cli.cmd_web_health(parse_config({"web": {"auth_token": "cfgtok"}}), ns) == 0
    # no token (compose --insecure-no-auth case): no Authorization header
    monkeypatch.delenv("TCGWATCH_WEB_TOKEN")
    assert cli.cmd_web_health(parse_config({}), ns) == 0
    assert seen == [("http://127.0.0.1:8765/api/status", "Bearer cfgtok"),
                    ("http://127.0.0.1:8765/api/status", "Bearer envtok"),
                    ("http://127.0.0.1:8765/api/status", None)]
    out = capsys.readouterr()
    assert "tok" not in out.out + out.err
    # failures: 401 and unreachable are unhealthy
    raised = []  # keep a handle so the HTTPError can be closed (no ResourceWarning on GC)

    def deny(req, timeout):
        err = urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b""))
        raised.append(err)
        raise err
    monkeypatch.setattr(urllib.request, "urlopen", deny)
    try:
        assert cli.cmd_web_health(parse_config({"web": {"auth_token": "cfgtok"}}), ns) == 1
    finally:
        for err in raised:
            err.close()
    def down(req, timeout):
        raise ConnectionRefusedError()
    monkeypatch.setattr(urllib.request, "urlopen", down)
    assert cli.cmd_web_health(parse_config({}), ns) == 1
    out = capsys.readouterr()
    assert "401" in out.err and "unreachable" in out.err and "cfgtok" not in out.err


def test_web_health_against_real_auth_app(tmp_path, monkeypatch):
    """The exact request web-health sends authenticates against the app (and fails without the token)."""
    c, _ = _authed(tmp_path, monkeypatch, token="envtok")
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer envtok"}).status_code == 200


def test_dockerfile_healthcheck_uses_web_health():
    from pathlib import Path
    df = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    hc = df[df.index("HEALTHCHECK"):]
    assert "web-health" in hc.split("\n\n")[0] and "urllib" not in hc.split("\n\n")[0]


def test_settings_mask_and_keep_token(tmp_path, monkeypatch):
    c, cfg = _authed(tmp_path, monkeypatch)
    cfg.write_text("database: t.db\nweb:\n  auth_token: s3cret\ngames: {}\n", encoding="utf-8")
    h = {"Authorization": "Bearer s3cret"}
    body = c.get("/api/settings", headers=h).json()
    assert body["config"]["web"]["auth_token"] == "__saved__"
    assert c.put("/api/settings", json=body, headers=h).status_code == 200
    assert "auth_token: s3cret" in cfg.read_text()


def test_web_token_env_overrides_config(monkeypatch):
    from tcgwatch.config import parse_config
    cfg = parse_config({"web": {"auth_token": " a "}})
    monkeypatch.delenv("TCGWATCH_WEB_TOKEN", raising=False)
    assert cfg.web_token == "a"
    monkeypatch.setenv("TCGWATCH_WEB_TOKEN", "b")
    assert cfg.web_token == "b"
    import pytest
    from tcgwatch.config import ConfigError
    for bad in ({"web": 3}, {"web": {"auth_token": 5}}):
        with pytest.raises(ConfigError):
            parse_config(bad)


def test_cmd_web_refuses_remote_without_token(monkeypatch, capsys):
    import argparse, sys, types
    from tcgwatch import cli
    from tcgwatch.config import parse_config
    monkeypatch.delenv("TCGWATCH_WEB_TOKEN", raising=False)
    ran = {}
    fake = types.SimpleNamespace(run=lambda app, **kw: ran.update(kw))
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    monkeypatch.setattr("tcgwatch.web.app.create_app", lambda *a, **k: ran.update(token=a[2]) or object())
    ns = lambda **kw: argparse.Namespace(config=None, port=1, allowed_host=[], insecure_no_auth=False, **kw)
    cfg = parse_config({})
    assert cli.cmd_web(cfg, ns(host="0.0.0.0")) == 2 and "refusing" in capsys.readouterr().err and not ran
    assert cli.cmd_web(cfg, ns(host="127.0.0.1")) == 0
    assert cli.cmd_web(cfg, argparse.Namespace(config=None, port=1, allowed_host=[], insecure_no_auth=True, host="0.0.0.0")) == 0
    assert "WARNING" in capsys.readouterr().err
    assert cli.cmd_web(parse_config({"web": {"auth_token": "t"}}), ns(host="0.0.0.0")) == 0 and ran["token"] == "t"



@pytest.mark.parametrize("count", [301, 650])
def test_watchlist_over_300_series_matches_per_series_history(tmp_path, count):
    """TEST-8: real >300 watched series through GET /api/watchlist (no chunk-size patch). Each row's
    points must equal the per-series `Store.history` result, and the span math must hold."""
    db = tmp_path / "t.db"
    odd = list(range(1, count + 1, 2))
    with Store(db) as s:
        s.upsert_cards([Card(pid, "op", 100, "Set A", f"Card {pid}", f"A-{pid}", "C") for pid in range(1, count + 1)])
        s.upsert_prices(date(2026, 10, 30), [PricePoint(pid, "Normal", 2.0 * pid / 10, None, None, None)
                                             for pid in range(1, count + 1)])
        s.upsert_prices(date(2026, 11, 6), [PricePoint(pid, "Normal", 3.0 * pid / 10, None, None, None)
                                            for pid in range(1, count + 1)]
                        + [PricePoint(pid, "Foil", 1.0, None, None, None) for pid in odd])
        for pid in range(1, count + 1):
            assert s.watch(pid, "Normal")
        for pid in odd:
            assert s.watch(pid, "Foil")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    c = TestClient(create_app(cfg), base_url="http://localhost")

    rows = c.get("/api/watchlist/op?days=30").json()
    assert len(rows) == count + len(odd)
    assert {(r["product_id"], r["printing"]) for r in rows} == (
        {(pid, "Normal") for pid in range(1, count + 1)} | {(pid, "Foil") for pid in odd})
    with Store(db, readonly=True) as s:
        for row in rows:
            key = (row["product_id"], row["printing"])
            assert row["points"] == s.history(*key, 30), key
            if row["printing"] == "Normal":
                assert len(row["points"]) == 2
                assert row["pct"] == 50.0 and row["cur"] == 3.0 * row["product_id"] / 10
            else:
                assert len(row["points"]) == 1 and row["pct"] is None

    # A 3-day span keeps only the 11-06 snapshot, so every Normal row has one point and no change.
    short = {(r["product_id"], r["printing"]): r for r in c.get("/api/watchlist/op?days=3").json()}
    assert len(short) == count + len(odd)
    assert all(len(r["points"]) == 1 and r["pct"] is None for r in short.values())


def test_alerts_utc_day_boundary_is_frozen(client, monkeypatch):
    """`days` counts UTC calendar days from a frozen clock: an alert dated exactly `days` ago is in,
    one day older is out, and the cut moves at 00:00 UTC, not at local time."""
    from datetime import datetime, timezone

    import tcgwatch.web.app as webapp
    from tcgwatch.models import Card, Mover
    c, cfg = client
    db = _db_path(cfg)
    card = Card(product_id=1, game_key="pokemon", group_id=1, group_name="S", name="A", number="1",
                rarity="R", url="u", image_url=None)
    with Store(db) as s:
        s.upsert_cards([card])
        m = Mover(card=card, printing="Normal", price_field="market", baseline_date="d", current_date="e",
                  baseline_price=1.0, current_price=2.0, pct_change=100.0)
        for day in (date(2026, 10, 31), date(2026, 11, 1)):
            s.record_alerts([m], day, 7)

    class Frozen(datetime):
        now_at = datetime(2026, 11, 6, 23, 59, 59, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.now_at.astimezone(tz) if tz else cls.now_at.replace(tzinfo=None)

    monkeypatch.setattr(webapp, "datetime", Frozen)
    # 2026-11-06 UTC: days=6 reaches back to 2026-10-31 (included), days=5 starts at 11-01
    assert [r["alerted_on"] for r in c.get("/api/alerts?days=6").json()] == ["2026-11-01", "2026-10-31"]
    assert [r["alerted_on"] for r in c.get("/api/alerts?days=5").json()] == ["2026-11-01"]
    # one second past UTC midnight the window slides a day: 2026-11-07 minus 6 is 11-01
    Frozen.now_at = datetime(2026, 11, 7, 0, 0, 1, tzinfo=timezone.utc)
    assert [r["alerted_on"] for r in c.get("/api/alerts?days=6").json()] == ["2026-11-01"]


def test_alerts_tied_dates_order_by_rowid_desc(client):
    """Alerts sharing one alerted_on come back newest insert first (rowid DESC), stable across calls."""
    from tcgwatch.models import Card, Mover
    c, cfg = client
    db = _db_path(cfg)
    card = Card(product_id=1, game_key="pokemon", group_id=1, group_name="S", name="A", number="1",
                rarity="R", url="u", image_url=None)
    with Store(db) as s:
        s.upsert_cards([card])
        for printing in ("Normal", "Foil", "Holo"):
            m = Mover(card=card, printing=printing, price_field="market", baseline_date="d", current_date="e",
                      baseline_price=1.0, current_price=2.0, pct_change=100.0)
            s.record_alerts([m], date(2026, 11, 1), 7)
    rows = c.get("/api/alerts").json()
    assert [r["printing"] for r in rows] == ["Holo", "Foil", "Normal"]
    assert [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)
    assert c.get("/api/alerts").json() == rows


def test_alerts_orphan_row_left_join_has_null_card_metadata(client):
    """An alert whose card row is gone still lists (LEFT JOIN): card fields are null, game filter excludes it."""
    c, cfg = client
    db = _db_path(cfg)
    with Store(db) as s:
        s.conn.execute("PRAGMA foreign_keys=OFF")
        s.conn.execute("INSERT INTO alerts (product_id, printing, alerted_on, window_days, price_field, "
                       "baseline, current, pct_change, discord) VALUES (999, 'Normal', '2026-11-01', 7, 'market', "
                       "1.0, 2.0, 100.0, 0)")
        s.conn.commit()
    rows = c.get("/api/alerts").json()
    assert len(rows) == 1
    assert rows[0]["product_id"] == 999 and rows[0]["game"] is None and rows[0]["name"] is None
    assert rows[0]["number"] is None and rows[0]["url"] is None
    assert c.get("/api/alerts?game=pokemon").json() == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_alerts_real_chmod000_is_500_not_crash(client):
    """A real unreadable database file (mode 000, not a mocked error) gives a clean 500 with the stable detail."""
    c, cfg = client
    db = _db_path(cfg)
    Store(db).close()
    db.chmod(0)
    try:
        r = c.get("/api/alerts")
        assert r.status_code == 500
        assert r.json() == {"detail": "cannot read alerts: database unreadable"}
    finally:
        db.chmod(0o600)


def test_logout_drops_cookie_only(tmp_path, monkeypatch):
    c, _ = _authed(tmp_path, monkeypatch)
    c.post("/login", data={"token": "s3cret"}, follow_redirects=False)
    assert c.get("/api/settings").status_code == 200
    r = c.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert r.headers["cache-control"] == "no-store" and "tcgwatch_token" in r.headers["set-cookie"]
    assert c.get("/api/settings").status_code == 401  # cookie-only request now refused
    assert c.get("/api/settings", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    # logout needs no token and is harmless when already logged out; GET is not allowed
    assert c.post("/logout", follow_redirects=False).status_code == 303
    assert c.get("/logout").status_code == 405


def test_logout_rejects_cross_origin(tmp_path, monkeypatch):
    c, _ = _authed(tmp_path, monkeypatch)
    c.post("/login", data={"token": "s3cret"}, follow_redirects=False)
    assert c.post("/logout", headers={"origin": "http://evil.example"}).status_code == 403
    assert c.post("/logout", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert c.post("/logout", headers={"host": "evil.example"}).status_code == 400
    assert c.get("/api/settings").status_code == 200  # cookie survived the forged attempts


# --- READ-2: GET /api/sets, /api/cards, /api/history, /api/watchlist are read-only ----------------

def _cfg_db(cfg):
    """The configured DB path, without touching the files (unlike _db_path)."""
    return Path(re.search(r"database:\s*(.+)", cfg.read_text()).group(1).strip().strip("'\""))


def _point_at(cfg, db):
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")


def test_read_routes_missing_db_never_created(client):
    c, cfg = client
    db = _cfg_db(cfg).parent / "fresh" / "tcg.db"
    _point_at(cfg, db)
    assert c.get("/api/sets/op").json() == []
    r = c.get("/api/cards/op").json()
    assert r["rows"] == [] and r["current_date"] is None
    assert r["message"] == "No price data yet. Run a snapshot or backfill."
    assert c.get("/api/history/1?printing=Normal").status_code == 404
    assert c.get("/api/watchlist/op").json() == []
    assert not db.exists() and not db.parent.exists()  # no file, no parent dir


def test_read_routes_unreadable_db_is_sanitized_500(client, monkeypatch):
    c, cfg = client
    db = _cfg_db(cfg)
    db.write_bytes(b"")  # exists, but has no schema: a query fails, not the open
    for url in ("/api/sets/op", "/api/cards/op", "/api/history/1?printing=Normal", "/api/watchlist/op"):
        r = c.get(url)
        assert r.status_code == 500 and "database unreadable" in r.json()["detail"], url
        assert "no such table" not in r.text, url
    def boom(self, *a, **k):
        raise sqlite3.DatabaseError("file is not a database /private/x.db")
    monkeypatch.setattr(Store, "sets", boom)
    r = c.get("/api/sets/op")
    assert r.status_code == 500 and "private" not in r.text


def test_read_routes_writes_stay_rw_and_reads_do_not_migrate(client):
    # A readable DB: the four GETs answer, and a legacy alerts table without `discord` still loads
    # (read-only open skips migrations; last_alerts must not select the missing column).
    c, cfg = client
    db = _cfg_db(cfg)
    _seed_alerts(db)
    with Store(db) as s:
        s.conn.execute("ALTER TABLE alerts DROP COLUMN discord")
        s.conn.commit()
    assert len(c.get("/api/sets/op").json()) == 2
    rows = c.get("/api/cards/op").json()["rows"]
    assert rows and all(r["posted"] is False for r in rows)
    assert c.get("/api/history/1?printing=Normal").status_code == 200
    assert c.get("/api/watchlist/op").json() == []
    with Store(db, readonly=True) as s:
        assert not s._has_column("alerts", "discord")  # the GETs did not migrate the schema
    # the write path still migrates, as before
    assert c.put("/api/watchlist", json={"product_id": 1, "printing": "Normal"}).status_code == 200
    with Store(db, readonly=True) as s:
        assert s._has_column("alerts", "discord")


def test_discord_test_without_webhook_is_400(client, monkeypatch):
    import tcgwatch.web.app as webapp
    c, cfg = client
    cfg.write_text(cfg.read_text().replace(f'webhook_url: "{HOOK}"', 'webhook_url: ""'), encoding="utf-8")
    sent = []
    monkeypatch.setattr(webapp, "post_discord", lambda *a, **k: sent.append(a))
    r = c.post("/api/discord-test")
    assert r.status_code == 400 and r.json() == {"detail": "No Discord webhook configured"}
    assert sent == []


def test_discord_test_post_failure_is_502_and_success_is_ok(client, monkeypatch):
    import tcgwatch.web.app as webapp
    c, _ = client
    sent = []
    monkeypatch.setattr(webapp, "post_discord", lambda url, text: sent.append((url, text)))
    assert c.post("/api/discord-test").json() == {"ok": True}
    assert sent[0][0] == HOOK

    def boom(url, text):
        raise OSError("HTTP 404 from webhook")
    monkeypatch.setattr(webapp, "post_discord", boom)
    r = c.post("/api/discord-test")
    assert r.status_code == 502
    assert r.json() == {"detail": "Discord rejected the post: HTTP 404 from webhook"}


def test_cards_unreadable_db_is_logged_sanitized_500(client, monkeypatch, caplog):
    c, cfg = client
    _db_path(cfg).write_bytes(b"this is not a sqlite database" * 100)  # DatabaseError, not "missing"
    with caplog.at_level("ERROR"):
        r = c.get("/api/cards/op")
    assert r.status_code == 500 and r.json() == {"detail": "cannot read cards: database unreadable"}
    assert any("cannot read cards" in rec.getMessage() for rec in caplog.records)
    assert "not a database" not in r.text


def test_cards_missing_db_is_empty_message_not_error(client):
    c, cfg = client
    db = _db_path(cfg)  # deletes the db files
    r = c.get("/api/cards/op")
    assert r.status_code == 200 and r.json()["rows"] == []
    assert not db.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes/groups")
def test_save_fchown_not_permitted_keeps_mode_and_saves(client, monkeypatch):
    c, cfg = client
    cfg.chmod(0o640)
    expected_gid = cfg.stat().st_gid  # captured before the save
    calls = []

    def denied(fd, uid, gid):
        calls.append((uid, gid))
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(os, "fchown", denied)
    body = c.get("/api/settings").json()["config"]
    body["alert"]["threshold_pct"] = 41
    assert c.put("/api/settings", json={"config": body}).status_code == 200
    assert calls and calls[0][0] == -1 and calls[0][1] == expected_gid  # group preserved, uid untouched
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o640
    assert "41" in cfg.read_text(encoding="utf-8")


def test_save_failed_dump_unlinks_tmp_and_keeps_original(client, monkeypatch):
    from tcgwatch.web.app import ConfigFile
    c, cfg = client
    before = cfg.read_text(encoding="utf-8")
    cf = ConfigFile(cfg)
    new = cf.plain()
    new["alert"]["threshold_pct"] = 42

    def bad_dump(doc, fh):
        fh.write("partial")
        raise RuntimeError("disk full")
    monkeypatch.setattr(cf.yaml, "dump", bad_dump)
    with pytest.raises(RuntimeError, match="disk full"):
        cf.save(new)
    assert cfg.read_text(encoding="utf-8") == before
    assert list(cfg.parent.glob("*.tmp")) == []

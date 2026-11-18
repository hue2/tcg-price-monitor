"""Slow benchmarks on a seeded large DB (PERF-0). Excluded from the default run (marker `slow`).

    .venv/bin/pytest -m slow --no-cov -s

Env knobs:
  TCGWATCH_PERF_DIR     reuse/keep the seeded DB in this directory (seeding takes a minute or two)
  TCGWATCH_PERF_REPORT  write the timings and EXPLAIN QUERY PLAN output to this JSON file
Results are recorded by hand in docs/PERF.md.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from perf_seed import assert_config_targets, seed, write_config

from tcgwatch import cli
from tcgwatch.store import Store
from tcgwatch.web.app import create_app

pytestmark = pytest.mark.slow

RUNS = 3  # repeats for the fast endpoints; the check run is timed once (it is the slow one)


@pytest.fixture(scope="module")
def perf_env(tmp_path_factory):
    keep = os.environ.get("TCGWATCH_PERF_DIR")
    base = Path(keep) if keep else tmp_path_factory.mktemp("perf")
    db = base / "tcgwatch.db"
    if not db.exists():
        t0 = time.perf_counter()
        counts = seed(db)
        print(f"\nseeded {counts} in {time.perf_counter() - t0:.1f}s")
    cfg = write_config(base, db)
    # Guard: a config that targets any other file (e.g. an empty DB that `check` would create)
    # must fail here, not produce a fast, passing baseline.
    assert_config_targets(cfg, db)
    return {"db": db, "cfg": cfg, "dir": base}


def _median(fn, runs: int = RUNS):
    times, result = [], None
    for _ in range(runs):
        t0 = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times), result


def _record(report: dict, section: str, key: str, value) -> None:
    report.setdefault(section, {})[key] = value


@pytest.fixture(scope="module")
def report():
    data: dict = {}
    yield data
    out = os.environ.get("TCGWATCH_PERF_REPORT")
    if out:
        Path(out).write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def test_seeded_db_has_realistic_size(perf_env, report):
    with Store(perf_env["db"], readonly=True) as store:
        printings = store.conn.execute("SELECT COUNT(*) FROM (SELECT DISTINCT product_id, printing FROM prices)").fetchone()[0]
        rows = store.conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        days = store.snapshot_days("op")
    _record(report, "size", "printings", printings)
    _record(report, "size", "price_rows", rows)
    _record(report, "size", "days", days)
    assert printings >= 30_000
    assert rows >= 5_000_000
    assert days == 180


def test_web_endpoints_timing(perf_env, report, monkeypatch):
    plans: dict[str, str] = {}
    _capture_plans(monkeypatch, perf_env["db"], plans)
    client = TestClient(create_app(perf_env["cfg"]), base_url="http://localhost")

    for game in ("op", "riftbound", "pokemon"):
        secs, resp = _median(lambda g=game: client.get(f"/api/cards/{g}"))
        assert resp.status_code == 200
        rows = len(resp.json()["rows"])
        assert rows > 0, f"/api/cards/{game} returned no rows: benchmark is not on the seeded data"
        _record(report, "timings_s", f"GET /api/cards/{game}", round(secs, 3))
        _record(report, "rows", f"/api/cards/{game}", rows)

    secs, resp = _median(lambda: client.get("/api/watchlist/op"))
    assert resp.status_code == 200
    watched = len(resp.json())
    assert watched > 0, "/api/watchlist/op returned no rows: benchmark is not on the seeded data"
    _record(report, "timings_s", "GET /api/watchlist/op", round(secs, 3))
    _record(report, "rows", "/api/watchlist/op", watched)

    _record(report, "plans", "endpoints", _explain(perf_env["db"], plans))


def test_check_timing(perf_env, report, monkeypatch):
    plans: dict[str, str] = {}
    _capture_plans(monkeypatch, perf_env["db"], plans)
    with Store(perf_env["db"], readonly=True) as store:
        assert store.snapshot_days("op") == 180, "seeded DB is not the expected 180-day shape"
    t0 = time.perf_counter()
    rc = cli.main(["--config", str(perf_env["cfg"]), "check", "--preview", "--quiet"])
    secs = time.perf_counter() - t0
    assert rc == 0
    _record(report, "timings_s", "tcgwatch check --preview (all games)", round(secs, 3))
    _record(report, "plans", "check", _explain(perf_env["db"], plans))


def _capture_plans(monkeypatch, db: Path, plans: dict[str, str]) -> None:
    """Record every distinct SELECT the code runs, with its parameters expanded by sqlite."""
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if str(db) in str(args[0]):
            conn.set_trace_callback(lambda sql: _keep(plans, sql))
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)


def _keep(plans: dict[str, str], sql: str) -> None:
    """Keep the first expanded statement per shape (literal values replaced), so per-series
    queries (one per watched card) collapse to a single entry."""
    if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
        return
    text = " ".join(sql.split())
    shape = re.sub(r"'[^']*'|\b\d+(\.\d+)?\b", "?", text)
    plans.setdefault(shape, text)


def _explain(db: Path, plans: dict[str, str]) -> dict[str, list[str]]:
    """EXPLAIN QUERY PLAN for each captured statement, on a separate connection."""
    out: dict[str, list[str]] = {}
    conn = sqlite3.connect(str(db))
    try:
        for sql in plans.values():
            try:
                rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
            except sqlite3.Error as exc:  # captured text can be a fragment; keep the error visible
                rows = [("error", str(exc))]
            out[sql] = [r[-1] for r in rows]
    finally:
        conn.close()
    return out

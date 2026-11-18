"""Every caught urllib HTTPError is closed (FIX-HTTP-1): the body fp must be closed on each path."""
import io
import urllib.error
import urllib.request
from argparse import Namespace

import pytest

from tcgwatch import cli, notify
from tcgwatch.config import parse_config
from tcgwatch.sources import tcgcsv

pytestmark = pytest.mark.filterwarnings("error::ResourceWarning")


def _err(code, headers=None):
    fp = io.BytesIO(b"body")
    return urllib.error.HTTPError("https://x.test", code, "x", headers or {}, fp), fp


def test_web_health_closes_http_error(monkeypatch):
    err, fp = _err(401)

    def deny(req, timeout):
        raise err
    monkeypatch.setattr(urllib.request, "urlopen", deny)
    assert cli.cmd_web_health(parse_config({}), Namespace(port=1, timeout=1)) == 1
    assert fp.closed


def test_post_discord_closes_429_before_sleeping(monkeypatch):
    err, fp = _err(429, {"Retry-After": "0.01"})
    state = []

    def fake(req, timeout):
        if not state:
            state.append(1)
            raise err
        return io.BytesIO()
    slept = []
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake)
    monkeypatch.setattr(notify.time, "sleep", lambda s: slept.append(fp.closed))
    notify.post_discord("https://discord.test/hook", "hi")
    assert slept == [True]  # closed before the wait, retry then succeeded


def test_tcgcsv_get_closes_retried_errors(monkeypatch):
    err, fp = _err(503)
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise err
        return Resp(b"ok")
    monkeypatch.setattr(tcgcsv.urllib.request, "urlopen", fake)
    monkeypatch.setattr(tcgcsv.time, "sleep", lambda s: None)
    assert tcgcsv.TcgCsvSource("ua")._get("/p") == b"ok"
    assert fp.closed


def test_tcgcsv_get_closes_error_when_budget_exhausted(monkeypatch):
    err, fp = _err(503, {"Retry-After": "9999"})
    monkeypatch.setattr(tcgcsv.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(err))
    monkeypatch.setattr(tcgcsv.time, "sleep", lambda s: None)
    monkeypatch.setattr(tcgcsv, "RETRY_BUDGET", 0)
    with pytest.raises(urllib.error.HTTPError):
        tcgcsv.TcgCsvSource("ua")._get("/p")
    assert fp.closed


def test_archive_404_closes_error(monkeypatch):
    pytest.importorskip("py7zr")
    err, fp = _err(404)

    def fake(req, timeout=None):
        raise err
    monkeypatch.setattr(tcgcsv.urllib.request, "urlopen", fake)
    from datetime import date
    assert tcgcsv.TcgCsvSource("ua").archive_prices(date(2026, 1, 1), {1}) is None
    assert fp.closed

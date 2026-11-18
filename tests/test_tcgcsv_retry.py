import io
import urllib.error

import pytest

from tcgwatch.sources import tcgcsv
from tcgwatch.sources.tcgcsv import TcgCsvSource


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, headers=None):
    """HTTPError with a closed body, so urllib does not warn about an unclosed file on GC."""
    err = urllib.error.HTTPError("u", code, "x", headers or {}, io.BytesIO(b""))
    err.close()
    return err


def _err(code):
    return _http_error(code)


def _src(monkeypatch, outcomes):
    calls, sleeps = [], []

    def fake(req, timeout=None):
        calls.append(req)
        o = outcomes[len(calls) - 1]
        if isinstance(o, Exception):
            raise o
        return _Resp(o)

    monkeypatch.setattr(tcgcsv.urllib.request, "urlopen", fake)
    monkeypatch.setattr(tcgcsv.time, "sleep", sleeps.append)
    return TcgCsvSource("ua", request_delay_seconds=0, backoff_seconds=1.0, max_attempts=4), calls, sleeps


@pytest.mark.parametrize("exc", [_err(503), _err(429), urllib.error.URLError("dns"), TimeoutError()])
def test_retries_transient_then_succeeds(monkeypatch, exc):
    s, calls, sleeps = _src(monkeypatch, [exc, exc, b"ok"])
    assert s._get("/x") == b"ok"
    assert len(calls) == 3
    assert sleeps[:2] == [1.0, 2.0]


def test_gives_up_after_max_attempts(monkeypatch):
    s, calls, _ = _src(monkeypatch, [_err(500)] * 4)
    with pytest.raises(urllib.error.HTTPError):
        s._get("/x")
    assert len(calls) == 4


def test_404_not_retried(monkeypatch):
    s, calls, _ = _src(monkeypatch, [_err(404), b"ok"])
    with pytest.raises(urllib.error.HTTPError):
        s._get("/x")
    assert len(calls) == 1


def test_archive_404_still_none(monkeypatch):
    import datetime
    s, calls, _ = _src(monkeypatch, [_err(404)])
    assert s.archive_prices(datetime.date(2026, 1, 1), {1}) is None
    assert len(calls) == 1


def _ra(code, value):
    return _http_error(code, {"Retry-After": value})


@pytest.mark.parametrize("code", [429, 503])
def test_retry_after_seconds(monkeypatch, code):
    s, calls, sleeps = _src(monkeypatch, [_ra(code, "7"), b"ok"])
    assert s._get("/x") == b"ok"
    assert sleeps[:-1] == [7.0]  # last entry is the post-success request delay


def test_retry_after_http_date(monkeypatch):
    import datetime as dt
    monkeypatch.setattr(tcgcsv, "_now", lambda: dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc))
    s, calls, sleeps = _src(monkeypatch, [_ra(429, "Thu, 01 Jan 2026 00:00:30 GMT"), b"ok"])
    assert s._get("/x") == b"ok"
    assert sleeps[:-1] == [30.0]  # last entry is the post-success request delay


def test_retry_after_past_date_retries_now(monkeypatch):
    import datetime as dt
    monkeypatch.setattr(tcgcsv, "_now", lambda: dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc))
    s, _, sleeps = _src(monkeypatch, [_ra(503, "Thu, 01 Jan 2026 00:00:30 GMT"), b"ok"])
    assert s._get("/x") == b"ok"
    assert sleeps[:-1] == [0.0]  # last entry is the post-success request delay


@pytest.mark.parametrize("bad", ["soon", "-5", "", "1.5"])
def test_invalid_retry_after_falls_back_to_exponential(monkeypatch, bad):
    s, _, sleeps = _src(monkeypatch, [_ra(429, bad), _ra(429, bad), b"ok"])
    assert s._get("/x") == b"ok"
    assert sleeps[:-1] == [1.0, 2.0]  # last entry is the post-success request delay


def test_retry_after_clamped_to_cap(monkeypatch):
    s, _, sleeps = _src(monkeypatch, [_ra(429, "3600"), b"ok"])
    assert s._get("/x") == b"ok"
    assert sleeps[:-1] == [tcgcsv.MAX_RETRY_WAIT]  # last entry is the post-success request delay


def test_stops_early_when_budget_exhausted(monkeypatch):
    s, calls, sleeps = _src(monkeypatch, [_ra(429, "60"), _ra(429, "60"), _ra(429, "60"), b"ok"])
    with pytest.raises(urllib.error.HTTPError):
        s._get("/x")
    assert sleeps == [60.0, 60.0]
    assert len(calls) == 3


@pytest.mark.parametrize("exc", [urllib.error.URLError("x"), TimeoutError(), ConnectionError()])
def test_budget_applies_to_network_errors_after_http_waits(monkeypatch, exc):
    s, calls, sleeps = _src(monkeypatch, [_ra(429, "60"), _ra(429, "60"), exc, b"ok"])
    with pytest.raises(type(exc)) as ei:
        s._get("/x")
    assert ei.value is exc  # original exception, not wrapped
    assert sleeps == [60.0, 60.0]  # no third wait
    assert len(calls) == 3  # no 4th request


def test_budget_applies_to_http_error_after_network_error(monkeypatch):
    s, calls, sleeps = _src(
        monkeypatch, [urllib.error.URLError("x"), _ra(429, "60"), _ra(429, "60"), b"ok"])
    with pytest.raises(urllib.error.HTTPError):
        s._get("/x")
    assert sleeps == [1.0, 60.0]
    assert len(calls) == 3

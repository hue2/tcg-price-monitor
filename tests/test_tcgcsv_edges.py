"""tcgcsv parse/error paths: Retry-After parsing, last-updated, archive_prices (no network, no real sleeps)."""

import datetime
import io
import json
import urllib.error
from datetime import timezone

import py7zr
import pytest

from tcgwatch.sources import tcgcsv
from tcgwatch.sources.tcgcsv import TcgCsvSource, _retry_after

DAY = datetime.date(2026, 1, 1)


@pytest.fixture
def src():
    return TcgCsvSource("ua", request_delay_seconds=0)


def _http_error(code, reason="forbidden"):
    """HTTPError whose body is closed: the code under test reads only status and headers,
    and an unclosed body makes urllib warn ("Implicitly cleaning up") when it is garbage-collected."""
    err = urllib.error.HTTPError("u", code, reason, {}, io.BytesIO(b""))
    err.close()
    return err


# --- _retry_after / _now ---------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "   "])
def test_retry_after_absent_is_none(value):
    assert _retry_after(value) is None


def test_retry_after_seconds_with_whitespace():
    assert _retry_after(" 12 ") == 12.0


def test_retry_after_non_ascii_digits_rejected():
    assert _retry_after("١٢") is None  # Arabic-Indic digits: isdigit() but not ascii


def test_retry_after_naive_http_date_assumed_utc(monkeypatch):
    now = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tcgcsv, "_now", lambda: now)
    # RFC 2822 date with "-0000" parses as naive; must be treated as UTC, not raise on subtraction
    assert _retry_after("Thu, 01 Jan 2026 12:00:30 -0000") == pytest.approx(30.0)


def test_retry_after_aware_http_date(monkeypatch):
    now = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tcgcsv, "_now", lambda: now)
    assert _retry_after("Thu, 01 Jan 2026 12:01:00 GMT") == pytest.approx(60.0)
    assert _retry_after("Thu, 01 Jan 2026 11:00:00 GMT") == 0.0


def test_now_is_timezone_aware_utc():
    assert tcgcsv._now().tzinfo is not None and tcgcsv._now().utcoffset().total_seconds() == 0


# --- published_at / snapshot_date ------------------------------------------------------------

def test_published_at_and_snapshot_date(src, monkeypatch):
    paths = []

    def fake_get(path):
        paths.append(path)
        return b" 2026-03-05T20:31:07+00:00\n"

    monkeypatch.setattr(src, "_get", fake_get)
    assert src.published_at() == datetime.datetime(2026, 3, 5, 20, 31, 7, tzinfo=timezone.utc)
    assert src.snapshot_date() == datetime.date(2026, 3, 5)
    assert paths == ["/last-updated.txt"] * 2


def test_published_at_garbage_raises(src, monkeypatch):
    monkeypatch.setattr(src, "_get", lambda path: b"not a date")
    with pytest.raises(ValueError):
        src.published_at()


# --- _results error shapes -------------------------------------------------------------------

def test_results_missing_success_key_is_an_error(src, monkeypatch):
    monkeypatch.setattr(src, "_get", lambda path: json.dumps({"results": []}).encode())
    with pytest.raises(tcgcsv.TcgCsvError, match=r"/p: None"):
        src._results("/p")


def test_results_invalid_json_propagates(src, monkeypatch):
    monkeypatch.setattr(src, "_get", lambda path: b"<html>502</html>")
    with pytest.raises(ValueError):
        src._results("/p")


# --- archive_prices --------------------------------------------------------------------------

def _make_archive(tmp_path, entries: dict[str, object]) -> bytes:
    """Build a real .7z whose members are JSON files at the given archive paths."""
    root = tmp_path / "build"
    for name, payload in entries.items():
        f = root / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    out = tmp_path / "a.7z"
    with py7zr.SevenZipFile(out, "w") as z:
        for name in entries:
            z.write(root / name, arcname=name)
    return out.read_bytes()


def _row(pid, market, printing="Normal"):
    return {"productId": pid, "subTypeName": printing, "marketPrice": market}


def test_archive_prices_filters_categories_and_pattern(src, monkeypatch, tmp_path):
    blob = _make_archive(tmp_path, {
        "2026-01-01/68/10/prices": {"success": True, "results": [_row(1, 1.5), _row(2, 2.5, "Foil")]},
        "2026-01-01/68/11/prices": {"success": True, "results": [_row(3, 3.5)]},
        "2026-01-01/3/5/prices": {"success": True, "results": [_row(9, 9.5)]},  # category not requested
        "2026-01-01/68/10/products": {"results": [{"productId": 1}]},  # not a prices entry
        "2026-01-01/68/notes.txt": "ignore me",
    })
    paths = []
    monkeypatch.setattr(src, "_get", lambda p: paths.append(p) or blob)
    out = src.archive_prices(DAY, {68, 777})
    assert paths == ["/archive/tcgplayer/prices-2026-01-01.ppmd.7z"]
    assert set(out) == {68, 777}
    assert out[777] == []  # requested but absent from the archive
    assert sorted((p.product_id, p.printing, p.market) for p in out[68]) == [
        (1, "Normal", 1.5), (2, "Foil", 2.5), (3, "Normal", 3.5)]


def test_archive_prices_no_matching_entries_gives_empty_lists(src, monkeypatch, tmp_path):
    blob = _make_archive(tmp_path, {"2026-01-01/3/5/prices": {"results": [_row(9, 9.5)]}})
    monkeypatch.setattr(src, "_get", lambda p: blob)
    assert src.archive_prices(DAY, {68}) == {68: []}


def test_archive_prices_missing_results_key_is_empty(src, monkeypatch, tmp_path):
    blob = _make_archive(tmp_path, {"2026-01-01/68/10/prices": {"success": False}})
    monkeypatch.setattr(src, "_get", lambda p: blob)
    assert src.archive_prices(DAY, {68}) == {68: []}


def test_archive_prices_rejects_traversal_style_names(src, monkeypatch, tmp_path):
    # Only names matching the strict pattern are extracted; a lookalike with extra path parts is ignored.
    blob = _make_archive(tmp_path, {"x/2026-01-01/68/10/prices": {"results": [_row(1, 1.0)]}})
    monkeypatch.setattr(src, "_get", lambda p: blob)
    assert src.archive_prices(DAY, {68}) == {68: []}


def test_archive_prices_non_404_http_error_propagates(src, monkeypatch):
    def boom(path):
        raise _http_error(403)

    monkeypatch.setattr(src, "_get", boom)
    with pytest.raises(urllib.error.HTTPError) as ei:
        src.archive_prices(DAY, {68})
    assert ei.value.code == 403


def test_archive_prices_corrupt_archive_raises(src, monkeypatch):
    monkeypatch.setattr(src, "_get", lambda p: b"definitely not a 7z")
    with pytest.raises(py7zr.Bad7zFile):
        src.archive_prices(DAY, {68})


# --- _get: network errors exhausting max_attempts ---------------------------------------------

@pytest.mark.parametrize("exc", [urllib.error.URLError("dns"), TimeoutError("slow"), ConnectionResetError("rst")])
def test_network_error_on_last_attempt_is_raised_after_backoff(monkeypatch, exc):
    sleeps = []

    def fake(req, timeout=None):
        raise exc

    monkeypatch.setattr(tcgcsv.urllib.request, "urlopen", fake)
    monkeypatch.setattr(tcgcsv.time, "sleep", sleeps.append)
    s = TcgCsvSource("ua", request_delay_seconds=0, backoff_seconds=1.0, max_attempts=3)
    with pytest.raises(type(exc)):
        s._get("/x")
    assert sleeps == [1.0, 2.0]  # slept between attempts, not after the final one

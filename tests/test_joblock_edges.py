"""joblock edge cases: Windows msvcrt path (faked), sidecar I/O failures, poll loop timing (no real sleeps)."""

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

from tcgwatch import joblock
from tcgwatch.joblock import JobLockedError, holder_path, job_lock, lock_path, read_holder


class FakeMsvcrt(types.SimpleNamespace):
    """Stand-in for msvcrt: records calls; ``busy`` makes LK_NBLCK fail like a held byte-range lock."""

    def __init__(self):
        super().__init__(LK_NBLCK=2, LK_UNLCK=0, calls=[], busy=False, unlock_fails=False)

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes))
        if mode == self.LK_NBLCK and self.busy:
            raise OSError("locked")
        if mode == self.LK_UNLCK and self.unlock_fails:
            raise OSError("not locked")


@pytest.fixture
def win(monkeypatch):
    fake = FakeMsvcrt()
    monkeypatch.setattr(joblock, "fcntl", None)
    monkeypatch.setattr(joblock, "msvcrt", fake, raising=False)
    return fake


def test_windows_path_locks_and_unlocks_one_byte(win, tmp_path):
    db = tmp_path / "t.db"
    with job_lock(db, "run"):
        assert win.calls == [(win.LK_NBLCK, 1)]
        assert read_holder(db)["kind"] == "run"
    assert win.calls == [(win.LK_NBLCK, 1), (win.LK_UNLCK, 1)]
    assert read_holder(db) is None


def test_windows_contention_raises_with_holder(win, tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    holder_path(db).write_text(json.dumps({"kind": "backfill", "pid": 42, "started_at": "t0"}))
    win.busy = True
    with pytest.raises(JobLockedError) as ei:
        with job_lock(db, "check"):
            pytest.fail("must not enter")
    assert ei.value.holder["kind"] == "backfill"
    assert "backfill (pid 42, since t0)" in str(ei.value)
    # a failed acquire must not leave the in-process reservation behind
    win.busy = False
    with job_lock(db, "check"):
        pass


def test_windows_unlock_error_is_swallowed(win, tmp_path):
    win.unlock_fails = True
    with job_lock(tmp_path / "t.db", "run"):
        pass  # exiting must not raise even though LK_UNLCK failed


def test_module_falls_back_to_msvcrt_when_fcntl_missing(monkeypatch):
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "fcntl", None)  # `import fcntl` -> ImportError
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    spec = importlib.util.spec_from_file_location("joblock_win_copy", joblock.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # private copy: the real module is untouched
    assert mod.fcntl is None and mod.msvcrt is fake


def test_sidecar_write_and_unlink_failures_do_not_break_the_lock(tmp_path):
    db = tmp_path / "t.db"
    holder_path(db).mkdir()  # write_text and unlink on a directory both raise OSError
    with job_lock(db, "run"):
        assert lock_path(db).exists()
    assert holder_path(db).is_dir()


def test_read_holder_tolerates_garbage(tmp_path):
    db = tmp_path / "t.db"
    assert read_holder(db) is None  # missing
    holder_path(db).write_text("{not json")
    assert read_holder(db) is None
    holder_path(db).write_text("[1, 2]")  # valid JSON, wrong shape
    assert read_holder(db) is None
    holder_path(db).write_bytes(b"\xff\xfe\x00")  # undecodable
    assert read_holder(db) is None
    holder_path(db).unlink()
    holder_path(db).mkdir()  # unreadable (a directory)
    assert read_holder(db) is None


def test_error_message_without_or_with_partial_holder():
    assert str(JobLockedError("run", None)) == "cannot start run: another job is already running"
    assert str(JobLockedError("run", {})) == "cannot start run: another job is already running"  # empty = no info
    msg = str(JobLockedError("run", {"pid": 7}))
    assert msg == "cannot start run: a job (pid 7, since None) is already running"


def test_stale_sidecar_is_overwritten_on_acquire(tmp_path):
    db = tmp_path / "t.db"
    holder_path(db).write_text(json.dumps({"kind": "old", "pid": 1, "started_at": "x"}))
    with job_lock(db, "snapshot"):
        info = read_holder(db)
        assert info["kind"] == "snapshot" and info["pid"] == os.getpid()


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def test_file_lock_poll_loop_times_out_without_real_sleep(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(joblock.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(joblock.time, "sleep", clock.sleep)
    monkeypatch.setattr(joblock, "_try_lock", lambda fd: False)
    with pytest.raises(JobLockedError):
        with job_lock(tmp_path / "t.db", "run", wait=1.0):
            pass
    assert clock.sleeps and all(s == joblock.POLL_SECONDS for s in clock.sleeps)
    assert 1.0 <= sum(clock.sleeps) < 1.0 + joblock.POLL_SECONDS + 1e-9
    assert joblock._held == {}  # reservation released


def test_file_lock_poll_loop_acquires_after_a_few_polls(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(joblock.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(joblock.time, "sleep", clock.sleep)
    results = iter([False, False, True])
    monkeypatch.setattr(joblock, "_try_lock", lambda fd: next(results))
    monkeypatch.setattr(joblock, "_unlock", lambda fd: None)
    with job_lock(tmp_path / "t.db", "run", wait=5.0):
        assert len(clock.sleeps) == 2


def test_negative_wait_is_treated_as_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(joblock, "_try_lock", lambda fd: False)
    with pytest.raises(JobLockedError):
        with job_lock(tmp_path / "t.db", "run", wait=-5):
            pass


def test_same_process_other_thread_wait_expires(tmp_path, monkeypatch):
    """A second thread with a small wait gives up while the owner still holds (fake clock drives _cond.wait)."""
    import threading

    db = tmp_path / "t.db"
    clock = FakeClock()
    out = {}
    entered, done = threading.Event(), threading.Event()

    def owner():
        with job_lock(db, "run"):
            entered.set()
            done.wait(5)

    t = threading.Thread(target=owner)
    t.start()
    assert entered.wait(5)
    monkeypatch.setattr(joblock.time, "monotonic", clock.monotonic)
    # each _cond.wait advances the fake clock instead of really waiting
    monkeypatch.setattr(joblock._cond, "wait", lambda timeout=None: clock.sleep(timeout) or True, raising=False)
    try:
        with pytest.raises(JobLockedError) as ei:
            with job_lock(db, "check", wait=0.5):
                pass
        out["holder"] = ei.value.holder
    finally:
        monkeypatch.undo()
        done.set()
        t.join(5)
    assert out["holder"]["kind"] == "run"
    assert clock.sleeps and sum(clock.sleeps) == pytest.approx(0.5)

"""Cross-process job lock so only one snapshot/backfill/run/check touches the DB at a time.

An advisory lock on ``<db>.lock`` (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows). The OS
releases it when the process dies, so there is no stale-lock cleanup. Holder info (kind, pid, start
time) goes in the sidecar ``<db>.lock.json``: informational only and possibly stale, the lock is
authoritative. It never lives in the locked file because Windows byte-range locks block reads.

Same-host only (not NFS safe). Re-entrant within a thread (other threads wait), so ``run`` can call ``snapshot``/``check``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

POLL_SECONDS = 0.2

_held: dict[str, dict] = {}  # canonical lock path -> {owner thread id, re-entry depth} (this process)
_cond = threading.Condition()


class JobLockedError(RuntimeError):
    def __init__(self, kind: str, holder: dict | None):
        self.kind, self.holder = kind, holder
        who = "another job"
        if holder:
            who = f"{holder.get('kind', 'a job')} (pid {holder.get('pid')}, since {holder.get('started_at')})"
        super().__init__(f"cannot start {kind}: {who} is already running")


def _canon(db: Path | str) -> str:
    """Canonical database identity (symlinks resolved) so aliases share one lock."""
    return os.path.realpath(db)


def lock_path(db: Path | str) -> Path:
    return Path(f"{_canon(db)}.lock")


def holder_path(db: Path | str) -> Path:
    return Path(f"{_canon(db)}.lock.json")


def read_holder(db: Path | str) -> dict | None:
    """Best-effort holder info from the sidecar; may be stale or missing."""
    try:
        data = json.loads(holder_path(db).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _try_lock(fd: int) -> bool:
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def job_lock(db: Path | str, kind: str, wait: float = 0.0):
    """Hold the job lock for the ``with`` body; raise JobLockedError if busy after ``wait`` seconds.

    No-op for ``:memory:`` databases. Re-entry is allowed only for the owning thread; other threads
    of this process wait (up to ``wait``) like any other process, so the lock lives until the owner's
    outermost exit.
    """
    if str(db) == ":memory:":
        yield
        return
    path = lock_path(db)
    key = str(path)
    me = threading.get_ident()
    deadline = time.monotonic() + max(wait, 0.0)
    with _cond:
        while True:
            st = _held.get(key)
            if st is None:
                _held[key] = st = {"owner": me, "depth": 1}  # reserve; others wait on _cond
                break
            if st["owner"] == me:  # re-entrant, same thread only
                st["depth"] += 1
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JobLockedError(kind, read_holder(db))
            _cond.wait(min(remaining, POLL_SECONDS))
        nested = st["depth"] > 1
    if nested:
        try:
            yield
        finally:
            with _cond:
                st["depth"] -= 1
        return

    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise JobLockedError(kind, read_holder(db))
            time.sleep(POLL_SECONDS)
        info = {
            "kind": kind,
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            holder_path(db).write_text(json.dumps(info), encoding="utf-8")
        except OSError:
            pass
        try:
            yield
        finally:
            try:
                holder_path(db).unlink()
            except OSError:
                pass
            _unlock(fd)
    finally:
        if fd is not None:
            os.close(fd)
        with _cond:
            _held.pop(key, None)
            _cond.notify_all()

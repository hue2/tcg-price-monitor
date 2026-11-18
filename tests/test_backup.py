"""BACKUP-1: `tcgwatch backup <dest.db>` (online, WAL-safe, job-locked, 0600, no silent overwrite)."""

import errno
import os
import shutil
import sqlite3
import stat
import threading
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from tcgwatch import cli
from tcgwatch.config import parse_config
from tcgwatch.joblock import job_lock
from tcgwatch.models import Card, PricePoint
from tcgwatch.store import Store


def make_cfg(tmp_path):
    return parse_config({"database": str(tmp_path / "t.db"),
                         "games": {"op": {"name": "OP", "category_id": 68}}}, base_dir=tmp_path)


def seed(cfg):
    with Store(cfg.database) as s:
        s.upsert_cards([Card(1, "op", 1, "S", "Keep", "1", "R")])
        s.write_snapshot(date(2026, 11, 7), [([], [PricePoint(1, "Normal", 1.5, None, None, None)])])


def args(dest, force=False, wait=0):
    return SimpleNamespace(dest=str(dest), force=force, lock_wait=wait)


def count(path):
    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    finally:
        c.close()


def test_round_trip_restores_rows_with_mode_0600(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    dest = tmp_path / "bk" / "copy.db"
    assert cli.cmd_backup(cfg, args(dest)) == 0
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert count(dest) == 1
    assert not list(dest.parent.glob("*-wal")) and not list(dest.parent.glob(".*tmp"))
    with Store(dest) as s:  # the copy is a usable database
        assert s.conn.execute("SELECT name FROM cards").fetchone()[0] == "Keep"


def test_captures_uncheckpointed_wal_rows(tmp_path):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    live = Store(cfg.database)  # keep open and commit here so rows sit only in the -wal
    try:
        live.upsert_cards([Card(2, "op", 1, "S", "Wal", "2", "R")])
        live.write_snapshot(date(2026, 11, 8), [([], [PricePoint(2, "Normal", 2.5, None, None, None)])])
        dest = tmp_path / "c.db"
        assert cli.cmd_backup(cfg, args(dest)) == 0
        assert count(dest) == 2
    finally:
        live.close()


def test_refuses_overwrite_without_force(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    dest = tmp_path / "c.db"
    dest.write_bytes(b"precious")
    assert cli.cmd_backup(cfg, args(dest)) == 1
    assert "--force" in capsys.readouterr().err
    assert dest.read_bytes() == b"precious"
    assert cli.cmd_backup(cfg, args(dest, force=True)) == 0
    assert count(dest) == 1 and stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_rejects_live_db_dir_and_missing_source(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    assert cli.cmd_backup(cfg, args(tmp_path / "x.db")) == 1  # no DB yet
    seed(cfg)
    assert cli.cmd_backup(cfg, args(cfg.database, force=True)) == 2
    assert cli.cmd_backup(cfg, args(tmp_path, force=True)) == 2


def test_mid_job_run_is_busy_then_waits(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    dest = tmp_path / "c.db"
    release = threading.Event()
    held = threading.Event()

    def job():
        with job_lock(cfg.database, "snapshot"):
            held.set()
            release.wait(10)

    t = threading.Thread(target=job)
    t.start()
    held.wait(5)
    try:
        rc = cli.main(["--config", str(write_cfg(tmp_path)), "backup", str(dest)])
        assert rc == 3 and not dest.exists()
        threading.Timer(0.5, release.set).start()
        assert cli.cmd_backup(cfg, args(dest, wait=10)) == 0
        assert count(dest) == 1
    finally:
        release.set()
        t.join()


def write_cfg(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(f"database: {tmp_path / 't.db'}\ngames:\n  op: {{name: OP, category_id: 68}}\n")
    return p


def test_symlinked_temp_cannot_truncate_live_db_or_other_files(tmp_path):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    before = Path(cfg.database).read_bytes()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep me")
    dest = tmp_path / "c.db"
    import os
    for name in (f".c.db.{os.getpid()}.tmp", ".c.db.tmp"):  # the old predictable names
        os.symlink(cfg.database, tmp_path / name)
    os.symlink(victim, tmp_path / ".c.db.0.tmp")
    assert cli.cmd_backup(cfg, args(dest)) == 0
    assert Path(cfg.database).read_bytes() == before and victim.read_bytes() == b"keep me"
    assert count(cfg.database) == 1 and count(dest) == 1 and not dest.is_symlink()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_temp_is_0600_even_with_permissive_umask(tmp_path):
    import os
    cfg = make_cfg(tmp_path)
    seed(cfg)
    old = os.umask(0)
    try:
        dest = tmp_path / "c.db"
        assert cli.cmd_backup(cfg, args(dest)) == 0
    finally:
        os.umask(old)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_refuses_sidecar_and_lock_destinations_even_with_force(tmp_path, capsys):
    import os
    from tcgwatch.joblock import holder_path, lock_path
    cfg = make_cfg(tmp_path)
    seed(cfg)
    live = Store(cfg.database)  # open: -wal/-shm exist with committed rows
    try:
        live.upsert_cards([Card(2, "op", 1, "S", "Wal", "2", "R")])
        live.write_snapshot(date(2026, 11, 8), [([], [PricePoint(2, "Normal", 2.5, None, None, None)])])
        db = str(cfg.database)
        alias = tmp_path / "alias"
        os.symlink(tmp_path, alias)
        for d in (db + "-wal", db + "-shm", db + "-journal", lock_path(db), holder_path(db),
                  alias / "t.db-wal", alias / "t.db.lock"):
            sig = {p: p.read_bytes() for p in tmp_path.glob("t.db*") if p.is_file()}
            assert cli.cmd_backup(cfg, args(d, force=True)) == 2, d
            assert {p: p.read_bytes() for p in tmp_path.glob("t.db*") if p.is_file()} == sig
            assert "live database" in capsys.readouterr().err
        assert count(cfg.database) == 2
    finally:
        live.close()


def test_directory_is_rc2_even_without_force(tmp_path):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    assert cli.cmd_backup(cfg, args(tmp_path)) == 2


def test_dest_appearing_during_lock_wait_is_not_clobbered(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    dest = tmp_path / "c.db"
    release, held = threading.Event(), threading.Event()

    def job():
        with job_lock(cfg.database, "snapshot"):
            held.set()
            release.wait(10)

    t = threading.Thread(target=job)
    t.start()
    held.wait(5)

    def arrive():
        dest.write_bytes(b"precious")
        release.set()

    threading.Timer(0.4, arrive).start()
    try:
        assert cli.cmd_backup(cfg, args(dest, wait=10)) == 1
    finally:
        release.set()
        t.join()
    assert dest.read_bytes() == b"precious" and "--force" in capsys.readouterr().err
    assert not list(tmp_path.glob(".c.db*"))


def test_dest_appearing_before_publish_is_not_clobbered(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    dest = tmp_path / "c.db"
    real = cli.os.chmod

    def chmod_then_arrive(path, mode):
        real(path, mode)
        dest.write_bytes(b"precious")  # lands after the pre-checks, before publication

    monkeypatch.setattr(cli.os, "chmod", chmod_then_arrive)
    assert cli.cmd_backup(cfg, args(dest)) == 1
    assert dest.read_bytes() == b"precious" and "--force" in capsys.readouterr().err
    assert not list(tmp_path.glob(".c.db*"))


@pytest.mark.parametrize("err", [errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.EIO])
def test_no_hardlink_fails_safely_without_force(tmp_path, monkeypatch, capsys, err):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    dest = tmp_path / "c.db"

    def nolink(*a, **k):
        raise OSError(err, "hard links unsupported")

    def no_replace(*a, **k):
        raise AssertionError("must not replace/reserve without --force")

    monkeypatch.setattr(cli.os, "link", nolink)
    monkeypatch.setattr(cli.os, "replace", no_replace)
    assert cli.cmd_backup(cfg, args(dest)) == 2
    assert "--force" in capsys.readouterr().err
    assert not dest.exists() and not list(tmp_path.glob(".c.db*"))  # no 0-byte dest, temp removed


def test_no_hardlink_does_not_clobber_concurrent_dest(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    dest = tmp_path / "c.db"
    real = cli.os.chmod

    def chmod_then_arrive(path, mode):
        real(path, mode)
        dest.write_bytes(b"precious")

    def nolink(*a, **k):
        raise OSError(errno.EOPNOTSUPP, "x")

    monkeypatch.setattr(cli.os, "chmod", chmod_then_arrive)
    monkeypatch.setattr(cli.os, "link", nolink)
    assert cli.cmd_backup(cfg, args(dest)) == 2
    assert dest.read_bytes() == b"precious"


def test_no_hardlink_force_still_replaces(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    dest = tmp_path / "c.db"
    dest.write_bytes(b"old")

    def nolink(*a, **k):
        raise OSError(errno.EPERM, "x")

    monkeypatch.setattr(cli.os, "link", nolink)
    assert cli.cmd_backup(cfg, args(dest, force=True)) == 0 and count(dest) == 1


def test_output_connect_failure_closes_source_connection(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    closed = []
    real = sqlite3.connect

    class Spy:
        def __init__(self, c):
            self.c = c

        def close(self):
            closed.append(1)
            self.c.close()

        def __getattr__(self, n):
            return getattr(self.c, n)

    calls = []

    def fake(*a, **k):
        calls.append(a)
        if len(calls) == 2:
            raise sqlite3.OperationalError("cannot open output")
        return Spy(real(*a, **k))

    monkeypatch.setattr(cli.sqlite3, "connect", fake)
    dest = tmp_path / "c.db"
    try:
        cli.cmd_backup(cfg, args(dest))
    except sqlite3.OperationalError:
        pass
    assert closed == [1] and not dest.exists() and not list(tmp_path.glob(".c.db*"))


# TEST-11: backup -> documented restore round trip (README/SPEC §8 "Restore" steps).

def plant_stale_sidecars(path):
    """Make `path` a database with an older, uncheckpointed WAL and return its -wal/-shm bytes.

    The -wal belongs to this (different) database, so restoring a backup over `path` while the
    sidecars are still there is the mistake the documented steps exist to prevent.
    """
    s = Store(path)
    s.upsert_cards([Card(9, "op", 1, "S", "Stale", "9", "R")])
    s.write_snapshot(date(2026, 10, 1), [([], [PricePoint(9, "Normal", 9.9, None, None, None)])])
    wal = Path(f"{path}-wal").read_bytes()
    shm = Path(f"{path}-shm").read_bytes()
    s.close()
    assert len(wal) > 32  # carries frames, i.e. rows that were never checkpointed
    Path(f"{path}-wal").write_bytes(wal)
    Path(f"{path}-shm").write_bytes(shm)
    return wal, shm


def restored_state(path):
    """(integrity_check result, sorted card names, price row count) read through plain sqlite3."""
    c = sqlite3.connect(str(path))
    try:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        names = sorted(r[0] for r in c.execute("SELECT name FROM cards"))
        return integrity, names, c.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    finally:
        c.close()


def documented_restore(backup, target, delete_sidecars=True):
    """The README/SPEC §8 restore steps: drop sidecars, copy the backup over, chmod 0600."""
    if delete_sidecars:
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(backup, target)
    os.chmod(target, 0o600)


def make_backup_with_wal_rows(tmp_path):
    """Backup taken while two rows sit only in the live -wal. Returns (backup path, expected state)."""
    cfg = make_cfg(tmp_path)
    seed(cfg)
    live = Store(cfg.database)
    try:
        live.upsert_cards([Card(2, "op", 1, "S", "Wal", "2", "R")])
        live.write_snapshot(date(2026, 11, 8), [([], [PricePoint(2, "Normal", 2.5, None, None, None)])])
        backup = tmp_path / "bk" / "backup.db"
        assert cli.cmd_backup(cfg, args(backup)) == 0
    finally:
        live.close()
    return backup, ("ok", ["Keep", "Wal"], 2)


def test_restore_documented_steps_round_trip(tmp_path):
    backup, expected = make_backup_with_wal_rows(tmp_path)
    assert restored_state(backup) == expected
    target = tmp_path / "restore" / "tcgwatch.db"
    target.parent.mkdir()
    plant_stale_sidecars(target)
    os.chmod(target, 0o644)  # so the chmod step below is what makes it 0600
    assert Path(f"{target}-wal").exists() and Path(f"{target}-shm").exists()

    documented_restore(backup, target)

    assert not Path(f"{target}-wal").exists() and not Path(f"{target}-shm").exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert restored_state(target) == expected
    with Store(target) as s:  # and the app can open it again
        assert s.conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 2


def test_restore_without_deleting_stale_sidecars_is_detected(tmp_path):
    """Regression: skipping the sidecar step is not caught by integrity_check.

    SQLite replays the foreign -wal into the restored file (its salts and checksums are
    self-consistent), so the result reads as "ok" while the rows are wrong. The only detector is
    comparing the restored contents against what the backup holds, which is what this asserts.
    """
    backup, expected = make_backup_with_wal_rows(tmp_path)
    target = tmp_path / "restore" / "tcgwatch.db"
    target.parent.mkdir()
    plant_stale_sidecars(target)

    documented_restore(backup, target, delete_sidecars=False)

    integrity, names, prices = restored_state(target)
    assert integrity == "ok"  # integrity_check alone passes: the corruption is silent
    assert (names, prices) != (expected[1], expected[2])  # but the contents are not the backup's


# RESTORE-1: `tcgwatch restore <backup.db>` (inverse of backup).

def rargs(backup, force=False, wait=0):
    return SimpleNamespace(backup=str(backup), force=force, lock_wait=wait)


def test_restore_command_round_trip_with_stale_sidecars(tmp_path, capsys):
    backup, expected = make_backup_with_wal_rows(tmp_path)
    cfg = make_cfg(tmp_path / "r")
    cfg.database.parent.mkdir()
    plant_stale_sidecars(cfg.database)
    os.chmod(cfg.database, 0o644)
    assert cli.cmd_restore(cfg, rargs(backup)) == 1  # exists, no --force
    assert Path(f"{cfg.database}-wal").exists()  # untouched
    assert cli.cmd_restore(cfg, rargs(backup, force=True)) == 0
    assert "restored" in capsys.readouterr().out
    for s in ("-wal", "-shm", "-journal"):
        assert not Path(f"{cfg.database}{s}").exists()
    assert stat.S_IMODE(cfg.database.stat().st_mode) == 0o600
    assert restored_state(cfg.database) == expected
    assert not list(cfg.database.parent.glob(".t.db.*"))
    with Store(cfg.database) as s:
        assert s.conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 2


def test_restore_into_fresh_location_needs_no_force(tmp_path):
    backup, expected = make_backup_with_wal_rows(tmp_path)
    cfg = make_cfg(tmp_path / "new" / "deeper")
    assert cli.cmd_restore(cfg, rargs(backup)) == 0
    assert restored_state(cfg.database) == expected


def test_restore_corrupt_backup_leaves_live_db_untouched(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    before = cfg.database.read_bytes()
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    assert cli.cmd_restore(cfg, rargs(bad, force=True)) == 1
    assert "not a usable backup" in capsys.readouterr().err
    assert cfg.database.read_bytes() == before
    assert not list(tmp_path.glob(".t.db.*"))


def test_restore_failed_integrity_check_is_refused(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    seed(cfg)
    backup = tmp_path / "b.db"
    assert cli.cmd_backup(cfg, args(backup)) == 0
    before = cfg.database.read_bytes()
    real = cli.sqlite3.connect

    class Fake:
        def __init__(self, c):
            self.c = c

        def execute(self, sql, *a):
            if "integrity_check" in sql:
                return SimpleNamespace(fetchone=lambda: ("*** bad page ***",))
            return self.c.execute(sql, *a)

        def __getattr__(self, n):
            return getattr(self.c, n)

    monkeypatch.setattr(cli.sqlite3, "connect", lambda *a, **k: Fake(real(*a, **k)))
    assert cli.cmd_restore(cfg, rargs(backup, force=True)) == 1
    assert cfg.database.read_bytes() == before


def test_restore_refused_paths_and_missing(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cli.cmd_restore(cfg, rargs(tmp_path / "none.db")) == 1
    seed(cfg)
    for p in (cfg.database, f"{cfg.database}-wal", f"{cfg.database}-shm", f"{cfg.database}-journal",
              f"{cfg.database}.lock", f"{cfg.database}.lock.json"):
        Path(p).touch()
        assert cli.cmd_restore(cfg, rargs(p, force=True)) == 2, p
    assert cli.cmd_restore(cfg, rargs(tmp_path, force=True)) == 2
    link = tmp_path / "ln.db"
    link.symlink_to(cfg.database)
    assert cli.cmd_restore(cfg, rargs(link, force=True)) == 2


def test_restore_memory_db(tmp_path):
    cfg = parse_config({"database": ":memory:", "games": {"op": {"name": "OP", "category_id": 68}}}, base_dir=tmp_path)
    assert cli.cmd_restore(cfg, rargs(tmp_path / "x.db")) == 1


def test_restore_busy_then_waits(tmp_path):
    backup, expected = make_backup_with_wal_rows(tmp_path)
    cfg = make_cfg(tmp_path)
    release, held = threading.Event(), threading.Event()

    def job():
        with job_lock(cfg.database, "snapshot"):
            held.set()
            release.wait(10)

    t = threading.Thread(target=job)
    t.start()
    held.wait(5)
    try:
        before = cfg.database.read_bytes()
        assert cli.main(["--config", str(write_cfg(tmp_path)), "restore", str(backup), "--force"]) == 3
        assert cfg.database.read_bytes() == before
        threading.Timer(0.5, release.set).start()
        assert cli.cmd_restore(cfg, rargs(backup, force=True, wait=10)) == 0
        assert restored_state(cfg.database) == expected
    finally:
        release.set()
        t.join()


def test_restore_target_appearing_while_waiting_for_lock(tmp_path, capsys):
    backup, _ = make_backup_with_wal_rows(tmp_path)
    cfg = make_cfg(tmp_path / "r")
    cfg.database.parent.mkdir()
    release, held = threading.Event(), threading.Event()

    def job():
        with job_lock(cfg.database, "snapshot"):
            held.set()
            release.wait(10)

    t = threading.Thread(target=job)
    t.start()
    held.wait(5)
    try:
        def appear():
            cfg.database.write_bytes(b"x")
            release.set()
        threading.Timer(0.3, appear).start()
        assert cli.cmd_restore(cfg, rargs(backup, wait=10)) == 1
        assert cfg.database.read_bytes() == b"x"
    finally:
        release.set()
        t.join()


def test_restore_hostile_path_is_escaped(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    evil = tmp_path / "ev\x1b[2Jil.db"
    assert cli.cmd_restore(cfg, rargs(evil)) == 1
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\\x1b" in err


# FIX-RESTORE-1: WAL survival, canonical symlink target, no-clobber publication, dir-fd preflight.

def _plain_db(path, value):
    c = sqlite3.connect(str(path))
    try:
        c.execute("CREATE TABLE test(value TEXT)")
        c.execute("INSERT INTO test VALUES (?)", (value,))
        c.commit()
    finally:
        c.close()


def _rows(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return [r[0] for r in c.execute("SELECT value FROM test ORDER BY value")]
    finally:
        c.close()


def _wal_only_live(live, backup):
    """live has a checkpointed row plus a committed row that lives only in <live>-wal (writer died)."""
    import subprocess
    import sys
    _plain_db(live, "checkpointed")
    _plain_db(backup, "backup")
    subprocess.run([sys.executable, "-c",
                    "import sqlite3,os,sys;c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA journal_mode=WAL');"
                    "c.execute('PRAGMA wal_autocheckpoint=0');c.execute(\"INSERT INTO test VALUES ('wal-only')\");"
                    "c.commit();os._exit(0)", str(live)], check=True)
    assert Path(f"{live}-wal").exists()
    assert _rows(live) == ["checkpointed", "wal-only"]


def _cfg_for(path):
    return parse_config({"database": str(path), "games": {"op": {"name": "OP", "category_id": 68}}},
                        base_dir=Path(path).parent)


def test_restore_rename_failure_keeps_wal_only_rows(tmp_path, monkeypatch, capsys):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    real = os.replace

    def boom(src, dst, *a, **k):
        if str(dst) == str(live) and str(src).endswith(".tmp"):  # the publish step only
            raise OSError(errno.EIO, "injected")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(cli.os, "replace", boom)
    assert cli.cmd_restore(_cfg_for(live), rargs(backup, force=True)) == 2
    assert "old database restored in place" in capsys.readouterr().err
    assert _rows(live) == ["checkpointed", "wal-only"]
    assert not list(tmp_path.glob("*.pre-restore*")) and not list(tmp_path.glob(".live.db.*"))


def test_restore_crash_before_publish_leaves_recoverable_set(tmp_path):
    import subprocess
    import sys
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    code = ("import os,sys;from pathlib import Path;from types import SimpleNamespace;from tcgwatch import cli;"
            "from tcgwatch.config import parse_config;p=Path(sys.argv[1]);"
            "c=parse_config({'database':str(p),'games':{'op':{'name':'OP','category_id':68}}},base_dir=p.parent);"
            "real=cli.os.replace\n"
            "def r(a,b,*x):\n"
            "    if str(b)==str(p): os._exit(23)\n"
            "    return real(a,b,*x)\n"
            "cli.os.replace=r\n"
            "cli.cmd_restore(c,SimpleNamespace(backup=sys.argv[2],force=True,lock_wait=0))")
    assert subprocess.run([sys.executable, "-c", code, str(live), str(backup)]).returncode == 23
    # the old DB + its WAL are intact under the aside names and open as an ordinary database
    assert _rows(f"{live}.pre-restore") == ["checkpointed", "wal-only"]


def _keep_empty_wal(monkeypatch):
    """The pre-aside checkpoint removes the WAL; recreate an empty one so the sidecar rename paths still run."""
    orig = cli._restore_checkpoint

    def cp(l):
        ok = orig(l)
        Path(f"{l}-wal").write_bytes(b"")
        Path(f"{l}-shm").write_bytes(b"x")
        return ok
    monkeypatch.setattr(cli, "_restore_checkpoint", cp)


def test_restore_aside_failure_touches_nothing(tmp_path, monkeypatch, capsys):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    real = os.replace

    def boom(src, dst, *a, **k):
        if str(dst).endswith(".pre-restore-wal"):
            raise OSError(errno.EIO, "injected")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(cli.os, "replace", boom)
    _keep_empty_wal(monkeypatch)
    assert cli.cmd_restore(_cfg_for(live), rargs(backup, force=True)) == 1
    assert "left untouched" in capsys.readouterr().err
    assert _rows(live) == ["checkpointed", "wal-only"]
    assert not list(tmp_path.glob("*.pre-restore*"))


def test_restore_partial_aside_failure_rolls_back_byte_identical(tmp_path, monkeypatch, capsys):
    """OSError on the 3rd aside rename (main + -wal already moved): exit 1, everything rolled back unchanged."""
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    snap = {}
    orig = cli._restore_checkpoint

    def cp(l):
        ok = orig(l)
        Path(f"{l}-wal").write_bytes(b"w" * 32)
        Path(f"{l}-shm").write_bytes(b"s" * 32)
        snap.update({suf: Path(f"{l}{suf}").read_bytes() for suf in ("", "-wal", "-shm")})  # state at aside time
        return ok
    monkeypatch.setattr(cli, "_restore_checkpoint", cp)
    real = os.replace
    renames = []

    def boom(src, dst, *a, **k):
        if str(dst).endswith(".pre-restore-shm"):
            raise OSError(errno.EIO, "injected")
        if ".pre-restore" in str(dst):
            renames.append(str(dst))
        return real(src, dst, *a, **k)
    monkeypatch.setattr(cli.os, "replace", boom)
    assert cli.cmd_restore(_cfg_for(live), rargs(backup, force=True)) == 1
    err = capsys.readouterr().err
    assert "cannot move the old database aside" in err and "database left untouched" in err
    assert len(renames) == 2  # main and -wal really were moved before the failure, then rolled back
    for suf, data in snap.items():
        assert Path(f"{live}{suf}").read_bytes() == data
    assert not list(tmp_path.glob("*.pre-restore*")) and not list(tmp_path.glob(".live.db.*"))


def test_restore_success_removes_aside_and_old_wal(tmp_path, capsys):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    assert cli.cmd_restore(_cfg_for(live), rargs(backup, force=True)) == 0
    assert _rows(live) == ["backup"]
    assert not list(tmp_path.glob("*.pre-restore*")) and not Path(f"{live}-wal").exists()


def test_restore_refuses_when_earlier_aside_set_exists(tmp_path, capsys):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    Path(f"{live}.pre-restore").write_bytes(b"older recovery data")
    assert cli.cmd_restore(_cfg_for(live), rargs(backup, force=True)) == 1
    assert "pre-restore" in capsys.readouterr().err
    assert Path(f"{live}.pre-restore").read_bytes() == b"older recovery data"
    assert _rows(live) == ["checkpointed", "wal-only"]
    assert cli.cmd_restore(_cfg_for(live), rargs(Path(f"{live}.pre-restore"), force=True)) == 2  # reserved


def test_restore_symlink_target_restores_referent_and_keeps_link(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    live, backup, alias = real_dir / "live.db", tmp_path / "backup.db", tmp_path / "alias.db"
    _wal_only_live(live, backup)
    alias.symlink_to(live)
    cfg = _cfg_for(alias)
    assert cli.cmd_restore(cfg, rargs(backup, force=True)) == 0
    assert alias.is_symlink() and os.readlink(alias) == str(live)
    assert _rows(live) == ["backup"] and _rows(alias) == ["backup"]
    assert not Path(f"{live}-wal").exists() and not list(real_dir.glob("*.pre-restore*"))
    from tcgwatch.joblock import lock_path
    assert lock_path(alias) == lock_path(live)
    assert not list(tmp_path.glob("alias.db.*"))  # nothing created beside the alias


def test_restore_dangling_symlink_creates_referent(tmp_path):
    live, backup, alias = tmp_path / "live.db", tmp_path / "backup.db", tmp_path / "alias.db"
    _plain_db(backup, "backup")
    alias.symlink_to(live)
    assert cli.cmd_restore(_cfg_for(alias), rargs(backup)) == 0
    assert alias.is_symlink() and _rows(live) == ["backup"]


def test_restore_no_force_never_clobbers_file_appearing_mid_copy(tmp_path, monkeypatch, capsys):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _plain_db(backup, "backup")
    real, fired = os.fsync, []

    def late(fd):
        real(fd)
        if not fired and os.readlink(f"/proc/self/fd/{fd}").endswith(".tmp"):
            fired.append(1)
            live.write_bytes(b"precious concurrent target")
    monkeypatch.setattr(cli.os, "fsync", late)
    assert cli.cmd_restore(_cfg_for(live), rargs(backup)) == 1
    assert "already exists" in capsys.readouterr().err
    assert live.read_bytes() == b"precious concurrent target"
    assert not list(tmp_path.glob(".live.db.*")) and not list(tmp_path.glob("*.pre-restore*"))


def test_restore_no_force_appearing_target_keeps_stale_sidecars(tmp_path, monkeypatch):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _plain_db(backup, "backup")
    Path(f"{live}-wal").write_bytes(b"orphan wal")
    real, fired = os.fsync, []

    def late(fd):
        real(fd)
        if not fired and os.readlink(f"/proc/self/fd/{fd}").endswith(".tmp"):
            fired.append(1)
            live.write_bytes(b"x")
    monkeypatch.setattr(cli.os, "fsync", late)
    assert cli.cmd_restore(_cfg_for(live), rargs(backup)) == 1
    assert Path(f"{live}-wal").read_bytes() == b"orphan wal" and live.read_bytes() == b"x"


def test_restore_no_force_without_hard_links_fails_safe(tmp_path, monkeypatch, capsys):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _plain_db(backup, "backup")
    Path(f"{live}-wal").write_bytes(b"orphan wal")

    def nolink(*a, **k):
        raise OSError(errno.EPERM, "no links")
    monkeypatch.setattr(cli.os, "link", nolink)
    assert cli.cmd_restore(_cfg_for(live), rargs(backup)) == 2
    assert "--force" in capsys.readouterr().err
    assert not live.exists() and Path(f"{live}-wal").read_bytes() == b"orphan wal"
    assert not list(tmp_path.glob(".live.db.*")) and not list(tmp_path.glob("*.pre-restore*"))


def test_restore_unopenable_parent_dir_refused_before_any_mutation(tmp_path, monkeypatch, capsys):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    real = os.open

    def deny(path, flags, *a, **k):
        if Path(path) == tmp_path and flags == os.O_RDONLY:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real(path, flags, *a, **k)
    monkeypatch.setattr(cli.os, "open", deny)
    assert cli.cmd_restore(_cfg_for(live), rargs(backup, force=True)) == 2
    assert "left untouched" in capsys.readouterr().err
    assert _rows(live) == ["checkpointed", "wal-only"]
    assert not list(tmp_path.glob("*.pre-restore*")) and not list(tmp_path.glob(".live.db.*"))


def test_restore_skips_dir_fsync_on_windows(tmp_path, monkeypatch):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _plain_db(backup, "backup")
    fd = cli._open_dir_fd(tmp_path)
    assert fd is not None
    os.close(fd)
    monkeypatch.setattr(cli, "_HAS_DIR_FSYNC", False)
    assert cli._open_dir_fd(tmp_path) is None
    assert cli.cmd_restore(_cfg_for(live), rargs(backup)) == 0
    assert _rows(live) == ["backup"]


def test_restore_dir_fsync_failure_reports_replaced_and_keeps_aside(tmp_path, monkeypatch, capsys):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    real, calls = os.fsync, []

    def fs(fd):
        calls.append(fd)
        if len(calls) == 2:  # 1st: temp file; 2nd: the directory
            raise OSError(errno.EIO, "dir fsync failed")
        return real(fd)
    monkeypatch.setattr(cli.os, "fsync", fs)
    assert cli.cmd_restore(_cfg_for(live), rargs(backup, force=True)) == 1
    err = capsys.readouterr().err
    assert "was replaced" in err and "pre-restore" in err
    assert _rows(live) == ["backup"] and _rows(f"{live}.pre-restore") == ["checkpointed", "wal-only"]


def test_restore_aside_rollback_tolerates_failing_rename(tmp_path, monkeypatch):
    live = tmp_path / "live.db"
    _plain_db(live, "x")
    monkeypatch.setattr(cli.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "x")))
    cli._restore_rollback([(str(live), str(live) + ".pre-restore")])  # must not raise


def test_restore_checkpoints_wal_before_aside(tmp_path):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    seen = []
    real = os.replace

    def spy(src, dst, *a, **k):
        if not seen:  # first rename: the WAL must already be folded into the main file
            seen.append(Path(f"{live}-wal").exists() and Path(f"{live}-wal").stat().st_size)
            c = sqlite3.connect(f"{live.as_uri()}?mode=ro&immutable=1", uri=True)
            seen.append([r[0] for r in c.execute("SELECT value FROM test ORDER BY value")])
            c.close()
        return real(src, dst, *a, **k)
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(cli.os, "replace", spy)
    try:
        assert cli.cmd_restore(_cfg_for(live), rargs(backup, force=True)) == 0
    finally:
        mp.undo()
    assert seen[1] == ["checkpointed", "wal-only"] and not seen[0]


def test_restore_busy_checkpoint_refuses_with_exit_3(tmp_path, capsys):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    reader = sqlite3.connect(str(live))
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM test").fetchall()
    # a writer appends after the reader's snapshot, so TRUNCATE cannot finish
    w = sqlite3.connect(str(live))
    w.execute("INSERT INTO test VALUES ('later')")
    w.commit()
    try:
        assert cli.cmd_restore(_cfg_for(live), rargs(backup, force=True)) == 3
    finally:
        w.close()
        reader.close()
    assert "checkpointed" in capsys.readouterr().err
    assert not list(tmp_path.glob("*.pre-restore*"))
    assert "later" in _rows(live)


def test_restore_readonly_wal_db_refuses_nothing_moved(tmp_path, capsys):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    live.chmod(0o444)
    try:
        if os.access(live, os.W_OK):
            import pytest
            pytest.skip("read-only mode not enforced (root)")
        assert cli.cmd_restore(_cfg_for(live), rargs(backup, force=True)) == 2
    finally:
        live.chmod(0o644)
    assert "checkpointed" in capsys.readouterr().err
    assert not list(tmp_path.glob("*.pre-restore*"))
    assert Path(f"{live}-wal").exists()
    assert "wal-only" in _rows(live)


def test_restore_checkpoint_fails_closed_on_operational_error(tmp_path, monkeypatch):
    live = tmp_path / "live.db"
    _plain_db(live, "x")

    def boom(*a, **k):
        e = sqlite3.OperationalError("disk I/O error")
        e.sqlite_errorcode = 778  # SQLITE_IOERR_WRITE
        raise e
    monkeypatch.setattr(cli.sqlite3, "connect", boom)
    assert cli._restore_checkpoint(str(live))[0] == 2


def test_restore_checkpoint_skips_only_corrupt(tmp_path):
    live = tmp_path / "live.db"
    live.write_bytes(b"this is not a sqlite database" * 50)
    assert cli._restore_checkpoint(str(live)) == (0, "")


def test_restore_failed_publication_rollback_is_reported(tmp_path, monkeypatch, capsys):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    real = os.replace

    def boom(src, dst, *a, **k):
        if (str(dst) == str(live) and str(src).endswith(".tmp")) or str(src).endswith(".pre-restore-wal"):
            raise OSError(errno.EIO, "injected")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(cli.os, "replace", boom)
    _keep_empty_wal(monkeypatch)
    assert cli.cmd_restore(_cfg_for(live), rargs(backup, force=True)) == 2
    err = capsys.readouterr().err
    assert "FAILED" in err and "NOT fully restored" in err and "STOPPED" in err
    assert f"{live}.pre-restore-wal" in err and f"{live}.pre-restore" in err  # main stays aside with its WAL
    assert Path(f"{live}.pre-restore-wal").exists() and Path(f"{live}.pre-restore").exists()
    assert not Path(live).exists()  # main never put back without its WAL


def test_restore_failed_aside_rollback_is_reported(tmp_path, monkeypatch, capsys):
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    real = os.replace

    def boom(src, dst, *a, **k):
        if str(dst).endswith(".pre-restore-shm") or str(src).endswith(".pre-restore-wal"):
            raise OSError(errno.EIO, "injected")  # shm aside fails, then wal rollback fails
        return real(src, dst, *a, **k)
    monkeypatch.setattr(cli.os, "replace", boom)
    _keep_empty_wal(monkeypatch)
    assert cli.cmd_restore(_cfg_for(live), rargs(backup, force=True)) == 2
    err = capsys.readouterr().err
    assert "FAILED" in err and f"{live}.pre-restore-wal" in err and "STOPPED" in err
    assert Path(f"{live}.pre-restore-wal").exists()


def test_restore_crash_between_aside_renames_loses_nothing(tmp_path):
    import subprocess
    import sys
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    _wal_only_live(live, backup)
    code = ("import os,sys;from pathlib import Path;from types import SimpleNamespace;from tcgwatch import cli;"
            "from tcgwatch.config import parse_config;p=Path(sys.argv[1]);"
            "c=parse_config({'database':str(p),'games':{'op':{'name':'OP','category_id':68}}},base_dir=p.parent);"
            "real=cli.os.replace\n"
            "def r(a,b,*x):\n"
            "    real(a,b,*x)\n"
            "    if str(b)==str(p)+'.pre-restore': os._exit(23)\n"
            "cli.os.replace=r\n"
            "cli.cmd_restore(c,SimpleNamespace(backup=sys.argv[2],force=True,lock_wait=0))")
    assert subprocess.run([sys.executable, "-c", code, str(live), str(backup)]).returncode == 23
    assert not live.exists()
    # the documented split state: the aside main alone already holds the WAL-only row (checkpointed first)
    assert _rows(f"{live}.pre-restore") == ["checkpointed", "wal-only"]
    # and the documented recovery (rename back) restores the original
    for suf in ("", "-wal", "-shm", "-journal"):
        if os.path.lexists(f"{live}.pre-restore{suf}"):
            os.replace(f"{live}.pre-restore{suf}", f"{live}{suf}")
    assert _rows(live) == ["checkpointed", "wal-only"]

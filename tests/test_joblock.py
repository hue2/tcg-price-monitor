import json
import subprocess
import sys
import threading
import time

import pytest

from tcgwatch import cli
from tcgwatch.joblock import JobLockedError, holder_path, job_lock, lock_path, read_holder


def test_sidecar_written_and_removed(tmp_path):
    db = tmp_path / "t.db"
    with job_lock(db, "snapshot"):
        info = json.loads(holder_path(db).read_text())
        assert info["kind"] == "snapshot" and info["pid"] > 0 and info["started_at"]
        assert lock_path(db).exists()
    assert read_holder(db) is None


def test_contention_reports_holder(tmp_path):
    db = tmp_path / "t.db"
    with job_lock(db, "run"):
        # a different process must be refused; same-process is re-entrant by design
        code = (
            "import sys;from tcgwatch.joblock import job_lock,JobLockedError\n"
            "try:\n  job_lock(sys.argv[1],'check').__enter__()\nexcept JobLockedError as e:\n"
            "  print(e.holder['kind']);sys.exit(7)\n"
        )
        r = subprocess.run([sys.executable, "-c", code, str(db)], capture_output=True, text=True)
        assert r.returncode == 7 and r.stdout.strip() == "run"


def test_released_when_holder_process_dies(tmp_path):
    db = tmp_path / "t.db"
    code = (
        "import sys,os,time;from tcgwatch.joblock import job_lock\n"
        "with job_lock(sys.argv[1],'run'):\n print('go',flush=True);time.sleep(sys.argv[2]=='live' and 30 or 0);os._exit(0)"
    )
    # alive child: acquisition refused
    live = subprocess.Popen([sys.executable, "-c", code, str(db), "live"], stdout=subprocess.PIPE, text=True)
    try:
        assert live.stdout.readline().strip() == "go"
        with pytest.raises(JobLockedError):
            with job_lock(db, "check"):
                pass
    finally:
        live.kill()
        live.wait()
        live.stdout.close()
    # abrupt exit inside `with` leaves the stale sidecar; it must not block
    dead = subprocess.run([sys.executable, "-c", code, str(db), "dead"], check=True, stdout=subprocess.PIPE, text=True)
    assert holder_path(db).exists() and read_holder(db)["kind"] == "run"
    with job_lock(db, "check"):
        assert read_holder(db)["kind"] == "check"


def test_symlink_alias_shares_lock(tmp_path):
    real = tmp_path / "real.db"
    alias = tmp_path / "alias.db"
    try:
        alias.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported")
    assert lock_path(alias) == lock_path(real)
    with job_lock(real, "run"):
        code = (
            "import sys;from tcgwatch.joblock import job_lock,JobLockedError\n"
            "try:\n  job_lock(sys.argv[1],'check').__enter__()\nexcept JobLockedError as e:\n"
            "  print(e.holder['kind']);sys.exit(7)\n"
        )
        r = subprocess.run([sys.executable, "-c", code, str(alias)], capture_output=True, text=True)
        assert r.returncode == 7 and r.stdout.strip() == "run"


def test_other_thread_waits_until_owner_exits(tmp_path):
    db = tmp_path / "t.db"
    a_in, b_try, a_out = threading.Event(), threading.Event(), threading.Event()
    result = {}

    def a():
        with job_lock(db, "run"):
            a_in.set()
            b_try.wait(5)
            a_out.set()  # set while still holding: whoever gets the lock next must see it

    def b():
        a_in.wait(5)
        try:
            with job_lock(db, "check"):
                pass
        except JobLockedError:
            result["busy"] = True
        b_try.set()
        with job_lock(db, "check", wait=10):
            result["a_done_before_b"] = a_out.is_set()
            result["holder"] = read_holder(db)["kind"]

    ta, tb = threading.Thread(target=a), threading.Thread(target=b)
    ta.start(); tb.start(); ta.join(15); tb.join(15)
    assert result == {"busy": True, "a_done_before_b": True, "holder": "check"}
    assert read_holder(db) is None


def test_reentrant_in_process(tmp_path):
    db = tmp_path / "t.db"
    with job_lock(db, "run"):
        with job_lock(db, "snapshot"):
            assert read_holder(db)["kind"] == "run"
        assert lock_path(db).exists()
    assert read_holder(db) is None


def test_wait_acquires_after_release(tmp_path):
    db = tmp_path / "t.db"
    ready = threading.Event()
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time;from tcgwatch.joblock import job_lock\n"
         "with job_lock(sys.argv[1],'run'):\n print('go',flush=True);time.sleep(1)", str(db)],
        stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "go"
    with pytest.raises(JobLockedError):
        with job_lock(db, "check"):
            pass
    start = time.monotonic()
    with job_lock(db, "check", wait=10):
        assert time.monotonic() - start < 9
    proc.wait()
    proc.stdout.close()


def test_memory_db_is_noop():
    with job_lock(":memory:", "run"):
        pass


def test_cli_busy_exit_code(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    db = tmp_path / "t.db"
    cfg.write_text(f"database: {db}\n")
    code = ("import sys,time;from tcgwatch.joblock import job_lock\n"
            "with job_lock(sys.argv[1],'run'):\n print('go',flush=True);time.sleep(5)")
    proc = subprocess.Popen([sys.executable, "-c", code, str(db)], stdout=subprocess.PIPE, text=True)
    try:
        proc.stdout.readline()
        assert cli.main(["--config", str(cfg), "check", "--preview"]) == 3
        assert "busy" in capsys.readouterr().err
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()

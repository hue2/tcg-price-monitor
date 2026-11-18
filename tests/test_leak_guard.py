"""Regression for the PLAN-4 leak guard (pyproject filterwarnings).

GC-time ResourceWarnings are re-raised by pytest as PytestUnraisableExceptionWarning. The config must turn
that into a failure, otherwise a leaked connection only prints a warning. Each check runs a child pytest with
the repo's own pyproject.toml so the check exercises the real config, not a copy of it.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEAK = 'import gc\nimport sqlite3\n\n\ndef test_leak():\n    sqlite3.connect(":memory:")\n    gc.collect()\n'
CLEAN = 'import sqlite3\n\n\ndef test_clean():\n    conn = sqlite3.connect(":memory:")\n    conn.close()\n'


def _run_child(tmp_path: Path, name: str, source: str) -> subprocess.CompletedProcess:
    (tmp_path / name).write_text(source)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(ROOT / "pyproject.toml"), "--rootdir", str(tmp_path),
         "-p", "no:cacheprovider", "--no-cov", "-q", str(tmp_path / name)],
        capture_output=True, text=True, timeout=120,
    )


def test_planted_unclosed_connection_fails_the_run(tmp_path):
    result = _run_child(tmp_path, "test_leak_child.py", LEAK)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "PytestUnraisableExceptionWarning" in result.stdout + result.stderr


def test_closed_connection_passes_the_run(tmp_path):
    result = _run_child(tmp_path, "test_clean_child.py", CLEAN)
    assert result.returncode == 0, result.stdout + result.stderr

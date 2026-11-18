"""scripts/release-check.sh on a throwaway git fixture (stub python/node/docker-smoke, no network, no docker)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release-check.sh"


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "release-check.sh")
    (tmp_path / "tests" / "js").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (tmp_path / "src" / "tcgwatch").mkdir(parents=True)
    (tmp_path / "src" / "tcgwatch" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (tmp_path / "notes.txt").write_text("history: shipped 0.1.0\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "BACKLOG.md").write_text("old release 0.1.0\n")
    (tmp_path / "tests" / "js" / "a.cjs").write_text("")
    for name, code in (("fakepy", 0), ("fakenode", 0), ("failnode", 1)):
        p = tmp_path / name
        p.write_text(f"#!/bin/sh\nexit {code}\n")
        p.chmod(0o755)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    # release commit: bump version, drop the leftover
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n')
    (tmp_path / "src" / "tcgwatch" / "__init__.py").write_text('__version__ = "0.2.0"\n')
    (tmp_path / "docs" / "BACKLOG.md").write_text(f"shipped `{base[:7]}`\n")
    git(tmp_path, "commit", "-qam", "release")
    return tmp_path, base


def run(repo, version, base, **env):
    e = {**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, "PYTHON": str(repo / "fakepy"), "NODE": str(repo / "fakenode"), "RELEASE_CHECK_NO_DOCKER": "1", **env}
    return subprocess.run([str(repo / "scripts" / "release-check.sh"), version, base], cwd=repo, env=e,
                          capture_output=True, text=True)


def test_passes_on_clean_release(repo):
    r, base = repo
    p = run(r, "0.2.0", base)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "PASS" in p.stdout and "checked 1 sha" in p.stdout


def test_leftover_old_version_fails(repo):
    r, base = repo
    (r / "src" / "tcgwatch" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    git(r, "commit", "-qam", "oops")
    p = run(r, "0.2.0", base)
    assert p.returncode == 1 and "leftover old version 0.1.0" in p.stdout and "__init__.py" in p.stdout


def test_history_line_with_old_version_passes(repo):
    r, base = repo
    (r / "notes.txt").write_text("- v0.1 (shipped, 0.1.0): history\n")
    git(r, "commit", "-qam", "history")
    p = run(r, "0.2.0", base)
    assert p.returncode == 0, p.stdout + p.stderr


def test_stale_user_agent_fails(repo):
    r, base = repo
    (r / "config.example.yaml").write_text('user_agent: "x/0.1.0"\n')
    git(r, "add", "config.example.yaml")
    git(r, "commit", "-qm", "ua")
    p = run(r, "0.2.0", base)
    assert p.returncode == 1 and "config.example.yaml" in p.stdout


def test_wrong_pyproject_version_fails(repo):
    r, base = repo
    assert run(r, "0.3.0", base).returncode == 1


def test_missing_and_non_ancestor_backlog_sha_fail(repo):
    r, base = repo
    git(r, "checkout", "-q", "-b", "side", base)
    (r / "x").write_text("x")
    git(r, "add", "x")
    git(r, "commit", "-qm", "side")
    side = git(r, "rev-parse", "--short", "HEAD")
    git(r, "checkout", "-q", "-")
    (r / "docs" / "BACKLOG.md").write_text(f"`{side}` `deadbee`\n")
    git(r, "commit", "-qam", "bad shas")
    p = run(r, "0.2.0", base)
    assert p.returncode == 1
    assert f"{side} is not an ancestor" in p.stdout and "deadbee does not exist" in p.stdout


def test_js_failure_and_pytest_failure_fail(repo):
    r, base = repo
    assert run(r, "0.2.0", base, NODE=str(r / "failnode")).returncode == 1
    assert run(r, "0.2.0", base, PYTHON=str(r / "failnode")).returncode == 1


def test_bad_args_and_unknown_ref(repo):
    r, base = repo
    assert subprocess.run([str(r / "scripts" / "release-check.sh")], cwd=r).returncode == 2
    assert run(r, "0.2.0", "nope").returncode == 2


def _side_commit(r, base):
    git(r, "checkout", "-q", "-b", "side", base)
    (r / "x").write_text("x")
    git(r, "add", "x")
    git(r, "commit", "-qm", "side")
    side = git(r, "rev-parse", "--short", "HEAD")
    git(r, "checkout", "-q", "-")
    return side


def test_missing_or_directory_backlog_fails(repo):
    r, base = repo
    git(r, "rm", "-q", "docs/BACKLOG.md")
    git(r, "commit", "-qm", "rm backlog")
    p = run(r, "0.2.0", base)
    assert p.returncode == 1 and "BACKLOG.md is missing" in p.stdout and "PASS" not in p.stdout
    (r / "docs" / "BACKLOG.md").mkdir(parents=True)
    assert run(r, "0.2.0", base).returncode == 1


def test_git_grep_error_fails(repo, tmp_path_factory):
    r, base = repo
    wrap = tmp_path_factory.mktemp("bin")
    real = shutil.which("git")
    (wrap / "git").write_text(f'#!/bin/sh\n[ "$1" = grep ] && {{ echo fatal >&2; exit 2; }}\nexec {real} "$@"\n')
    (wrap / "git").chmod(0o755)
    p = run(r, "0.2.0", base, PATH=f"{wrap}:{os.environ['PATH']}")
    assert p.returncode == 1 and "git grep" in p.stdout and "PASS" not in p.stdout


def test_tip_exemption_is_occurrence_local(repo):
    r, base = repo
    side = _side_commit(r, base)
    (r / "docs" / "BACKLOG.md").write_text(f"Shipped feature (`{side}`)\nSuperseded (tip `{side}`)\n")
    git(r, "commit", "-qam", "mixed")
    p = run(r, "0.2.0", base)
    assert p.returncode == 1 and f"{side} is not an ancestor" in p.stdout
    # tip alone is still fine
    (r / "docs" / "BACKLOG.md").write_text(f"Superseded (tip `{side}`)\n")
    git(r, "commit", "-qam", "tip only")
    assert run(r, "0.2.0", base).returncode == 0


def test_malformed_tip_is_not_exempt(repo):
    r, base = repo
    side = _side_commit(r, base)
    (r / "docs" / "BACKLOG.md").write_text(f"(tip `{side}` not superseded, shipped reference\n")
    git(r, "commit", "-qam", "unclosed")
    p = run(r, "0.2.0", base)
    assert p.returncode == 1 and f"{side} is not an ancestor" in p.stdout


def _smoke(repo, body):
    p = repo / "fakesmoke"
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(p)


def _docker_run(repo, base, smoke):
    return run(repo, "0.2.0", base, RELEASE_CHECK_NO_DOCKER="", DOCKER_SMOKE=smoke)


def test_docker_gate_pass(repo):
    r, base = repo
    p = _docker_run(r, base, _smoke(r, "echo built; echo 'docker-smoke OK'; exit 0"))
    assert p.returncode == 0 and "docker gate: ok" in p.stdout and "PASS" in p.stdout, p.stdout
    assert "DOCKER_SMOKE OVERRIDE" in p.stdout and str(r / "fakesmoke") in p.stdout


def test_docker_gate_fail(repo):
    r, base = repo
    p = _docker_run(r, base, _smoke(r, "echo FAIL: boom; exit 1"))
    assert p.returncode == 1 and "docker-smoke.sh failed" in p.stdout and "FAILED" in p.stdout


def test_docker_gate_skip_is_fail(repo):
    r, base = repo
    p = _docker_run(r, base, _smoke(r, "echo 'SKIP: docker compose not available'; exit 0"))
    assert p.returncode == 1 and "SKIPped" in p.stdout and "FAILED" in p.stdout


def test_docker_gate_missing_script_fails(repo):
    r, base = repo
    p = _docker_run(r, base, str(r / "nope"))
    assert p.returncode == 1 and "missing or not executable" in p.stdout


def test_docker_gate_default_on_and_opt_out(repo):
    r, base = repo
    p = run(r, "0.2.0", base, RELEASE_CHECK_NO_DOCKER="")  # default path: fixture has no docker-smoke.sh
    assert p.returncode == 1 and "docker gate:" in p.stdout
    p = run(r, "0.2.0", base)  # opt-out set by run()
    assert p.returncode == 0 and "docker gate skipped" in p.stdout


def test_docker_gate_default_smoke_prints_no_override(repo):
    r, base = repo
    p = run(r, "0.2.0", base, RELEASE_CHECK_NO_DOCKER="")
    assert "DOCKER_SMOKE OVERRIDE" not in p.stdout


@pytest.mark.parametrize("body", [
    "echo built; exit 0",                              # no success marker
    "echo '  SKIP: no docker'; exit 0",                # indented skip
    "echo 'skip: no docker'; exit 0",                  # lowercase skip
    "echo 'docker-smoke OK (partial)'; exit 0",        # marker must be the whole line
])
def test_docker_gate_requires_exact_marker(repo, body):
    r, base = repo
    p = _docker_run(r, base, _smoke(r, body))
    assert p.returncode == 1 and "FAILED" in p.stdout, p.stdout


def _fake_tool(repo, name, body):
    d = repo / "fakebin"
    d.mkdir(exist_ok=True)
    t = d / name
    t.write_text("#!/bin/sh\n" + body + "\n")
    t.chmod(0o755)
    return str(d) + os.pathsep + os.environ["PATH"]


def test_docker_gate_tee_failure_fails_closed(repo):
    r, base = repo
    path = _fake_tool(r, "tee", "cat >/dev/null; exit 1")  # ENOSPC-style: cannot save the stream
    p = run(r, "0.2.0", base, RELEASE_CHECK_NO_DOCKER="", PATH=path,
            DOCKER_SMOKE=_smoke(r, "echo 'docker-smoke OK'; exit 0"))
    assert p.returncode == 1 and "could not save" in p.stdout and "PASS" not in p.stdout, p.stdout


def test_docker_gate_grep_error_fails_closed(repo):
    r, base = repo
    path = _fake_tool(r, "grep", "exit 2")  # read error, not "no match"
    p = run(r, "0.2.0", base, RELEASE_CHECK_NO_DOCKER="", PATH=path,
            DOCKER_SMOKE=_smoke(r, "echo 'docker-smoke OK'; exit 0"))
    assert p.returncode == 1 and "could not inspect" in p.stdout and "PASS" not in p.stdout, p.stdout

"""Static checks of Dockerfile + docker-compose.yml (no docker needed; see scripts/smoke-install.sh)."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from tcgwatch.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def _instructions() -> list[tuple[str, str]]:
    text = re.sub(r"\\\n", " ", DOCKERFILE)
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            kw, _, rest = line.partition(" ")
            out.append((kw.upper(), rest.strip()))
    return out


def _subcommands() -> set[str]:
    parser = build_parser()
    sub = next(a for a in parser._actions if a.choices and hasattr(a, "_name_parser_map"))
    return set(sub.choices)


def test_dockerfile_final_user_is_non_root():
    ins = _instructions()
    users = [r for k, r in ins if k == "USER"]
    assert users, "no USER instruction"
    assert users[-1].split(":")[0] not in ("root", "0")
    kws = [k for k, _ in ins]
    assert kws.index("USER") > max(i for i, k in enumerate(kws) if k == "RUN")


def test_dockerfile_entrypoint_and_cmd_are_real_subcommands():
    ins = dict(_instructions())
    assert yaml.safe_load(ins["ENTRYPOINT"]) == ["tcgwatch"]
    assert yaml.safe_load(ins["CMD"])[0] in _subcommands()


def test_dockerfile_healthcheck_runs_real_subcommand():
    hc = next(r for k, r in _instructions() if k == "HEALTHCHECK")
    cmd = yaml.safe_load(hc.split("CMD", 1)[1])
    assert cmd[0] == "tcgwatch" and cmd[1] == "web-health", hc
    assert cmd[1] in _subcommands()
    port = cmd[cmd.index("--port") + 1]
    assert f"EXPOSE {port}" in DOCKERFILE


def test_compose_commands_are_real_subcommands():
    subs = _subcommands()
    for name, svc in COMPOSE["services"].items():
        assert svc["command"][0] in subs, name


def test_compose_ports_loopback_only():
    for svc in COMPOSE["services"].values():
        for p in svc.get("ports", []):
            assert str(p).startswith("127.0.0.1:"), p
    assert COMPOSE["services"]["web"]["ports"], "web must publish a port"


def test_compose_web_port_matches_dockerfile_and_command():
    cmd = COMPOSE["services"]["web"]["command"]
    port = cmd[cmd.index("--port") + 1]
    assert COMPOSE["services"]["web"]["ports"][0].endswith(f":{port}:{port}")
    assert f"EXPOSE {port}" in DOCKERFILE


def test_compose_scheduler_healthcheck_is_real_subcommand():
    test = COMPOSE["services"]["scheduler"]["healthcheck"]["test"]
    assert test[0] == "CMD" and test[1] == "tcgwatch" and test[2] == "health" and test[2] in _subcommands()


def test_smoke_install_refuses_occupied_port():
    """An unrelated HTTP listener on SMOKE_PORT must never yield smoke success (R-DOCK-1)."""
    import http.server, subprocess, threading
    srv = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "scripts" / "smoke-install.sh")],
                           env={**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, "SMOKE_PORT": str(srv.server_port)},
                           capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
        srv.server_close()
    assert r.returncode != 0
    assert "already in use" in r.stdout
    assert "smoke-install OK" not in r.stdout


def test_docker_smoke_script_has_skip_guard():
    import subprocess
    script = Path(__file__).resolve().parents[1] / "scripts" / "docker-smoke.sh"
    text = script.read_text()
    assert os.access(script, os.X_OK)
    assert "command -v docker" in text and "SKIP" in text and "exit 0" in text
    assert "web-health" in text and "health" in text and "compose down" in text
    # Behavioural: with docker absent from PATH it must skip with rc 0.
    r = subprocess.run(["/bin/bash", str(script)], env={"PATH": "/nonexistent"},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "SKIP" in r.stdout


# --- scripts/docker-smoke.sh against a fake `docker` (R-DOCK-2): isolation, perms, strict health ---

FAKE_DOCKER = r'''#!/usr/bin/env bash
log="$FAKE_LOG"
echo "$*" >> "$log"
args=" $* "
case "$args" in
  *" compose version "*) exit 0;;
  *" config "*) exit 0;;
  *" up "*)
    f=$(sed -n 's/.*-f \(\S*override.yml\).*/\1/p' <<<"$*" | head -1)
    d=$(dirname "$f")
    { echo "OVERRIDE=$f"; stat -c 'MODE=%a UID=%u' "$d" "$d/data" "$d/data/config.yaml"; } >> "$FAKE_LOG.up"
    cp "$f" "$FAKE_LOG.override"; exit 0;;
  *" exec "*"web-health"*) exit 0;;
  *" exec "*"--help"*) exit "${FAKE_EXEC_RC:-0}";;
  *" exec "*" health"*) echo "${FAKE_HEALTH_ERR-unhealthy: no database at /data/tcgwatch.db}" >&2; exit "${FAKE_HEALTH_RC:-1}";;
esac
exit 0
'''


def _run_smoke(tmp_path, **env):
    import shutil
    import subprocess
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "docker-smoke.sh", repo / "scripts" / "docker-smoke.sh")
    shutil.copy(ROOT / "docker-compose.yml", repo / "docker-compose.yml")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    e = {"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_LOG": str(tmp_path / "calls"), "TMPDIR": str(tmpdir), **env}
    r = subprocess.run(["bash", str(repo / "scripts" / "docker-smoke.sh")], env=e, cwd=repo,
                       capture_output=True, text=True, timeout=60)
    return r, repo, tmpdir


def test_smoke_uses_private_fixture_not_checkout_data(tmp_path):
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    os.symlink("../user-config", repo / "data" / "config.yaml")     # dangling, pre-existing
    (repo / "data" / "tcgwatch.db").write_bytes(b"real")
    r, repo, tmpdir = _run_smoke(tmp_path)
    assert r.returncode == 0 and "docker-smoke OK" in r.stdout, r.stdout + r.stderr
    assert os.readlink(repo / "data" / "config.yaml") == "../user-config"
    assert not (tmp_path / "user-config").exists()
    assert (repo / "data" / "tcgwatch.db").read_bytes() == b"real"
    assert list(tmpdir.iterdir()) == []                              # fixture removed on exit
    class _L(yaml.SafeLoader):
        pass
    _L.add_multi_constructor("!", lambda loader, suffix, node: None)       # compose's !reset
    override = yaml.load((tmp_path / "calls.override").read_text(), Loader=_L)
    for svc in ("web", "scheduler"):
        vols = override["services"][svc]["volumes"]
        assert len(vols) == 1 and vols[0].endswith("/data:/data") and str(repo) not in vols[0]
        assert override["services"][svc]["user"] == f"{os.getuid()}:{os.getgid()}"
        assert override["services"][svc]["environment"]["TCGWATCH_DISCORD_WEBHOOK"] == ""
    assert override["services"]["scheduler"]["entrypoint"] == ["sleep", "infinity"]   # prod scheduler never runs
    assert "-f " + str(repo / "docker-compose.yml") in (tmp_path / "calls").read_text()


def test_smoke_fixture_owned_and_private(tmp_path):
    r, _, _ = _run_smoke(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = (tmp_path / "calls.up").read_text().splitlines()
    stats = [ln for ln in lines if ln.startswith("MODE=")]
    assert len(stats) == 3 and all(f"UID={os.getuid()}" in s for s in stats)   # invoker-owned, runs as that uid
    assert [s.split()[0] for s in stats] == ["MODE=700", "MODE=700", "MODE=600"]


def test_smoke_fails_when_exec_fails(tmp_path):
    for rc in ("126", "127", "1"):
        r, _, _ = _run_smoke(tmp_path / rc, FAKE_EXEC_RC=rc, FAKE_HEALTH_RC=rc, FAKE_HEALTH_ERR="")
        assert r.returncode != 0 and "docker-smoke OK" not in r.stdout, (rc, r.stdout)


def test_smoke_requires_exact_empty_db_health(tmp_path):
    cases = [{"FAKE_HEALTH_RC": "126"}, {"FAKE_HEALTH_RC": "127"}, {"FAKE_HEALTH_RC": "0"},
             {"FAKE_HEALTH_RC": "2"}, {"FAKE_HEALTH_ERR": "something else broke"},
             {"FAKE_HEALTH_ERR": ""}]
    for i, env in enumerate(cases):
        r, _, _ = _run_smoke(tmp_path / str(i), **env)
        assert r.returncode != 0 and "docker-smoke OK" not in r.stdout, (env, r.stdout)
    r, _, _ = _run_smoke(tmp_path / "ok", FAKE_HEALTH_ERR="unhealthy: no ok schedule run recorded")
    assert r.returncode == 0 and "docker-smoke OK" in r.stdout

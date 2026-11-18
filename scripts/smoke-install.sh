#!/usr/bin/env bash
# Docker-free install check: clean venv, `pip install .`, start the web app on a temp config,
# curl /api/status, run `tcgwatch web-health`, and confirm `tcgwatch health` fails on an empty DB. Run from anywhere.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
pid=""
cleanup() { [ -n "$pid" ] && { kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; } || true; rm -rf "$tmp"; }
trap cleanup EXIT

# Port: pick a free one, or fail if the caller's SMOKE_PORT is already taken (never trust a foreign listener).
port_free() { python3 -c 'import socket,sys; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1])))' "$1" 2>/dev/null; }
if [ -n "${SMOKE_PORT:-}" ]; then
  port="$SMOKE_PORT"
  port_free "$port" || { echo "FAIL: port $port is already in use"; exit 1; }
else
  port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
fi

python3 -m venv "$tmp/venv"
"$tmp/venv/bin/pip" install --quiet "$repo"
tw="$tmp/venv/bin/tcgwatch"

printf 'database: tcgwatch.db\n' > "$tmp/config.yaml"
export TCGWATCH_CONFIG="$tmp/config.yaml"
unset TCGWATCH_WEB_TOKEN  # loopback bind needs no token (AUTH-0)

"$tw" web --host 127.0.0.1 --port "$port" > "$tmp/web.log" 2>&1 &
pid=$!
# Ready only when /api/status comes from THIS server: its config_path must be our unique temp config.
ours() { python3 - "$tmp/status.json" "$tmp/config.yaml" <<'PY'
import json, os, sys
try:
    got = json.load(open(sys.argv[1]))["config_path"]
except Exception:
    sys.exit(1)
sys.exit(0 if os.path.realpath(got) == os.path.realpath(sys.argv[2]) else 1)
PY
}
ready=""
for _ in $(seq 1 50); do
  kill -0 "$pid" 2>/dev/null || { cat "$tmp/web.log"; echo "FAIL: web exited"; exit 1; }
  if curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$port/api/status" -o "$tmp/status.json" 2>/dev/null; then
    ours && { ready=1; break; }
    cat "$tmp/status.json"; echo "FAIL: port $port answered, but not as this run's tcgwatch (config_path mismatch)"; exit 1
  fi
  sleep 0.2
done
[ -n "$ready" ] || { cat "$tmp/web.log"; echo "FAIL: /api/status not reachable"; exit 1; }
echo "status: $(cat "$tmp/status.json")"
"$tw" web-health --port "$port" || { echo "FAIL: web-health"; exit 1; }
echo "web-health: ok"

if "$tw" health; then echo "FAIL: health returned 0 on an empty DB"; exit 1; fi
echo "health: nonzero on empty DB (expected)"
echo "smoke-install OK"

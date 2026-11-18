#!/usr/bin/env bash
# Docker image smoke: build, compose up against a PRIVATE temporary fixture, poll `tcgwatch web-health`
# in the web container, assert `tcgwatch health` on the empty fixture DB, then compose down.
# Never touches the checkout's data/ (real config, DB, symlinks) and never runs the production
# scheduler: a generated compose override binds a mktemp dir at /data, runs the containers as the
# invoking uid:gid (so no root/chown is needed), blanks notification credentials, drops host ports and
# replaces the scheduler with an idle container (health is exec'd against it, no jobs run).
# TCGWATCH_SMOKE_IMAGE=<ref> smokes that already-built local image (both services, no build, never pulled)
# instead of building tcgwatch:local; CI uses it so the exact image that is published is the one tested.
# Skips with exit 0 when docker (or the compose plugin) is missing. Uses a per-run project name.
set -euo pipefail
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "SKIP: docker compose not available on this host; image not built (use scripts/smoke-install.sh)"
  exit 0
fi
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

fx="$(mktemp -d "${TMPDIR:-/tmp}/tcgwatch-smoke.XXXXXX")"   # mode 0700, owned by the invoker
chmod 700 "$fx"
export COMPOSE_PROJECT_NAME="tcgwatch-smoke-$$"
dc=(docker compose -f "$repo/docker-compose.yml" -f "$fx/override.yml")
cleanup() {
  "${dc[@]}" down -v --remove-orphans >/dev/null 2>&1 || echo "WARN: compose down failed; check project $COMPOSE_PROJECT_NAME" >&2
  rm -rf "$fx"
}
trap cleanup EXIT

# Host credentials never reach the containers (override blanks them too).
unset TCGWATCH_WEB_TOKEN TCGWATCH_DISCORD_WEBHOOK
mkdir "$fx/data"
printf 'database: tcgwatch.db\n' > "$fx/data/config.yaml"
chmod 700 "$fx/data"; chmod 600 "$fx/data/config.yaml"
img_web=""; img_sched=""
if [ -n "${TCGWATCH_SMOKE_IMAGE:-}" ]; then
  if ! docker image inspect "$TCGWATCH_SMOKE_IMAGE" >/dev/null 2>&1; then
    echo "FAIL: TCGWATCH_SMOKE_IMAGE=$TCGWATCH_SMOKE_IMAGE is not a local image (load or build it first)"; exit 1
  fi
  img_web="    image: \"$TCGWATCH_SMOKE_IMAGE\"
    pull_policy: never
    build: !reset null"
  img_sched="$img_web"
  echo "smoke image: $TCGWATCH_SMOKE_IMAGE ($(docker image inspect --format '{{.Id}}' "$TCGWATCH_SMOKE_IMAGE"))"
fi
cat > "$fx/override.yml" <<YML
services:
  web:
${img_web:+$img_web
}    user: "$(id -u):$(id -g)"
    volumes:
      - $fx/data:/data
    environment:
      TCGWATCH_DISCORD_WEBHOOK: ""
      TCGWATCH_WEB_TOKEN: ""
    ports: !reset []
    restart: "no"
  scheduler:
${img_sched:+$img_sched
}    user: "$(id -u):$(id -g)"
    volumes:
      - $fx/data:/data
    environment:
      TCGWATCH_DISCORD_WEBHOOK: ""
    entrypoint: ["sleep", "infinity"]
    command: []
    healthcheck:
      disable: true
    restart: "no"
YML

"${dc[@]}" config -q || { echo "FAIL: compose config invalid (override needs compose >= 2.24 for !reset)"; exit 1; }
if [ -n "${TCGWATCH_SMOKE_IMAGE:-}" ]; then
  "${dc[@]}" up -d --no-build
else
  "${dc[@]}" build
  "${dc[@]}" up -d
fi

ok=""
for _ in $(seq 1 30); do
  if "${dc[@]}" exec -T web tcgwatch web-health >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [ -z "$ok" ]; then "${dc[@]}" logs --tail=50; echo "FAIL: web-health never passed"; exit 1; fi
echo "web-health: ok"

# The scheduler container must execute commands at all (catches 126/127, stopped service, engine errors)...
if ! "${dc[@]}" exec -T scheduler tcgwatch --help >/dev/null 2>&1; then
  echo "FAIL: cannot exec tcgwatch in the scheduler container"; exit 1
fi
# ...then health on the idle, never-run fixture must be exactly the documented "unhealthy" (exit 1 plus
# a structured "unhealthy: no database ..." / "unhealthy: no ok schedule run ..." diagnostic).
rc=0
err="$("${dc[@]}" exec -T scheduler tcgwatch health 2>&1 >/dev/null)" || rc=$?
if [ "$rc" -ne 1 ] || ! printf '%s' "$err" | grep -Eq '^unhealthy: (no database at |no ok schedule run recorded)'; then
  echo "FAIL: expected health exit 1 with 'unhealthy: no database|no ok schedule run' on an empty fixture; got rc=$rc: $err"; exit 1
fi
echo "health: exit 1, $err (expected on an empty DB)"
echo "docker-smoke OK"

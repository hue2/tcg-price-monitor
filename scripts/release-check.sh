#!/usr/bin/env bash
# Release-review recipe (TOOL-1). Usage: scripts/release-check.sh <new-version> <base-ref>
#   1. old-version leftovers: the version in <base-ref>'s pyproject.toml must not appear in the
#      version-carrying files (VERSION_FILES below); prose and history (SPEC, BACKLOG, README) may keep
#      old versions. pyproject.toml must say <new-version>.
#      Skipped when the old and new versions are equal (re-checking an already released tree).
#   2. every backticked SHA in docs/BACKLOG.md exists and is an ancestor of HEAD ("(tip `sha`)" superseded
#      branch tips need only exist).
#   3. pytest --cov (gate from pyproject.toml), then every tests/js/*.cjs by glob.
#   4. docker gate: runs scripts/docker-smoke.sh (builds the image, ~1 min). Success requires exit 0 AND the exact
#      final marker line 'docker-smoke OK'; non-zero, SKIP, a missing marker, or a tee/grep failure is FAIL.
#      A non-default DOCKER_SMOKE prints a prominent override notice with its path. Opt out with RELEASE_CHECK_NO_DOCKER=1 (hosts without
#      docker, the release-check tests); the gate then prints a visible 'docker gate skipped' line.
# Exits non-zero on any failure. No network except the docker build. Run from anywhere inside the repo.
# Env: PYTHON (default python3), NODE (default node), PYTEST_ARGS (extra pytest args),
#      RELEASE_CHECK_NO_DOCKER=1 (skip the docker gate), DOCKER_SMOKE (script path, default
#      scripts/docker-smoke.sh; a test seam).
set -uo pipefail

[ $# -eq 2 ] || { echo "usage: $0 <new-version> <base-ref>" >&2; exit 2; }
new="$1"; base="$2"
repo="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)" || exit 2
cd "$repo" || exit 2
py="${PYTHON:-python3}"; node="${NODE:-node}"
fail=0
bad() { echo "FAIL: $*"; fail=1; }

git rev-parse --verify --quiet "$base^{commit}" >/dev/null || { echo "FAIL: unknown base ref: $base" >&2; exit 2; }

# 1. version strings. Positive list: files that carry the live version (__version__, default and example
#    user_agent, the version test). Add a file here when it gains a version string.
VERSION_FILES=(pyproject.toml src/tcgwatch/__init__.py src/tcgwatch/config.py config.example.yaml tests/test_version.py)
ver_of() { sed -n 's/^version *= *"\([^"]*\)".*/\1/p' | head -n 1; }
old="$(git show "$base:pyproject.toml" | ver_of)"
cur="$(ver_of < pyproject.toml)"
[ -n "$old" ] || { echo "FAIL: no version in $base:pyproject.toml" >&2; exit 2; }
[ "$cur" = "$new" ] || bad "pyproject.toml version is '$cur', expected '$new'"
if [ "$old" = "$new" ]; then
  echo "version: base and new are both $new, leftover check skipped"
else
  hits="$(git grep -n -F -e "$old" -- "${VERSION_FILES[@]}")"; rc=$?
  if [ "$rc" -gt 1 ]; then bad "git grep for old version $old failed (status $rc)"
  elif [ "$rc" -eq 0 ]; then bad "leftover old version $old:"; echo "$hits"
  else echo "version: no leftover $old"; fi
fi

# 2. BACKLOG SHAs. Every one must exist; each must also be an ancestor of HEAD, except exact
#    "(tip `sha`)" occurrences (deliberately unmerged, superseded branch tips: existence only). The
#    exemption is occurrence-local and needs the closing parenthesis; any other backticked sha, even
#    the same one, is checked as an ordinary reference.
n=0
backlog=docs/BACKLOG.md
tip_re='\(tip `[0-9a-f]{7,40}`\)'
# extract <regex> <text>: sets $found to the matches. grep status 1 = none (fine), >1 = error (fail).
# Runs in the current shell (no command substitution) so bad() sticks.
extract() {
  found="$(grep -oE "$1" <<<"$2")"; local rc=$?
  if [ "$rc" -gt 1 ]; then bad "grep failed on $backlog (status $rc)"; found=""; fi
}
if [ ! -f "$backlog" ] || [ ! -r "$backlog" ]; then
  bad "$backlog is missing, not a regular file, or unreadable"
else
  text="$(cat "$backlog")" || bad "cannot read $backlog"
  extract "$tip_re" "$text"
  tips="$(grep -oE '[0-9a-f]{7,40}' <<<"$found" | sort -u)"
  rest="$(sed -E "s/$tip_re//g" <<<"$text")" || bad "sed failed on $backlog"
  extract '`[0-9a-f]{7,40}`' "$rest"
  ordinary="$(tr -d '`' <<<"$found" | sort -u)"
  while read -r sha; do
    [ -n "$sha" ] || continue
    n=$((n + 1))
    if ! git cat-file -e "$sha^{commit}" 2>/dev/null; then bad "BACKLOG sha $sha does not exist"
    elif ! git merge-base --is-ancestor "$sha" HEAD; then bad "BACKLOG sha $sha is not an ancestor of HEAD"; fi
  done <<<"$ordinary"
  while read -r sha; do
    [ -n "$sha" ] || continue
    n=$((n + 1))
    git cat-file -e "$sha^{commit}" 2>/dev/null || bad "BACKLOG tip sha $sha does not exist"
  done <<<"$tips"
fi
echo "backlog: checked $n sha(s)"

# 3. tests
# shellcheck disable=SC2086
"$py" -m pytest --cov ${PYTEST_ARGS:-} || bad "pytest failed"
shopt -s nullglob
for f in tests/js/*.cjs; do
  if "$node" "$f" "$repo"; then echo "ok: $f"; else bad "$f failed"; fi
done

# 4. docker gate (ship ON; the single opt-out is RELEASE_CHECK_NO_DOCKER=1)
if [ "${RELEASE_CHECK_NO_DOCKER:-}" = "1" ]; then
  echo "docker gate skipped (RELEASE_CHECK_NO_DOCKER=1): docker-smoke.sh not run"
else
  smoke="${DOCKER_SMOKE:-$repo/scripts/docker-smoke.sh}"
  smoke_out="$(mktemp "${TMPDIR:-/tmp}/release-check-docker.XXXXXX")" || { bad "cannot create temp file"; smoke_out=/dev/null; }
  default_smoke="$repo/scripts/docker-smoke.sh"
  if [ "$smoke" != "$default_smoke" ]; then
    echo "!!! DOCKER_SMOKE OVERRIDE: the docker gate runs $smoke instead of $default_smoke (test seam; not the real gate) !!!"
  fi
  if [ ! -x "$smoke" ]; then bad "docker gate: $smoke is missing or not executable"
  else
    "$smoke" 2>&1 | tee "$smoke_out"
    st=("${PIPESTATUS[@]}")
    if [ "${st[0]}" -ne 0 ]; then bad "docker gate: docker-smoke.sh failed"
    elif [ "${st[1]}" -ne 0 ]; then bad "docker gate: could not save docker-smoke.sh output (tee exit ${st[1]})"
    else
      grep -qx 'docker-smoke OK' "$smoke_out"; g=$?
      if [ "$g" -eq 0 ]; then echo "docker gate: ok"
      elif [ "$g" -gt 1 ]; then bad "docker gate: could not inspect docker-smoke.sh output (grep exit $g)"
      elif grep -q 'SKIP' "$smoke_out"; then bad "docker gate: docker-smoke.sh SKIPped (no docker); set RELEASE_CHECK_NO_DOCKER=1 to opt out"
      else bad "docker gate: docker-smoke.sh did not print its final 'docker-smoke OK' line"; fi
    fi
  fi
  [ "$smoke_out" = /dev/null ] || rm -f "$smoke_out"
fi

[ "$fail" -eq 0 ] && echo "release-check: PASS" || echo "release-check: FAILED"
exit "$fail"

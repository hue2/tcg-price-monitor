# tcg-price-watch

Flags trading-card singles whose price moved past a threshold over a time window. The
default rule is **+30% over 7 days, regular prints only**, for **One Piece TCG** and
**Riftbound**. Prices come from [TCGCSV](https://tcgcsv.com), a daily mirror of TCGplayer.

Full design: [docs/SPEC.md](docs/SPEC.md).

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
Copy-Item config.example.yaml config.yaml

tcgwatch backfill --days 7          # once: today's snapshot + the past week from archives
tcgwatch web                        # http://127.0.0.1:8765 - settings and the full card list
tcgwatch check                      # flag movers (CLI)
```

After that, run `tcgwatch run` once a day (see Scheduling) to add each day's prices.

## Web app

`tcgwatch web` serves a local-only app at http://127.0.0.1:8765:

- **Cards:** every tracked card with its % change over the window, sortable and
  searchable, with filters for set and rarity. Flagged cards are highlighted. You can try
  a different window or threshold without changing your saved settings. **Download CSV**
  saves every row matching the current search, set, rarity and show filters, in the current sort
  order (not just the visible page), as `cards.csv` (name, number, rarity, printing, set, base_date, base,
  cur, pct, flagged, url). It is built in the browser; there is no API call. Text cells starting with
  `=`, `+`, `-`, `@`, tab or CR get a leading `'` so spreadsheets don't run them as formulas; numeric
  columns are written as plain numbers.
- **Health and runs:** a Healthy/Unhealthy badge (from `/api/status` `health`, same check as `tcgwatch health`) sits above the status line; the Runs tab lists the last 20 job runs from `/api/job-runs`, failed ones highlighted. If the runs cannot be loaded, the tab clears the old rows and shows a notice with a retry (click the Runs tab again).
- **Alerts:** the Alerts tab lists recorded alerts from `/api/alerts`, newest first, filterable to posted-to-Discord or noise-suppressed and by span, with a link to each card. If it cannot load, the old rows are cleared and a notice explains (change a filter or click the tab to retry).
- **Expired token:** on a 401 the UI shows one banner linking to `/login` and stops sending further requests (reload after logging in). When a token is configured, `/api/status` reports `auth_enabled` and the header shows a Log out button (POST `/logout`).
- **Watchlist:** star a card to watch it. The Watchlist tab shows each watched card's
  latest price, change over a chosen span (7d to 1y) and a price-history sparkline. The
  chart button on any card row expands a larger history line. Stars are stored in the
  database, not in `config.yaml`.
- **Watchlist in `check`:** `tcgwatch check` always prints a Watchlist section (price and % change
  for every watched card, ignoring filters). Watched cards go to Discord only past the Discord threshold, with the usual cooldown.
- **Settings:** edit everything in `config.yaml`: thresholds, Discord, rarities, sets
  (pick per set), and add or remove games. Comments in the file are preserved.
- **Protection:** the app answers only to loopback Host names (`localhost`, `127.0.0.1`, `[::1]`) plus
  the `--host` value; any other Host gets 400 (DNS rebinding). POST/PUT/DELETE with a cross-origin
  `Origin`/`Referer` get 403 (CSRF). Binding to a non-loopback `--host` is **refused** unless
  `web.auth_token` (or env `TCGWATCH_WEB_TOKEN`) is set; then every request needs
  `Authorization: Bearer <token>` or the cookie set by `POST /login` (the form at `GET /login`, or a JSON/urlencoded body `token=...`; same-origin and Host enforced). The token is never accepted in a URL, so it cannot reach access logs, history or Referer; login responses send `Cache-Control: no-store` and `Referrer-Policy: no-referrer` (constant-time compare). `POST /logout` (same-origin, no token needed) clears that cookie and redirects to `/login`; Bearer clients are unaffected.
  `--insecure-no-auth` is the explicit opt-out for a port that is only published on loopback.
  In Docker (bind `0.0.0.0`, published on 127.0.0.1) the defaults work; for other names use
  `--allowed-host NAME` (repeatable) or `TCGWATCH_ALLOWED_HOSTS=a,b`.
- **Header buttons:** Snapshot now, Backfill 7 days, and Run check + Discord.

## Remote access

The web app has no per-user login and is meant for this machine. The default (v0.5) keeps it on loopback, and
the supported way to reach it from another device is a tunnel, so the app itself never listens on a
network address:

- **SSH tunnel (recommended).** From the device you are on, forward the local port to the machine
  that runs `tcgwatch web`:

  ```bash
  ssh -N -L 8765:127.0.0.1:8765 you@your-server
  ```

  Then open http://127.0.0.1:8765 on your device. Nothing else is exposed, and the Host/Origin
  guard still applies.
- **Tailscale.** Install Tailscale on both machines and reach the server over its tailnet address.
  Tailscale does not forward traffic to the loopback port, so you have to bind a tailnet address
  with `tcgwatch web --host <tailnet-ip>`. That is a non-loopback bind, so it is refused until a token
  is set. Set `TCGWATCH_WEB_TOKEN` (or `web.auth_token`) first, then pass the token as
  `Authorization: Bearer <token>` or open `/login` once in the browser and submit the token in the form (a POST with the token in the body; never in a URL). Add the name you
  use to `--allowed-host`. Use this only for a private tailnet: the token is a single shared secret
  and the app has no per-user accounts.
- **Do not** publish port 8765 to the internet or a LAN without a token and a TLS proxy in front. The
  app serves plain HTTP.

A full login system is not built. It is listed as deferred in `docs/SPEC.md` §11 and `docs/BACKLOG.md`.

## Adjusting the rules

All settings live in `config.yaml`. Set a global `alert:` block and override it per game.

| Want | Config | One-off CLI |
|------|--------|-------------|
| Different threshold | `alert.threshold_pct: 50` | `--threshold 50` |
| Different window | `alert.window: 14d` | `--window 2w` |
| Drops too, or only drops | `alert.direction: both` / `down` | `--direction both` |
| Re-flag a card sooner or later | `alert.cooldown: 3d` (default = the window) | — |
| Change the daily run time | `schedule.at: "20:30"` (UTC) | — |
| Include higher rarities | add e.g. `Epic`, `Showcase` to `rarities` and remove matching `exclude_name_patterns` | `--all-rarities` |
| Ignore cheap cards | `alert.min_price: 1.00` | `--min-price 1` |
| Only certain sets | `include_sets: [24344, 'Spiritforged']` per game | `--set 24344` |
| Skip certain sets | `exclude_sets: ['Promo']` per game | — |
| Discord only for big moves | `alert.discord_threshold_pct: 75` | `--discord-threshold 75` |

Use `tcgwatch check --preview ...` to try settings without recording alerts or posting to
Discord.

## Adding or editing a game

Add a block under `games:`. Any game TCGplayer lists works, with no code changes:

```yaml
games:
  lorcana:
    name: Disney Lorcana
    category_name: Lorcana TCG   # or category_id: <n>
    rarities: [Common, Uncommon, Rare, Super Rare]
    exclude_name_patterns: ['\(Enchanted\)']
```

To find rarity values and variant tags for a new game, see `/tcgplayer/{categoryId}/groups`
and `/products` on tcgcsv.com.

TCGCSV requests retry up to 4 attempts with exponential backoff (1s, 2s, 4s) on HTTP 429/5xx,
timeouts and network errors. Other 4xx (including the 404 for a missing archive) are not retried.
A `Retry-After` header on 429/503 (delta-seconds or HTTP-date) replaces the exponential wait for
that retry; it is clamped to 60s per wait, and if the total retry sleep for one request would pass
a 120s budget the request fails immediately instead of retrying early. Missing or invalid
values fall back to the exponential schedule (past dates retry immediately).

## Filtering by set

After one `snapshot`, list each game's sets with their IDs:

```powershell
tcgwatch sets --game riftbound
#  +  24344  Origins  (353 singles)
#  -  24343  Riftbound Promotional Cards  (13 singles)
```

Manage the watchlist from the shell (same validation as the web app; `add`/`remove` take the
job lock, so they exit 3 if a snapshot/check is running, or wait with `--lock-wait SECONDS`):

```powershell
tcgwatch watchlist list [--game riftbound]
tcgwatch watchlist add 12345 Normal      # exit 1 if that printing has no stored prices
tcgwatch watchlist remove 12345 Normal   # exit 1 if it wasn't watched
```

Read alert history (same filters and ranges as `GET /api/alerts`; read-only, never creates the database):

```powershell
tcgwatch alerts [--game riftbound] [--days 30] [--discord|--no-discord] [--limit 100]
```

Read job history (mirrors `GET /api/job-runs`; read-only, never creates the database; a missing database prints `no job runs yet`, exit 0):

```
tcgwatch runs [--kind snapshot] [--limit 50] [--json]
```

`--kind` is a lowercase slug, `--limit` is 1-500 (default 50). `--json` prints the rows as a JSON array identical to the API body (raw text, like the API); the table escapes stored text like the other commands.

All CLI and console output (`games`, `sets`, `watchlist list`, mover/watchlist tables, `alerts`, snapshot/backfill/check/schedule messages) escapes stored, upstream and config text: it is printed with control characters shown as `\xNN` escapes (and each literal backslash as `\\`, so escapes stay unambiguous), so a hostile card name cannot manipulate your terminal or forge table columns. Boundary: this covers the CLI's own prints, including config game keys and names (`games`, `sets`, `prune` and its notes), database/backup/config paths and the explicit `error:`/`config error:`/`busy:`/health messages. Python tracebacks, argparse usage errors and the web server's own log lines are third-party/diagnostic output and are not escaped. JSON/API output, stored job notes and Discord messages keep the original text.

Newest first, tab-separated. `--days` 1-3650 (default: all), `--limit` 1-500 (default 100); out-of-range values exit 2.
No database yet prints `no database yet` and exits 0; an unreadable one prints an error and exits 1.

Then put IDs or name patterns in that game's `include_sets` / `exclude_sets`. Numbers
match set IDs, and text is a case-insensitive regex on the set name. Exclusions win.

## Scheduling

TCGCSV refreshes about 20:00 UTC each day. Pick one:

- **Long-running (recommended):** `tcgwatch schedule` (alias `watch`). Runs daily at `schedule.at`
  (UTC wall clock, default `20:30`). If TCGCSV is late it polls every 10 minutes for up to
  `schedule.late_window` (default and max 3h), then records a failed `job_runs` row. On startup it resumes
  unfinished work and fills missed days from the archives (`schedule.catch_up_days`, default
  `max(7, largest alert window)` capped at 30). Snapshot and catch-up are retried (3 attempts); a `check`
  that already started is never auto-retried, so Discord can't be double-posted. Every tick, including
  no-ops and busy skips, leaves a `job_runs` row. After each successful snapshot it also prunes price
  history older than `retention_days` (below). `schedule.check_every` still works but is deprecated.
- **Windows Task Scheduler** (daily at 4:30 PM US Eastern time, machine set to that timezone;
  that is 20:30 UTC during US daylight time and 21:30 UTC after the clocks change in November):
  ```powershell
  schtasks /Create /TN tcgwatch /SC DAILY /ST 16:30 `
    /TR "\"$PWD\.venv\Scripts\tcgwatch.exe\" --config \"$PWD\config.yaml\" run"
  ```
- **cron:** `30 20 * * * /path/.venv/bin/tcgwatch --config /path/config.yaml run`

**Repairing old days.** Versions before atomic snapshots wrote a game's snapshot set by set, so a crash could leave
a partial day that later runs treat as complete. `tcgwatch backfill --force [--days N]` re-fetches the
latest snapshot and the archived days in range and rewrites each game/day in one transaction, replacing
the stored rows. Default 7 days, max 30. It never runs a check or posts to Discord; a day whose archive
is missing or has no prices for a game, or a live snapshot with no prices, keeps its stored rows. Run it once after upgrading.

**Retention.** `retention_days` (top-level, default `180`; `0` or `null` keeps everything) deletes price
rows older than that many days before the newest stored day, plus cards left with no prices that no
watchlist entry or alert references. It never goes below your largest alert window (+2 days) or
`catch_up_days`, and the watchlist is never pruned (alert history is bounded, see below). `tcgwatch schedule` does it after each successful
snapshot; a `schedule --window` override raises the floor to that window; run `tcgwatch prune --dry-run` (read-only; reports per-table counts. On a DB from an older release that lacks a table it exits 1 with `no such table` and cannot migrate it: run `tcgwatch games` once (migrates offline, no live fetch), then retry; an older `job_runs` without the `game` column previews fine, counting 0 job runs because legacy rows are never planned for deletion; the real prune runs after migration) to see what would go, `tcgwatch prune` to do it now.

The same `retention_days` also bounds `alerts` rows (by `alerted_on`) and finished `job_runs` rows (by `started`).
Alerts still inside the Discord/console cooldown window are never deleted. `job_runs` pruning is per game:
pruning one game never deletes another game's rows, and each game always keeps its newest row and newest
successful row per kind (so `tcgwatch health` and the one-shot `repair` marker keep working) plus every row of its
current dataset (so a failed `check` is never silently retried). `tcgwatch prune` ends with `VACUUM` + `PRAGMA optimize`
under the job lock so the file shrinks; the scheduler's per-snapshot prune only runs `PRAGMA optimize`.
`VACUUM` rewrites the database and can need free disk of up to twice its size
([SQLite docs](https://www.sqlite.org/lang_vacuum.html)). The deletes commit before it runs, so if `VACUUM` fails
(e.g. disk full) the data stays pruned but uncompacted: free space and run `tcgwatch prune` again.

**Backup.** `tcgwatch backup backups/tcgwatch-2026-11-07.db` makes an online, WAL-safe copy (sqlite's backup API; safe while
the web app is up, and it takes the job lock so no job writes mid-copy). It refuses to overwrite an existing file unless you pass
`--force`, writes the copy with mode 0600 (it holds your watchlist and alert history) and replaces the target atomically, so
a failed backup never damages an older good one. Without `--force` publication is atomic no-clobber via a hard link (a file that appears mid-copy is never overwritten); on a filesystem without hard links it fails safely (exit 2, nothing created at the destination) and you pass `--force` instead, and the live database, its `-wal`/`-shm`/`-journal` files and the job lock files are refused as destinations even with `--force`. Restore with `tcgwatch restore backups/tcgwatch-2026-11-07.db --force` (see below).

In Docker: `docker compose exec scheduler tcgwatch backup /data/backup.db`.

**Restore.** `tcgwatch restore <backup.db> [--force] [--lock-wait S]` is the inverse of `backup`. **Stop the web app first**: it does not
take the job lock, so the command cannot protect it (the scheduler and other CLI jobs are covered: busy exits 3, or wait with `--lock-wait`).
It opens the backup read-only and runs `integrity_check` (failure: exit 1, your database untouched) and copies it to a private temp file
beside the database (mode 0600, fsynced). A symlinked `database:` path is resolved once and the real file is restored (the link stays).
The old database is never deleted before the new one is in place: it and its `-wal`/`-shm`/`-journal` are renamed to
`<db>.pre-restore*` (so committed rows that only live in the WAL survive), the copy is published, the directory is fsynced, and only then
is the `.pre-restore*` set removed. Under the job lock and before any rename, `--force` opens the live database and runs
`PRAGMA wal_checkpoint(TRUNCATE)`, so every committed row is in the main file; if the checkpoint cannot complete the command changes nothing and exits 3 (database busy) or 2
(any other failure, e.g. a read-only or unwritable database file); the only skip is a live file SQLite reports as corrupt or not a database. A later restore refuses (exit 1) while a `.pre-restore*` set exists. If the old database or its sidecars cannot be moved aside (an OSError such as a permission error) the command exits 1 and the database is left untouched.

**Recover from an interrupted restore.** Nothing is ever deleted before the new file is durable, so every state is recoverable. Keep the web
app and scheduler stopped. If the process died (or the command exited 2 with "putting the old database back FAILED") the old files are
split between the live names and `<db>.pre-restore*`: the main file may be aside while its `-wal` is still at `<db>-wal` (the checkpoint
above means the main file already holds all committed rows, so this loses nothing). To recover: if `<db>` is a fresh restored file you do
not want, move it away; then rename every `<db>.pre-restore<suffix>` back to `<db><suffix>` (`.pre-restore` -> `<db>`,
`.pre-restore-wal` -> `<db>-wal`, and so on) so the original main and its WAL are together again, and open it. If the restore had finished
publishing, `<db>` is already the restored database and the `.pre-restore*` files are just the previous one: keep or delete them, then
remove nothing else. Never open a lone `<db>.pre-restore` without its `-wal` if one exists beside it under either name. Without `--force` an existing database is refused (exit 1) and
publication is atomic no-clobber (hard link: a file that appears mid-copy is never overwritten; no hard links: exit 2, pass `--force`);
with `--force` it is an atomic rename. If the directory cannot be opened for fsync the command exits 2 before touching anything (Windows
cannot fsync a directory and skips that step); a directory fsync failure after publication exits 1 and keeps the `.pre-restore*` set.
The live database, its sidecars, the lock files and directories are refused as the backup argument (exit 2). Start the app again afterwards.

**Restore in Docker.** Stop both services first (`docker compose stop`), because both use the DB. The container runs as uid 10001,
so it can't read a host-owned backup that is `0600`. Run the restore inside the container, which owns the files it writes:

```bash
docker compose stop
# backup.db must be readable by the container user (see the file-owner note in Docker step 1)
docker compose run --rm scheduler restore /data/backup.db --force
docker compose up -d
```

(`restore` writes the `database:` path from `data/config.yaml`. Restoring from the host instead needs the restored file owned by the container user and `chmod 0600`; the owner depends on the uid override, see Docker step 1.)

**Job lock.** `snapshot`, `backfill`, `check`, `prune`, `backup`, `restore` and `run` take a cross-process lock beside the database
(`<db>.lock`, with holder info in `<db>.lock.json`), so a cron run, `watch` and a second shell can't
overlap. A busy command exits with code 3 and names the holder; add `--lock-wait SECONDS` to wait instead.
`schedule` waits up to 10 minutes (or `--lock-wait`), then skips the tick. The OS drops the lock if a job crashes. It is same-host only (not NFS), and
with Docker Desktop don't run host-side `tcgwatch` against a DB the containers are using.

## Docker

One image runs both the web app and the background job (`schedule`) as two compose services.
Config and the SQLite DB live on a mounted `./data` volume; the image contains neither.

**1. Prepare `./data` first (Linux).** The container runs as uid 10001, so that user must be able to
write `./data` and read `data/config.yaml` (a `0600` file owned by you is unreadable to it):

```bash
mkdir -p data && cp config.example.yaml data/config.yaml
# in data/config.yaml set:  database: tcgwatch.db   (paths are relative to the config file)
sudo chown -R 10001:10001 data     # needs root; keeps config.yaml private to uid 10001 (chmod 600 ok)
```

No sudo? Either `chmod 777 data && chmod 644 data/config.yaml` (any local user can then read it, so
avoid on shared hosts), or run the containers as your own uid by adding `user: "${UID}:${GID}"`
(`export UID GID="$(id -g)"` first; bash does not export them and `GID` is usually unset) to both services in a compose override. With that override the containers run as your own uid, not 10001, so chown every file you copy in (`sudo chown "$(id -u):$(id -g)" <file>`), including a backup for the restore below; without it, use 10001:10001.
Docker Desktop (Mac/Windows) handles bind-mount ownership itself, so skip the chown there.

**Prebuilt image (GHCR).** CI (`.github/workflows/docker-publish.yml`) builds the image once on every pull request and push (a read-only `build` job: no `packages:write`, nothing pushed) and smokes that exact
image with `TCGWATCH_SMOKE_IMAGE=<tag> scripts/docker-smoke.sh` (no rebuild; unset, the script builds `tcgwatch:local` as before). On `main` only, a
separate `publish` job (the sole holder of `packages:write`) loads that same tested image, re-checks its image id, and `docker push`es it as
`ghcr.io/hue2/tcg-price-watch` with the tags `latest`, the `pyproject.toml` version (e.g. `0.12.0`) and `sha-<short>`. It authenticates with the built-in `GITHUB_TOKEN` only (no other secrets). GHCR
packages start **private**: until the owner makes the package public (GitHub: Packages > tcg-price-watch > Package settings), `docker pull`
needs `docker login ghcr.io` with a token that has `read:packages`. Pull and run it like the compose image
(`docker pull ghcr.io/hue2/tcg-price-watch:latest`; config and DB on a `/data` volume owned by uid 10001, see step 1 above; the image
needs no secrets).
If the web service logs `unable to open database file` or a config permission error, this is why.

**2. Start it:**

```bash
docker compose up -d --build
docker compose exec scheduler tcgwatch backfill --days 7   # once, to seed history
```

- Web UI: http://127.0.0.1:8765 (published on the host's loopback only; compose passes `--insecure-no-auth` for the in-container `0.0.0.0` bind. Set `TCGWATCH_WEB_TOKEN` to require a token).
- Discord webhook: put it in `data/config.yaml`, or export `TCGWATCH_DISCORD_WEBHOOK`
  (or use a git-ignored `.env`) before `docker compose up`. It is never baked into the image.
- Manual smoke (optional extra; the release gate is the checklist below, which runs `scripts/docker-smoke.sh` live): `docker compose build && docker compose up -d`, then confirm `docker compose ps`
  shows `web` healthy and `curl http://127.0.0.1:8765/api/status` answers. Without docker, CI-style
  coverage is `pytest tests/test_docker_static.py` (Dockerfile/compose parsed: non-root, loopback
  ports, real healthcheck route, real subcommands) plus `scripts/smoke-install.sh` (clean venv
  `pip install .`, `tcgwatch web` + `/api/status`, `tcgwatch health` nonzero on an empty DB).
- Release checklist, docker gate: `scripts/release-check.sh` runs `scripts/docker-smoke.sh` live by default (TOOL-3; about 1 min; needs docker with compose >= 2.24). You can also run `scripts/docker-smoke.sh` alone on a host with docker. It builds the image and runs compose against a private `mktemp` fixture (throwaway `config.yaml`, no webhook/token, no host ports, containers run as your uid:gid so no chown is needed, idle scheduler); it never reads or writes the checkout's `data/`. It polls `web-health`, then requires `tcgwatch health` to exit exactly 1 with `unhealthy: no database at ...` or `unhealthy: no ok schedule run recorded` (an exec failure such as 126/127 fails), then `compose down`s and deletes the fixture. Skips with exit 0 and a message when docker is missing. First live run: 2026-11-07 at `ac17feb` (docker 29.9.3, compose v5.7.0): exit 0, no fixes needed. Each release runs it through `release-check.sh`; a printed SKIP is a failure for the gate.
- Release checklist (review gate): `scripts/release-check.sh <new-version> <base-ref>` (e.g. `scripts/release-check.sh 0.12.0 0d648ab`; `PYTHON=.venv/bin/python` to pick the interpreter). It fails if the base ref's old version string is still in a version-carrying file (the positive `VERSION_FILES` list in the script: `pyproject.toml`, `src/tcgwatch/__init__.py`, `src/tcgwatch/config.py`, `config.example.yaml`, `tests/test_version.py`; prose and history such as SPEC, BACKLOG and README may keep old versions), if `pyproject.toml` is not at the new version, or if a backticked SHA in `docs/BACKLOG.md` is missing or not an ancestor of HEAD (`(tip `sha`)` superseded branch tips need only exist); then runs `pytest --cov` and every `tests/js/*.cjs` (found by glob). Then runs `scripts/docker-smoke.sh` (the docker gate, ON by default, about 1 min): success needs exit 0 plus the exact final `docker-smoke OK` line; a non-zero exit, a `SKIP` (no docker on the host), a missing marker, or a failure to capture/inspect the output is a FAIL (closed). A non-default `DOCKER_SMOKE` (test seam) prints a prominent `!!! DOCKER_SMOKE OVERRIDE ... !!!` notice with the path before running. The single opt-out is `RELEASE_CHECK_NO_DOCKER=1`, which prints a visible `docker gate skipped` line. Exits non-zero on any failure.
- The `web` service has a healthcheck (`tcgwatch web-health`: GET `/api/status`, sending the effective `TCGWATCH_WEB_TOKEN` / `web.auth_token` as a Bearer header, never printed); `docker compose ps` shows it.
- Runs as a non-root user (uid 10001; see step 1). Times are UTC.
- Run one-off commands with `docker compose run --rm web check --preview`.
- The `scheduler` service runs `tcgwatch schedule` (daily at `schedule.at`, UTC). Its healthcheck is
  `tcgwatch health`: healthy while an ok `job_runs` row (a skipped tick counts) is under
  24h + `schedule.late_window` + 1h old (28h by default, so a 3h late publication stays healthy); it stays "starting" until the first daily run. `docker compose ps` shows it.
- `TCGWATCH_ALLOWED_HOSTS` is not needed for the default `127.0.0.1:8765` publish (loopback Hosts
  are always accepted); set it only when you reach the UI under another name.
- Docker Desktop (Mac/Windows): keep `./data` on the host's normal filesystem and don't run host-side
  `tcgwatch` on the same DB while the containers are up. The job lock is same-host only and does not
  span the VM boundary, and SQLite WAL over some bind mounts (virtiofs/gRPC FUSE, network shares) is unreliable.
  If you need host-side commands, `docker compose stop` first or use `docker compose exec`.
- Both services share the DB; the job lock (above) keeps their runs from overlapping.
- The local `pip install -e .` / `tcgwatch` workflow is unchanged.

## Discord alerts

Put the channel's webhook URL in `config.yaml` (gitignored, never in the example file): Saving from the web UI keeps the file's existing permissions (a new file is created `0600`).

```yaml
notify:
  discord:
    webhook_url: "https://discord.com/api/webhooks/..."
alert:
  discord_threshold_pct: 50   # only post moves of 50%+; console still shows everything >= threshold_pct
```

Alternatively, leave `webhook_url` empty and set the env var named by `webhook_env`
(default `TCGWATCH_DISCORD_WEBHOOK`). Check the connection with `tcgwatch discord-test`.
Each card is posted at most once per cooldown period. If a post fails, it's retried on the
next run.

## Layout

```
src/tcgwatch/
  cli.py          commands
  config.py       YAML loading, durations, rule merging
  filters.py      rarity / variant / printing filters
  movement.py     movement rule (pure)
  models.py       plain data types shared by sources, storage and movement
  store.py        SQLite (WAL, 30s busy_timeout): cards, prices, alerts, watchlist, job_runs
  scheduler.py    `tcgwatch schedule`: daily wall-clock run, late-publish polling, resumable state
  joblock.py      cross-process job lock (flock / msvcrt) beside the database
  notify.py       console, Discord, JSON
  sources/        PriceSource interface + TCGCSV (live + history archives)
  web/            FastAPI app + static frontend
scripts/          smoke-install.sh: clean-venv install check; docker-smoke.sh: image build + compose smoke (see Docker)
tests/            pytest
```

## Development

### Build and test

Requires Python 3.11+. From a clean checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

A borrowed venv (editable install from another worktree) is fine: `tests/conftest.py` puts this checkout's `src` first on `PYTHONPATH` for the test process and its subprocesses.

`pytest` also measures coverage (pytest-cov) and fails below the gate set in
`pyproject.toml` (`--cov-fail-under=95`; the measured total on main is 97.87%, so the gate is the
measured value rounded down to the nearest 5). The per-module report prints after the tests. To
see the report without the gate, run `pytest --cov-fail-under=0`. When coverage rises, raise N in
`pyproject.toml` to the new measured total rounded down to the nearest 5.

Resource leaks fail the run. `pyproject.toml` sets `filterwarnings = ["error::ResourceWarning",
"error::pytest.PytestUnraisableExceptionWarning"]`, so an unclosed `HTTPError` body, DB connection or pipe fd
turns the test red instead of printing a warning. The second line is needed: a leak finalized during garbage
collection is re-raised by pytest as `PytestUnraisableExceptionWarning`, which otherwise only warns.
`tests/test_leak_guard.py` proves both directions with a planted leak.
Close what you catch or open (`err.close()`, `conn.close()`, `proc.stdout.close()`). A test that
removes one of those `close()` calls must fail; `tests/test_http_error_close.py` covers the HTTP ones.

Verified on Python 3.14.8: clean install, 666 tests pass (the 3 `slow` benchmarks are deselected by default), total coverage 97.87%.

The large-database benchmarks (`tests/test_perf_benchmarks.py`, marker `slow`) are not part of the
default run. Run them with `pytest -m slow --no-cov -s`; they seed a 30,000-printing, 5.4M-row DB and take
about a minute. See `docs/PERF.md` for the method and the baseline numbers.

The Runs-tab regression (`tests/js/runs_failure.cjs`) runs static `app.js` in Node. It needs `node` on
the PATH; without Node that one test is skipped, and the rest of the suite still runs.

## License

MIT. See [LICENSE](LICENSE).

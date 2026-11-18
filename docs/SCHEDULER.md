# Running the daily job reliably — plan (M1-sched-plan)

Status: **agreed design** (review: GO, Option C, with R1-R6 and Q1-Q3 folded in below).
C1-C7 are implemented (C7 compose `schedule` service and `tcgwatch health`, merged in `782f2a8`; Docker in `81b4e91`), including C5 (`tcgwatch schedule`, `src/tcgwatch/scheduler.py`; behaviour summary in
SPEC §8.2). Deviations from the plan as written: explicit `catch_up_days` may be 1-90 (default capped at 30);
`late_window` is capped at 3h; a busy-lock skip writes an `ok=1` tick row; the one-shot `repair` row (4.1)
is not part of C5 (REPAIR-2 supplies it).

## 1. Where we are

- `tcgwatch run` = `snapshot` + `check`. Something external must call it. `watch` loops `run`
  every `schedule.check_every` (default 1d). It runs `cmd_run` and then sleeps, i.e. it sleeps *from
  completion* (cli.py), so the fire time drifts by each run's duration; it is unaware of TCGCSV's ~20:00 UTC publish time, and fetches before the new dataset exists
  (harmless: it re-upserts yesterday's `snapshot_date`, then the next fire is a full day later,
  i.e. up to ~24h late). It also has no memory of whether a `check` ever ran for a stored day.
- Snapshots are already idempotent: `snapshot_date` comes from `last-updated.txt` and prices are
  upserted on `PK(product_id, printing, snapshot_date)` (SPEC §7). Re-running is safe.
- `check` is idempotent *via the alert cooldown* (`alerts` table), not via a lock.
- The web app's `JobRunner` serialises jobs with a `threading.Lock`, which only protects **one
  process**. A `tcgwatch run` from cron/`watch` plus a web "Run check" button are two processes with no
  mutual exclusion. Also it uses process-wide `redirect_stdout`, so it can't safely share a process with
  an in-process scheduler thread's output.
- SQLite is opened with defaults (no WAL, default 5s timeout in Python's `sqlite3`): two writers can
  fail with `database is locked`.
- Missed days: if a day is missed, `snapshot` alone never recovers it; only `backfill` (§3.1) does,
  and nothing calls it automatically.

## 2. Options

| | A. Scheduler in web process | B. OS scheduler (cron / systemd timer / Task Scheduler) | C. Separate compose service (`tcgwatch watch` / new `tcgwatch schedule`) |
|---|---|---|---|
| Moving parts | 0 extra | host config outside repo | 1 extra container, same image |
| Docker fit | one container | poor: needs host cron or exec into container | good: `web` + `scheduler` from one image |
| Survives web restarts/crashes | no, tied to web | yes | yes, independent |
| Web has to be running | yes (and it is localhost-only, no auth) | no | no |
| Windows/Mac dev story | same as Linux | differs per OS, README burden | same everywhere |
| Overlap risk | in-process lock only | cross-process | cross-process |
| Ops visibility | job log in UI | journald/cron mail | `docker logs` + UI status |

A couples the daily job to a UI process that is meant to be optional and that redirects global stdout.
B is fine for bare-metal users but cannot be the Docker story. C is the cleanest fit for "one image".

## 3. Recommendation

**C, with B kept supported.** Add one scheduling entry point, `tcgwatch schedule` (supersedes `watch`;
keep `watch` as an alias), run as a second compose service from the same image. `tcgwatch run`
stays the unit of work, so cron/systemd users are unchanged and benefit from the same lock and
catch-up (below).

Scheduler behaviour (detail in 3.1-3.3). `schedule.at` (default `"20:30"` UTC) is the documented path. There is no hourly
mode: the data is daily. `check_every` is kept for back-compat only: if set, interval mode is honoured and a
deprecation warning is logged. `watch` becomes an alias of `schedule`.

### 3.1 Expected date vs. remote date vs. stored date

Three different dates, never conflated:

- **Expected date `E`** (a pure function of the clock): the UTC date of the most recent `at` instant that
  has passed. At/after today's `at` (20:30 UTC) `E` = today; before it, `E` = yesterday.
- **Remote date `R`**: what `last-updated.txt` currently says.
- **Stored date**: what `has_prices` says is in the DB (trustworthy for newly written days once REL-2 lands, see 4.1).

`R < E` means the publication is **stale/late**. This is decided by comparing `R` with `E`, **not** by whether
`R` is already stored. "Yesterday is stored" is exactly the late-publish case, so it must never end the tick.

**Late-publish window.** Starting at `at`, while `R < E` the scheduler polls every 10 min until
`at + schedule.late_window` (default 3h, bounded). The poll that first sees `R >= E` proceeds to the workflow
(3.2) with dataset date `R`.

- *Acceptance:* stored D-1, remote D-1 at 20:30, remote D at 20:40 => the 20:40 poll sees `R = D`, fetches and
  checks D at 20:40 (not the next day).
- **Window expired** (`R < E` at `at + late_window`): stop polling for this tick; write a `job_runs` row
  `kind=schedule, ok=0, note="stale: remote D-1, expected D, window expired"`. This is a failure, not an `ok`
  skip, so the healthcheck (24h + late_window + 1h = 28h by default) and the UI show it. No further polling until the next `at`. If D appears later,
  the next tick or startup sees `R = D > D-1`, fetches it and catch-up (3.2 step 3) fills any gap; the check
  still runs once for the latest day, never for missed days.
- **Startup before the deadline:** with `now < at` today, `E` = yesterday, so nothing is stale; the scheduler
  does not poll early, runs 3.2 for whatever `R` is (this resumes unfinished work, see 3.2), then sleeps to
  `at`. If `R` is already today's date (published ~20:00, started 20:10), it is processed immediately rather
  than waiting for 20:30. **Startup after the deadline** inside the window polls as above; after the window
  it runs 3.2 once for `R` and, if `R < E`, records the expired-window status.

### 3.2 Per-dataset workflow state (snapshot existence is not completion)

A stored snapshot only says the *data* exists. Whether the *workflow* for that dataset ran (history filled,
first check attempted) is separate **scheduling/ops state**, persisted in `job_runs` and keyed by
`(dataset_date, game, stage)`. Price completeness is still derived only from prices, never from `job_runs`
(R2); `job_runs` only answers "was this stage attempted, and how did it end".

`job_runs` gains a nullable `game` column (additive migration, same mechanism as C1). Stages and rows:

| `kind` | written | meaning |
|---|---|---|
| `snapshot` | start (`finished` NULL) then close with `ok` | data fetch/store for `(date, game)` |
| `catchup` | start then close | backfill of missing days in the window |
| `check` | **start row written and committed BEFORE the first Discord post**, closed after | check attempt for `(date, game)` |

A stage is **done** when a closed `ok=1` row exists for its key. It is **interrupted** when a row exists with
`finished IS NULL` and no process holds the job lock (the OS released it on crash), which is detected on the
next start and closed as `ok=0, note="interrupted"`. A `check` row's *existence* (open, failed or ok) means
"attempted".

Each tick and each startup runs the same idempotent, resumable sequence for the latest dataset date `R`
(after the 3.1 gate), under the job lock:

1. **Snapshot**: if prices for `(R, game)` are missing, fetch and store (retried on failure, R3 allows
   this); otherwise skip. Record the stage row either way only if it ran.
2. **Date stability** (4.1) re-check applies as before.
3. **Catch-up**: missing days in `catch_up_days` via `has_prices`, backfill only (no checks, no posting;
   SPEC §3.1). A missing/failed catch-up is retried on the next tick; it does not depend on the snapshot
   having just been written.
4. **Check**: run if **no `check` row exists** for `(R, game)`. Existing snapshot, e.g. from a user running
   `snapshot` alone before the scheduler, does not suppress it.
5. **Tick row (R6)**: write the `schedule` `ok` row only if every stage is done or skipped as already done
   (note "nothing to do").

R3 on `check`, concretely: the start row is committed before posting. If a `check` row exists but is not
`ok=1` (interrupted or failed after it began), the scheduler **does not retry it automatically**. It records
`ok=0` (note "interrupted, not auto-retried, REL-1") and surfaces it in the UI; a human re-runs `tcgwatch
check`. If the process died *before* the start row was written, there is no row, so the next start runs the
check normally. Once REL-1 lands, auto-retry may be revisited as a separate decision.

*Acceptance cases:*

- Snapshot-only D already complete, no check row: first scheduled tick runs the check for D.
- Crash after D committed, before check started (no check row): restart resumes catch-up and runs the
  check.
- Crash during posting (open check row): restart closes it `ok=0, interrupted`, does **not** re-post, and the
  healthcheck/UI flags it.
- D fully done (all rows `ok=1`): the tick is a no-op `ok` row.

### 3.3 Retries and cost

Retries cover snapshot/catch-up only (R3). Backfill never posts, so catch-up cannot spam. The late-publish
poll (3.1) is the only loop; every iteration is one small `last-updated.txt` GET.

## 4. Locking and data design

### 4.1 Data completeness: atomic snapshots (R1, REL-2)

Today `_snapshot_game` commits once per set (`upsert_cards` and `upsert_prices` each commit), so a crash
mid-run leaves a partial day that looks complete to `has_prices` / `latest_snapshot_date`. The scheduler
would skip the fetch forever and backfill would skip the day. This is fixed **in the data, not in
`job_runs`**. REL-2 makes each game's snapshot atomic:

- Fetch all sets for the game into memory first, then write cards and prices in **one transaction**.
- Never hold a write transaction open across network fetches: that would starve other writers past
  `busy_timeout`.
- Read `last-updated.txt` before and after the fetch and retry if it changed, so a run that crosses the
  ~20:00 UTC publish cannot store new prices under the old date.

Once REL-2 lands, `has_prices` is trustworthy **for newly written days; no `snapshot_runs` table is
needed**. REL-2 does not repair partial days already stored by older versions. Bounded repair: C2 adds
`tcgwatch backfill --force --days N` (re-fetch and rewrite the last `catch_up_days`, atomically) and the
first scheduler start after upgrade runs it once (a `job_runs` `repair` row guards that it is one-shot).
Only a closed `ok=1` `repair` row suppresses it: a failed or interrupted repair (non-zero exit, `ok=0`
or unclosed row) is retried on the next start. REPAIR-1 implements only the
`backfill --force` CLI (SPEC §3.1 item 5, max 30 days). REPAIR-2 implements the one-shot workflow: at the
start of each game's workflow (before the snapshot stage), if that game has no closed `ok=1` `repair` row,
the scheduler runs `backfill --force --days min(catch_up_days, 30)` (the shared `backfill` code path, under the
already-held job lock) inside a `repair` stage (`game` = key, no retry within the tick). A non-zero exit
closes the row `ok=0` and fails the tick, but the normal snapshot/catch-up/check still run; the repair is
retried on the next tick/start until it succeeds. The repair range is capped at the 30-day force limit
(`MAX_FORCE_DAYS`) even when `catch_up_days` is 31-90; ordinary catch-up keeps the full configured range.

### 4.2 `job_runs` is ops status only (R2)

`job_runs (id, kind, started, finished, ok, dataset_date, game, note)` is written by the scheduler/CLI
wrapper and exists only for ops/UI and workflow state (3.2): last success, the healthcheck, and the
attempt/retry decision. **Data completeness is never inferred from it.** Created by the existing automatic column/table migration.

### 4.3 Job lock

- An advisory lock file next to the DB (`<db>.lock`), held with `fcntl.flock` (POSIX) / `msvcrt.locking`
  (Windows) via a small `joblock.py` context manager. Held for the duration of `snapshot`, `backfill`,
  `run`, `check`. The OS releases it on crash, so no stale-lock cleanup is needed.
- **Holder info goes in a sidecar, `<db>.lock.json` (R5)**, not into the flock'd file: `msvcrt.locking` is
  byte-range locking and reading a locked file on Windows fails. The sidecar stores `kind`, `pid`, start
  time as JSON; it is informational and may be stale, the flock is authoritative.
- **Same-host only (Q3):** works across processes and across containers sharing the data volume on the same
  host. Not NFS/network-volume safe. **Docker Desktop (Mac/Windows) caveat:** do not run a host-side
  `tcgwatch` against the bind-mounted DB while the containers use it; `flock` and the WAL shared memory do
  not cross the VM boundary. Containers sharing the volume are fine.
- **On contention:** the scheduler waits up to N minutes then skips that tick (logged); web
  `POST /api/jobs/*` returns the existing 409 "already running", extended to report the holder's `kind` and
  start time from the sidecar.

### 4.4 SQLite

`PRAGMA journal_mode=WAL` and `busy_timeout=30000` in `Store.__init__` (skipped for `:memory:`). Readers
(web) never block on the writer, and short write overlaps retry instead of failing. The lock is for
job-level exclusion; WAL is the safety net for ad-hoc commands (`discord-test`, settings reads). This
subsumes OPS-1 (closed into C1).

### 4.5 Idempotence

Snapshots upsert by dataset date, backfill skips stored days, alerts have a cooldown.

### 4.6 Web job runner (R4)

Keep `JobRunner`, but spawn `[sys.executable, "-m", "tcgwatch", ...]` as a **subprocess** (`__main__.py`
exists), not `tcgwatch` from PATH and not `cli.main` with global `redirect_stdout`. This removes the
global-redirect hazard, takes the file lock for free, and makes a web job and the scheduler the same code
path.

## 5. Docker fit (one image)

```
image: tcgwatch (python slim; pip install .)
volume: ./data  ->  /data   (tcgwatch.db, tcgwatch.db.lock, tcgwatch.db.lock.json, config.yaml mounted read-write)
services:
  web:        command: tcgwatch --config /data/config.yaml web --host 0.0.0.0 --port 8765 --insecure-no-auth
              ports: ["127.0.0.1:8765:8765"]   # publish on loopback only: no auth (SPEC §10)
  scheduler:  command: tcgwatch --config /data/config.yaml schedule
              restart: unless-stopped
```

Notes: `--host 0.0.0.0` is required inside the container, but the host port stays bound to 127.0.0.1.
The Discord webhook comes from the mounted `config.yaml` (never baked into the image). Set `TZ`/use
UTC explicitly since TCGCSV times are UTC. Healthcheck for `scheduler`: `job_runs` shows an `ok` row
within 28h (24h + late_window + 1h; covers a 3h late publication, skip ticks count, R6). Both services share the volume, which is exactly the case the lock + WAL design covers.

## 6. Implementation cards

| Card | What | Deps |
|---|---|---|
| C1 | Store: WAL + `busy_timeout=30000` in `Store.__init__` (skip `:memory:`); `job_runs` table via existing migration; tests. Small, lands first. Closes OPS-1. | none |
| C2 | REL-2: atomic per-game snapshot + date-stability check per 4.1; one-shot forced repair (`backfill --force`); test a simulated mid-fetch failure. | C1 merged (both touch store.py) |
| C3 | `joblock.py` (flock / msvcrt + sidecar JSON, R5) around snapshot/backfill/run/check; contention tests. | parallel with C2 |
| C4 | REL-1: no duplicate Discord chunks on partial failure. Must land before C5's retry logic touches `check`. | none |
| C5 | `tcgwatch schedule`: wall-clock `at`, expected/remote/stored dates and late-publish polling (10m, `late_window` 3h, 3.1), per-dataset `job_runs` workflow state with `game` column and check-start-before-post (3.2), startup resume, `catch_up_days` per Q2, retries per R3, tick rows per R6; `watch` alias + `check_every` deprecation; config + README + SPEC §3/§8. | C2, C3, C4 |
| C6 | Web `JobRunner` -> subprocess (R4) + lock-aware 409 reporting holder kind/start; drop `redirect_stdout`. | C3 |
| C7 | Compose: scheduler service `watch` -> `schedule` + `job_runs` healthcheck; README Docker section + Q3 caveat. | C5 |

Critical path: C1 -> C2 -> C5 -> C7. C3/C4 run in parallel; C6 after C3.

## 7. Decisions (formerly open questions)

- **Q1 `check_every`:** back-compat only; if set, honour interval mode and log a deprecation warning.
  `schedule.at` is the documented path; `watch` aliases `schedule`. No hourly mode.
- **Q2 `catch_up_days`:** default `max(7, largest configured alert window)`, capped at 30, explicit config
  overrides. Only missing days are fetched.
- **Q3 locking scope:** same-host only, documented, including the Docker Desktop caveat (4.3).

## RET-1: retention stage

The prune floor uses the EFFECTIVE check window: `schedule --window 365d` keeps 365d + grace even with `retention_days: 180`. Overrides are validated at startup, before any side effect. `prune --dry-run` is strictly read-only; only a truly missing DB reports "nothing stored", any other read error goes to stderr with exit 1.

After a game's snapshot stage succeeds in a tick (not on no-op ticks), the workflow runs a `prune` stage
for that game, last, under the same job lock: `scheduler.prune_history` -> `Store.prune_game` (prices and
orphan cards in one transaction). It is a normal stage: an open `prune` row is committed first, closed
ok with the counts in `note`, or closed failed (the tick then fails; the next snapshotting tick retries,
and `tcgwatch prune` can be run by hand). Never retried within a tick. Disabled when `retention_days` is
0/null. Retention days are floored by the game's alert window + grace and `catch_up_days`; see SPEC §6.

## C5 review fixes (R-C5)

- `schedule`/`watch` honor `--game` and pass the check options (`--threshold`, `--window`, `--set`, ...) to every scheduled check. `--preview` is rejected up front (a preview never posts, so it cannot be a standing job; use `tcgwatch check --preview`).
- A scheduled check runs `_check(strict=True)`: a total or partial Discord failure is accounted (delivered prefix recorded) and then raises, so the `check` row closes `ok=0` and the tick fails. Never auto-reposted (R3).
- A `check` row that is failed/interrupted keeps the workflow (and tick) failed on every restart and tick until a check succeeds.
- If the publication advances after the gate, the workflow re-resolves the day from the newest stored dataset and keys catch-up/check rows under that day, so the next tick does not re-check it.

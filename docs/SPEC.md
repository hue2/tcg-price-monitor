# TCG Price Watch — Specification

Status: draft v0.12 (shipped as 0.12.0) · 2026-11-07 (0.12: no product or schema change; tests and release gates only: TEST-18 to TEST-22, TOOL-3 release-check runs docker-smoke by default, DOCK-3 live docker pass recorded; 0.11: `tcgwatch restore`, RESTORE-1, FIX-RESTORE-1; 0.10: `term_safe` on the remaining CLI output (CLI-SAFE-2), watchlist reload errors shown instead of rejecting (UX-4), `tcgwatch runs` read-only job history (CLI-RUNS); 0.9: Cards tab CSV export, `tcgwatch alerts` with terminal-safe output, read-only DB opens for the sets/cards/history/watchlist/status GETs; 0.8: release-check script, auth-route inventory and watchlist UI tests, unstar and reload-failure fixes; 0.7.0: POST /logout and a 401 banner with Log out button in the web UI, backup/restore round-trip test; 0.6.0: backup command, HTTPError and fd leak fixes, leak guard, isolated docker-smoke check)

Dates in this document (and in `docs/BACKLOG.md`) are UTC calendar dates, `YYYY-MM-DD`.

## 1. Purpose

Track price movement of **card singles** for selected trading card games and flag cards
whose price moved more than a configurable threshold over a configurable time window.
The default rule: **flag any regular-print single that rose ≥ 30% over the last 7 days.**

## 2. Requirements

| # | Requirement | How it is met |
|---|-------------|---------------|
| R1 | Only regular prints by default — no epic / showcase / manga / alt-art — but higher rarities can be opted into | Per-game `rarities` allow-list + `exclude_name_patterns`; `--all-rarities` CLI flag bypasses both |
| R2 | Riftbound and One Piece TCG to start; games can be added or edited | Games live in `config.yaml` under `games:`; adding a game = adding a block. No code changes for any game TCGplayer lists |
| R3 | Price-movement threshold is adjustable, default +30% | `alert.threshold_pct` (global or per game), `--threshold` CLI override |
| R4 | Window and check frequency are adjustable, default weekly | `alert.window` (lookback, e.g. `7d`, `2w`) and `schedule.at` (UTC time of the daily run, default `20:30`; `schedule.check_every` is deprecated) |
| R5 | Filter by set, per game | Per-game `include_sets` / `exclude_sets` (set ID or name regex); `--set` CLI override; `tcgwatch sets` lists IDs and names |
| R6 | Notify a Discord webhook when a move exceeds a settable threshold | `notify.discord.webhook_url`; `alert.discord_threshold_pct` (global or per game), `--discord-threshold` CLI override |
| R7 | Start with a week of history instead of waiting a week | `tcgwatch backfill --days 7` loads past days from TCGCSV archives (§3.1) |
| R8 | Web app to adjust settings and see the full card list | `tcgwatch web` (§10) |

### Non-goals (v0.1)

- Sealed product (boxes, packs, bundles) — excluded automatically (they carry no rarity).
- Graded cards, non-English printings, marketplaces other than TCGplayer.

## 3. Data source

**TCGCSV** (`https://tcgcsv.com`) — a free, daily mirror of TCGplayer's catalog and pricing.
TCGplayer's own API is closed to new developers, so TCGCSV is the practical source.

| Endpoint | Use |
|----------|-----|
| `/tcgplayer/categories` | Resolve a game name → `categoryId` |
| `/tcgplayer/{categoryId}/groups` | Sets in a game |
| `/tcgplayer/{categoryId}/{groupId}/products` | Cards; `extendedData` carries `Rarity`, `Number` |
| `/tcgplayer/{categoryId}/{groupId}/prices` | `lowPrice`, `midPrice`, `highPrice`, `marketPrice` per `subTypeName` (Normal / Foil) |
| `/last-updated.txt` | Timestamp of the current dataset (updates ≈ 20:00 UTC daily) |
| `/archive/tcgplayer/prices-YYYY-MM-DD.ppmd.7z` | Historical daily price dumps (for backfill, §8) |

Verified category IDs (2026-11-06): **One Piece Card Game = 68**, **Riftbound League of Legends
Trading Card Game = 89**.

Etiquette: send a descriptive `User-Agent`, pause briefly between requests, and fetch at most
once per day, since the data only changes daily.

The source sits behind a `PriceSource` interface so another provider can be added later
(e.g. a paid API, or Cardmarket for EU prices).

### 3.1 Backfill

`tcgwatch backfill --days N` loads the N days before the latest dataset:

1. Each daily archive (`prices-YYYY-MM-DD.ppmd.7z`, about 4 MB) contains
   `{date}/{categoryId}/{groupId}/prices` files in the same JSON format as the live prices
   endpoint.
2. Archives hold **prices only**, so backfill first takes a current `snapshot` (if today's
   isn't stored yet) to load the card catalog. It then keeps only archive prices for cards
   in that catalog. Cards that were delisted before today aren't backfilled.
3. Only entries matching the strict path pattern for the configured categories are extracted,
   into a temporary directory.
4. Days that are already stored are skipped. A missing archive (404) is reported and
   skipped, and the next run retries it. Each game/day is written in one transaction.
5. **Repair: `--force`** (bounded, default 7 days, max 30 = `MAX_FORCE_DAYS`; more is a config
   error, exit 2). Days stored by pre-atomic versions (before REL-2) may be partial, and
   `has_prices` can't tell. `--force` re-fetches the latest snapshot and every archived day in
   range and rewrites each game/day via `Store.write_snapshot(..., replace_game=key)`: the
   game's existing rows for that day are deleted and the fetched rows inserted in **one
   transaction**, so a failure leaves the old day intact and stale rows don't survive. A
   missing archive, or one with no prices for a game, leaves the stored rows untouched (never
   replaced with nothing); likewise a stable live snapshot with no price points (no groups, or
   cards without prices) only upserts the cards and keeps the latest day's stored prices. Runs under the `backfill` job lock. Exit 0 only when it completed;
   the scheduler's one-shot `repair` row counts only with `ok=1` (docs/SCHEDULER.md §4.1).

Backfill never runs a check or posts to Discord.

## 4. Rarity and variant filtering

Observed values in TCGCSV data:

| Game | Rarity values | Variant markers in card name |
|------|---------------|------------------------------|
| One Piece | `C`, `UC`, `R`, `SR`, `SEC`, `L` (Leader), `SP`, `TR`, `PR`, `DON!!` | `(Alternate Art)`, `(Full Art)`, `(Manga)`, `(Parallel)`, `(SP)` |
| Riftbound | `Common`, `Uncommon`, `Rare`, `Epic`, `Showcase`, `Promo` | `(Alternate Art)`, `(Overnumbered)`, `(Signature)` |

**Default "regular print" profile**

| Game | Included rarities | Excluded by name pattern | Excluded sets |
|------|-------------------|--------------------------|---------------|
| One Piece | C, UC, R, SR, SEC, L | Alternate Art, Full Art, Manga, Parallel, SP, Treasure, Pre-Release | Promotion, Release Event, Judge, Pre-Release |
| Riftbound | Common, Uncommon, Rare (Alternate Art versions included) | Overnumbered, Signature | Promotional |

A card passes the filter if all of the following hold:

- its set is in scope (§4.1);
- its rarity is in the allow-list (empty list = all rarities);
- its name matches none of the exclude patterns.

All patterns are case-insensitive regex. `--all-rarities` drops the rarity and name filters
but keeps set scoping. Promo and event sets are excluded by default because they reuse
C/UC/R rarities for special printings.

### 4.1 Set filtering

Each game has `include_sets` and `exclude_sets`. Each entry is either:

- a **number**, which matches a TCGplayer set (group) ID exactly, e.g. `24344`; or
- **text**, a case-insensitive regex matched against the set name, e.g. `'Origins'` or `'^OP-1[0-9]'`.

A set is checked if `include_sets` is empty or matches it, **and** `exclude_sets` doesn't
match it, so exclusions win. `tcgwatch sets` lists every stored set with its ID and a
`+` / `-` mark showing whether it's checked. `--set` (repeatable) replaces `include_sets`
for one run. Exclusions still apply.

Set filters apply at check time, like the other filters, so narrowing or widening sets
uses the full stored history.

**Printing (Normal / Foil).** TCGplayer prices Normal and Foil separately. Each
`(product, printing)` pair is tracked as its own price series. The optional `printings` list
limits which printings are checked (empty = all). This matters for Riftbound, where Rares
and above are foil-only, so restricting to `Normal` would hide every Rare.

**Store broadly, filter late.** `snapshot` stores *every* single (any product with a rarity).
Filters apply only in `check`. Changing the rarity profile therefore takes effect at once,
with full history already in place.

## 5. Movement rule

For each game, take the latest snapshot date `D` and a lookback `W` (window, in days).

- **current** = the price on date `D`.
- **baseline** = the price from the most recent snapshot on or before `D − W`, and no older
  than `D − W − grace` (grace = 2 days). This tolerates a missed daily run without silently
  comparing against stale data.
- **price field**: the first field in `price_fields` (default `market`, then `mid`) where
  *both* baseline and current have a value. The same field is always used on both sides.
- **pct_change** = `(current − baseline) / baseline × 100`.

A card is flagged when all of the following hold:

| Check | Default |
|-------|---------|
| `direction` = `up` → pct ≥ threshold · `down` → pct ≤ −threshold · `both` → \|pct\| ≥ threshold | `up`, 30% |
| `max(baseline, current) ≥ min_price` (ignore penny cards) | $0.50 |
| `\|current − baseline\| ≥ min_abs_change` (ignore $0.10 → $0.14 noise) | $0.25 |
| `mid` fallback only: when `marketPrice` is null on a side, that side's `high / max(low, 0.01) ≤ max_fallback_spread` (checked on baseline and current) | 10 (`0`/`null` = off) |
| The same (product, printing) was not flagged within `cooldown` | = window |

Cards dropped by `max_fallback_spread` are not mentioned individually; `check` prints a
per-game "skipped N card(s) with a noisy mid fallback" line (suppressed by `--quiet`).
Rationale: TCGCSV exposes no listing/volume data, so a wide high/low spread is the only
free proxy for a thin, outlier-driven `mid` (see `docs/LIQUIDITY.md`).

Results are sorted by |pct_change|, largest first.

### 5.1 Discord notification

Every flagged card is reported on the console. Only cards whose \|pct_change\| is at or
above `discord_threshold_pct` are posted to the Discord webhook. The default is the same
as `threshold_pct`, and it can never be lower, because Discord can only receive cards that
were flagged.

- **Settable** globally in `alert`, per game in that game's `alert` block, or per run with
  `--discord-threshold`.
- **Separate cooldowns.** The alert log marks which alerts were posted to Discord. A card
  first flagged at +35% (console only, Discord threshold 50%) is still posted if it reaches
  +60% within the cooldown period.
- **Failures** (network error, HTTP error) are logged to stderr and the alert is not marked
  as posted, so the next run tries again. If a later chunk fails after earlier ones were
  delivered, only the cards in the delivered chunks are marked posted (no duplicate reposts
  next run); the rest retry. Rate limits (HTTP 429) are waited out and retried.
- **Message format:** one message per game, split so every post stays within Discord's 2000-character limit (packed to 1900). A single
  header or card line longer than that is truncated with an ellipsis (never mid-link: the label is
  shortened so the `[label](url)` stays intact), so one card is never split across posts and
  delivered-only accounting stays per card. Each
  line shows percent change, old price → new price, a linked card name, number, rarity,
  printing, and set. Brackets, parentheses and backslashes in card names are backslash-escaped
  (and `)` in URLs percent-encoded) so they can't break the link. `allowed_mentions` is empty, so card text can't ping anyone.
- **Embeds (optional):** `notify.discord.format: embeds` (default `text`, whose output is
  unchanged) posts one embed per card: title = name + number (linked to the product page),
  description = percent change, old → new price, rarity, printing, set; the product image
  as thumbnail when it has one; green for up, red for down. At most 10 embeds per message
  and 6000 title+description characters per message (title capped at 256, description at
  4096); the header goes in the first message's content. Delivered-only marking is the same:
  a failed message raises `DiscordPartialError` listing the cards in earlier messages.
- **Web auth guardrail:** a non-loopback `--host` exits 2 unless a token is set (`web.auth_token`, env `TCGWATCH_WEB_TOKEN` wins) or `--insecure-no-auth` is passed. With a token, every request needs a Bearer header or the `tcgwatch_token` cookie (set by `POST /login` with the token in the body; `GET /login` serves the form; `POST /logout` clears the cookie and 303s to `/login`, same-origin guarded, token-exempt, Bearer unaffected; a token in a URL is never accepted; login responses are `no-store` + `no-referrer`; Origin/Host enforced), compared with `hmac.compare_digest`; else 401. The token is masked in `/api/settings`. The Docker healthcheck is `tcgwatch web-health`, which sends the effective token as a Bearer header (never printed).
- **Web hardening:** own Host check (loopback + bound host + `--allowed-host` / `TCGWATCH_ALLOWED_HOSTS`;
  case-insensitive, IPv6 `[::1]:port` parsed independent of Starlette version) returns 400 on a forged or
  malformed Host; mutating requests whose Origin/Referer is not the same origin (scheme + host + effective
  port compared; malformed/opaque values) return 403; loopback is decided by `ipaddress`, so
  non-loopback `web --host` (including `127.evil.example`) is refused unless `web.auth_token` /
  `TCGWATCH_WEB_TOKEN` is set (v0.5, AUTH-0); with a token every request needs `Authorization: Bearer`
  or the `tcgwatch_token` cookie (set by `POST /login` with the token in the body; `GET /login` serves the form; `POST /logout` clears the cookie and 303s to `/login`, same-origin guarded, token-exempt, Bearer unaffected; a token in a URL is never accepted), compared in constant time, else 401.
- **Secret handling:** the webhook URL goes in `config.yaml` (gitignored) or in an env var
  named by `notify.discord.webhook_env`. It never goes in `config.example.yaml`.
- `tcgwatch discord-test` posts a one-line test message.

## 6. Configuration

A single YAML file (`config.yaml`; `--config` or `TCGWATCH_CONFIG` overrides the path).
Resolution order, lowest → highest priority: built-in defaults → global `alert` →
per-game `alert` → CLI flags.

```yaml
database: data/tcgwatch.db  # SQLite file; relative paths resolve from config.yaml's folder
source:
  user_agent: "tcg-price-watch/0.1 (+contact)"   # descriptive UA for TCGCSV
  request_delay_seconds: 0.2
alert:
  window: 7d            # lookback; Nd or Nw
  threshold_pct: 30
  direction: up         # up | down | both
  min_price: 0.50
  min_abs_change: 0.25
  price_fields: [market, mid]
  cooldown: 7d          # optional; don't re-flag a card within this period (default = window)
  discord_threshold_pct: 30   # post to Discord at/above this
  max_fallback_spread: 10     # skip mid-fallback moves when high/low exceeds this; 0/null = off
retention_days: 180     # prune price rows (and alerts / finished job_runs) older than this (0/null = keep everything); see §7
web:
  auth_token: ""        # required for a non-loopback `web --host`; env TCGWATCH_WEB_TOKEN overrides (v0.5)
schedule:
  at: "20:30"           # UTC wall-clock time of the daily `tcgwatch schedule` run
  late_window: 3h       # poll every 10m this long after `at` if the dataset is late (max 3h)
  catch_up_days: 7      # optional; default max(7, largest alert window), capped at 30
  # check_every: 1d     # DEPRECATED fixed-interval mode (warns); omit it
notify:
  console: true
  discord:
    webhook_url: "https://discord.com/api/webhooks/..."   # or use webhook_env
    webhook_env: TCGWATCH_DISCORD_WEBHOOK
    format: text        # text | embeds
games:
  one_piece:
    name: One Piece Card Game
    category_id: 68
    rarities: [C, UC, R, SR, SEC, L]
    exclude_name_patterns: ['\(Alternate Art\)', '\(Manga\)', ...]
    include_sets: [24833, 'Royal Blood']     # [] = all sets
    exclude_sets: ['Promotion', 'Release Event']
    alert: { threshold_pct: 40, discord_threshold_pct: 75 }   # optional per-game override
```

See `config.example.yaml` for the full annotated file.

## 7. Architecture

```
            ┌──────────── tcgwatch snapshot ────────────┐
TCGCSV ──▶  PriceSource ──▶ singles only ──▶ SQLite (cards, prices)
                                                │
            ┌──────────── tcgwatch check ───────┴───────┐
            observations (current vs baseline) ──▶ filters ──▶ movement rule ──▶ notifiers
                                                                     (console · Discord · JSON)
```

| Module | Responsibility |
|--------|----------------|
| `config.py` | Load/validate YAML, duration parsing, merge alert rules |
| `sources/` | `PriceSource` interface; `tcgcsv.py` implementation |
| `store.py` | SQLite schema, upserts, current-vs-baseline query, alert log |
| `filters.py` | Set / rarity / name-pattern / printing filter |
| `movement.py` | Pure movement computation (no I/O, unit-tested) |
| `models.py` | Shared plain data types: `Card`, `PricePoint`, `Observation`, `Mover` |
| `notify.py` | Console table, Discord webhook (threshold, rate limits), JSON file |
| `cli.py` | `games`, `sets`, `snapshot`, `backfill`, `check`, `run`, `schedule`/`watch`, `health`, `web`, `discord-test` |
| `web/` | FastAPI app (`app.py`) and static frontend (`static/`) |

### Storage schema (SQLite)

```
cards  (product_id PK, game_key, group_id, group_name, name, number, rarity, url, image_url, updated_at)
prices (product_id, printing, snapshot_date, market, low, mid, high,  PK(product_id, printing, snapshot_date))
alerts (product_id, printing, alerted_on, window_days, price_field, baseline, current, pct_change, discord)
job_runs (id PK, kind, started, finished, ok, dataset_date, note, game)
```

`job_runs` is ops/UI status only (last success, healthcheck, retry decision). Data completeness is
never inferred from it; use `prices`. The scheduler also keeps per-`(dataset_date, game, kind)` workflow
state there (§8.2). File databases open in WAL mode with a 30 s `busy_timeout` so the web app, scheduler
and CLI can share one DB (`:memory:` is left alone). **Read-only exception:** an existing DB file the
process cannot write (chmod 0444, read-only mount) is opened with `mode=ro` and nothing is written: no
WAL switch, no schema creation, no migrations. Reads work in either journal mode; a writer gets the
normal sqlite error.

`alerts.discord` = 1 when the alert was posted to Discord. Columns added after v0.1 are
applied to existing databases automatically when they're opened.

`snapshot_date` comes from TCGCSV's `last-updated.txt`, not from the local clock, so
re-running a snapshot on the same day is idempotent (it upserts).

Each game's snapshot is **atomic**: every set is fetched into memory first, then cards and
prices are written in one transaction (no write transaction is held across the network).
A failure mid-fetch or mid-write stores nothing for that game, so a partial day can never
become the "latest" snapshot that `check`/`backfill` treat as complete. The full
`last-updated.txt` timestamp is read before and after the fetch; if it changed at all (even a
same-day republish, e.g. 20:00 → 20:05), the fetch is discarded and retried (up to 3 attempts,
then the game fails with nothing stored). Only the timestamp's date is used as `snapshot_date`.

Size estimate: roughly 10k series × 365 days ≈ 3.7M price rows a year, well within SQLite's range.

**Retention (RET-1, decision Q5).** `retention_days` (default 180, ships on; `0`/`null` disables; must be a
whole number ≥ 0) bounds history. Per game, rows with `snapshot_date` earlier than
`latest stored day − N` are deleted (the boundary day itself is kept; the clock is the newest stored day, not
the wall clock, so a stalled feed never eats its own history). N is floored at
`max(retention_days, effective check window + 2-day baseline grace, catch_up_days)` (the effective window includes a `schedule --window` override, validated before the scheduler starts), so a baseline or catch-up
day is never pruned. Cards left with no prices are deleted only if no `watchlist` or `alerts` row
references them; `watchlist` is never pruned. **RET-3:** the same window also prunes `alerts` rows with `alerted_on` before
the cutoff (the cutoff is additionally held back by the largest configured cooldown, so an alert still inside
a cooldown is never deleted; cards only those alerts referenced then count as orphans) and finished `job_runs`
rows with `started` before the pruned game's own cutoff (game-less rows use the earliest cutoff). Per game, never deleted: open rows, the newest row and newest ok=1 row of each `kind` (health, one-shot `repair` marker) and any row of the game's current dataset (a failed/interrupted `check` guard, SCHEDULER 3.2); unselected games' rows are untouched. A VACUUM failure (needs up to 2x DB size free) leaves data pruned but uncompacted. Prices, alerts and orphan cards go in ONE transaction. A real `tcgwatch prune` ends with
`VACUUM` then `PRAGMA optimize` under the job lock; the scheduler's prune stage runs `PRAGMA optimize` only. `tcgwatch schedule` prunes each game right after a successful snapshot (under
the job lock) and records a `job_runs` `prune` row; a failed prune fails that tick's workflow but never
affects the data fetched. Pruning is destructive and not reversible except by re-running `backfill`.

## 8. CLI

| Command | Purpose |
|---------|---------|
| `tcgwatch games` | List configured games, filters, sets, active rule, Discord status |
| `tcgwatch sets [--game KEY]` | List stored sets with IDs and whether each is checked |
| `tcgwatch snapshot [--game KEY]` | Fetch today's prices and store them |
| `tcgwatch backfill [--days 7] [--force]` | Load past days from TCGCSV archives; `--force` atomically rewrites stored days (repair, max 30 days, §3.1) |
| `tcgwatch watchlist list [--game KEY]` | List watched card printings (read-only; empty if no DB file yet; any other read error, e.g. corrupt DB or missing table, prints to stderr and exits 1) |
| `tcgwatch watchlist add\|remove <product_id> <printing> [--lock-wait S]` | Edit the watchlist; same validation as `PUT/DELETE /api/watchlist` (`src/tcgwatch/watchlist.py`). Takes the job lock (§8.1). Exit 1: no prices for that printing / not watched; 2: invalid args |
| `tcgwatch alerts [--game KEY] [--days N] [--discord\|--no-discord] [--limit N]` | Alert history, newest first, as a tab-separated table (`Store.recent_alerts` on a read-only connection; same filters and ranges as `GET /api/alerts`: days 1-3650, limit 1-500 default 100, out of range exits 2). No alerts: `no alerts`; no DB file yet: `no database yet`, exit 0, and no file is created; any other read error prints to stderr and exits 1. Stored text cells (date, game, printing, name) pass through `textsafe.term_safe`: every control character (C0 incl. TAB/CR/LF/ESC/BEL, DEL, C1, U+2028/2029) prints as a visible `\xNN` / `\uNNNN` escape and each literal backslash as `\\` (reversible: real ESC is `\x1b`, the typed text `\x1b` is `\\x1b`), so a hostile card name cannot drive the terminal or forge columns/rows; only the app's own TABs and newlines remain The same `term_safe` applies to every other CLI/console print of stored, upstream or config text (`games`, `sets`, `watchlist list`, `notify.print_console` / `print_watchlist`, snapshot/backfill/check messages, scheduler log lines), including config game keys, `prune` notes, backup/health/config paths and explicit error messages (tracebacks, argparse usage errors and web-server log lines are outside this guarantee; JSON/API output, stored job notes and Discord text stay raw) |
| `tcgwatch web [--host 127.0.0.1] [--port 8765] [--insecure-no-auth]` | Local web app (§10); a non-loopback `--host` needs a token (§10) |
| `tcgwatch check [overrides]` | Evaluate movement and notify |
| `tcgwatch run` | `snapshot` then `check` — the entry point for schedulers |
| `tcgwatch health [--max-age 28h]` | Exit 0 if an ok `schedule` job_runs row is within max-age (default 24h + schedule.late_window + 1h; compose healthcheck) |
| `tcgwatch runs [--kind K] [--limit N] [--json]` | Job history, newest first (`Store.recent_jobs` on a read-only connection; same `kind` slug regex and `limit` 1-500 default 50 as `GET /api/job-runs`, out of range exits 2). Table columns `id kind game started finished ok dataset_date note`, text cells through `term_safe`; `--json` prints the raw rows as a JSON array, identical to the API. No rows or no DB file: `no job runs yet` (`[]` with `--json`), exit 0, no file created; any other read error goes to stderr, exit 1 |
| `tcgwatch prune [--game KEY] [--dry-run]` | Delete price history beyond `retention_days` (§6); `--dry-run` reports only (opens the DB read-only: never creates, migrates or switches journal mode; a missing DB reports nothing stored; reports per-table counts (prices, cards, alerts, job runs); an older-schema DB missing a table fails with rc 1 and a hint to run `tcgwatch games` once (migrates offline, no live fetch), then retry; permission/corruption errors keep their own diagnostics) and works on a read-only DB; real runs take the job lock and record a `prune` row |
| `tcgwatch backup <dest.db> [--force] [--lock-wait S]` | Online, WAL-safe copy of the database (`sqlite3.Connection.backup` from a read-only source connection, so rows still in the `-wal` are included). Takes the job lock (§8.1), so no job writes mid-copy (busy: exit 3). Refuses to overwrite an existing `dest` without `--force` (exit 1); `dest` that is the live DB, its `-wal`/`-shm`/`-journal` sidecars or the job lock files (`.lock`, `.lock.json`; paths resolved, even with `--force`) or a directory exits 2 (directory is checked first); a missing source DB exits 1. Writes to an exclusively created (`mkstemp`, never a predictable name, 0600 from creation) temp file in `dest`'s directory, runs `integrity_check`, sets mode 0600, then publishes it: with `--force` an atomic rename, without it an atomic no-clobber hard-link (fails if `dest` appeared meanwhile, including while waiting for the lock), so a failed or racing backup never clobbers a file. The copy is a single self-contained file (no `-wal`/`-shm`). |
| `tcgwatch restore <backup.db> [--force] [--lock-wait S]` | Inverse of `backup` (RESTORE-1). Takes the job lock (busy: exit 3); the web app does not hold it, so stop it first. Opens the backup read-only and runs `integrity_check` (failure or not a SQLite file: exit 1, live DB untouched); refuses a backup that is the live DB, its `-wal`/`-shm`/`-journal` sidecars, the lock files (paths resolved) or a directory (exit 2); a missing backup or an in-memory DB exits 1; an existing target DB needs `--force` (exit 1 otherwise, also re-checked after waiting for the lock); if the old DB and its sidecars cannot be moved aside (an OSError such as a permission error) it exits 1 and the database is left untouched. The target is `realpath`ed once (symlink kept, lock identity shared). Copies via sqlite's backup API into an exclusive `mkstemp` temp beside the DB (0600, `journal_mode=DELETE`, `integrity_check`, fsync). The parent directory is opened for fsync before any mutation (failure: exit 2, nothing touched; skipped on Windows). The old DB and its `-wal`/`-shm`/`-journal` are renamed to `<db>.pre-restore*` (never deleted first: WAL-only committed rows survive a failed or crashed restore; an existing `.pre-restore*` set makes restore refuse, exit 1), the copy is published (without `--force`: hard-link no-clobber, FileExists exit 1, no hard links exit 2; with `--force`: `os.replace`), the directory is fsynced, then the aside set is removed. Before any rename `--force` runs `PRAGMA wal_checkpoint(TRUNCATE)` on the live DB under the lock (fails closed, nothing touched: busy/incomplete exit 3; any other failure such as a read-only file, I/O error or full disk exit 2; the sole skip is a live file SQLite reports as SQLITE_CORRUPT/SQLITE_NOTADB), so a crash between the aside renames leaves all committed rows in the main file (split live-wal/aside-main recovery: README "Recover from an interrupted restore"). A publish failure rolls the set back (exit 2; sidecars first, main last and only if all sidecars returned); a FAILED rollback is reported truthfully (exit 2, lists the surviving `.pre-restore*` paths, live NOT fully restored, keep services stopped); a directory-fsync failure after publication exits 1 and keeps the set. Printed paths pass `term_safe`. In Docker: `docker compose stop`, `docker compose run --rm scheduler restore /data/backup.db --force`, `docker compose up -d` (README "Restore in Docker") |
| `tcgwatch schedule` | Long-running daily scheduler (§8.2); `watch` is an alias |
| `tcgwatch discord-test` | Post a test message to the configured webhook |

`check` overrides: `--game`, `--set 24344` / `--set Origins` (repeatable), `--window 14d`,
`--threshold 50`, `--discord-threshold 75`, `--direction both`, `--min-price 1`,
`--all-rarities`, `--json out.json`, `--quiet`, and `--preview`. Preview
ignores the cooldown, doesn't record alerts, and doesn't post to Discord, so you can try
rule changes safely.

Scheduling: use `schedule` for a long-running process, or call `run` once a day after 20:30 UTC
from Windows Task Scheduler or cron (see README).

### 8.1 Job lock

`snapshot`, `backfill`, `check`, `prune`, `backup`, `restore`, `run` and `watchlist add|remove` hold an exclusive advisory lock on `<db>.lock`
(`fcntl.flock` / `msvcrt.locking`; `src/tcgwatch/joblock.py`), re-entrant only for the owning thread (other threads wait or get busy). Holder info
(`kind`, `pid`, `started_at`) is written to the sidecar `<db>.lock.json` (informational, may be stale; the
lock is authoritative). Busy: exit code 3 unless `--lock-wait SECONDS`. `:memory:` DBs skip it.

### 8.2 `tcgwatch schedule`

Implemented in `src/tcgwatch/scheduler.py`; design and rationale in `docs/SCHEDULER.md`.

- **Dates.** Expected `E` = UTC date of the latest `schedule.at` instant that has passed; remote `R` =
  `last-updated.txt`; stored = `prices`. `R < E` means late, regardless of what is stored.
- **Late window.** While `R < E`, poll every 10 min until `at + late_window` (max 3h). The poll that sees
  `R >= E` runs the workflow immediately. On expiry the tick row is `ok=0`,
  `stale: remote D-1, expected D, window expired`. This also applies to a startup *before* today's `at`
  when `R` is older than yesterday. Startup before the deadline never polls early and processes an
  already-published `R` immediately.
- **Per-game workflow for dataset `R`** (idempotent, resumable, under the job lock): `snapshot` if `R`
  has no prices; `catchup` (backfill only, never posts) for missing days in `catch_up_days`; `check` only
  if no `check` row exists for `(R, game)`. Each stage writes an open `job_runs` row (committed before
  any side effect, so before the first Discord post) and closes it `ok` 1/0.
- **Retries (R3).** `snapshot`/`catchup` get 3 attempts (waits 60 s, 300 s). `check` is never retried
  automatically: an existing failed or interrupted `check` row is reported, not re-run; a human re-runs
  `tcgwatch check`. At each tick, open rows (a crashed run, detected under the lock) are closed
  `ok=0, interrupted`.
- **Tick rows (R6).** Every tick writes `kind=schedule`: `ok=1` for success, "nothing to do" and
  "skipped: busy" (lock held by another job after `--lock-wait`, default 10 min); `ok=0` for any failure.
  A healthcheck can require an `ok` row within 24 h + `schedule.late_window` + 1 h (28 h by default).
- `schedule.check_every`, if present, selects the deprecated fixed-interval loop (warning on stderr).
- **Sources.** `PriceSource.published_at()` is optional: sources without it fall back to
  `snapshot_date()` for the stability check (§7). `snapshot_date()` is required.

## 9. Known limitations and open questions

1. **Cold start.** Solved by `backfill` (§3.1). Run it once on a new install.
2. **Which One Piece rarities count as "regular"?** Decided (default, Q-SPEC9 Q1; revisit if the
   owner answers otherwise): SEC base art counts as regular, alongside SR and Leader. Alt-art,
   manga and parallel SEC variants stay excluded by the name patterns.
3. **Foil vs. Normal for "regular print".** Decided (Q2, keep both): foils and normal prints are
   tracked as separate series, including foils of Common/Uncommon. The `min_price` and
   `min_abs_change` defaults (§5) drop their noise. Excluding foils of Common/Uncommon is not built.
4. **Market price lag.** `marketPrice` comes from recent sales and can be null for illiquid
   cards. The fallback to `mid` (listing-based) is noisier. Mitigation shipped: `alert.max_fallback_spread`
   (default 10, `0`/`null` = off) skips noisy `mid`-fallback moves (§5 table, §6 config, and
   `docs/LIQUIDITY.md`). A listing-count or sales-volume signal is still not available from TCGCSV.
5. **Promo sets.** Promo sets have mixed rarities and are excluded by default through
   `exclude_sets`. Remove an entry there to track that set.
6. **Discord detail level.** Default (shipped): plain text lines, packed to 1,900 characters per
   message. Rich embeds are opt-in with `notify.discord.format: embeds`: one embed per card with a
   thumbnail, at most 10 embeds per message. Text stays the default because embeds are capped at 10
   cards per message.

## 10. Web app

`tcgwatch web` starts a FastAPI server at `http://127.0.0.1:8765`. It binds to localhost
by default. A non-loopback `--host` is refused unless a token is set (`web.auth_token` or
`TCGWATCH_WEB_TOKEN`; v0.5, AUTH-0), because the app has no per-user login and its settings include
the Discord webhook. `--insecure-no-auth` is the explicit opt-out, used by Docker's loopback-only
publish. Remote access is covered in the README ("Remote access"): an SSH tunnel by default. The
frontend is a single static page in plain JS, with no build step.

**Cards tab: the full list.** It shows every stored card that has a price on both
comparison dates, not just the movers.

- Columns: % change, then/now price, linked name, number, rarity, printing, set, and
  status badges (*flagged*, *Discord*, *alerted on date / posted*, *filtered out*).
- Rows above the threshold are highlighted. Every column is sortable.
- Filtering by search, set, and rarity happens in the browser.
- Three views: *flagged only*, *all cards in scope* (default), and *everything stored*.
  The last one includes cards hidden by rarity, variant, or set filters.
- One-off overrides (window, threshold %, Discord %, direction, min price, include higher
  rarities) apply to that view only and never change your saved settings.

**Watchlist and history.** A star on each Cards row adds that card printing to a `watchlist`
table in the database (not `config.yaml`). The Watchlist tab lists watched cards with latest
price, change over a span, and an inline-SVG sparkline; the chart button on a Cards row
expands a larger history line. API: `GET /api/history/{product_id}?printing=&days=`,
`GET /api/watchlist/{game}?days=`, `PUT /api/watchlist` (`{product_id, printing}`),
`DELETE /api/watchlist/{product_id}?printing=`. `/api/cards` rows gain `product_id` and `watched`.
Validation: `PUT` returns 400 for a body that is not `{product_id: int, printing: str}`, a blank printing, or a product id outside 1..2^63-1; it returns 404 `no prices for this printing` when the product is unknown or has no price series for that printing. `DELETE` returns 422 for a bad product id and 400 for an empty or whitespace-only `printing`; a non-empty `printing` that is unwatched, unknown, or already removed is a 200 no-op. `GET /api/history` returns 404 when there is no history for that card printing.

**Health badge and Runs tab.** The web UI shows a health badge from `/api/status.health` and a Runs tab with the last 20 `/api/job-runs` rows (failed rows highlighted); all text is HTML-escaped, no new JS dependency. If `/api/job-runs` fails (network or HTTP error), the Runs tab clears the stale rows, shows an escaped "Run history unavailable" notice, and retries when the tab is clicked again. The headless regression `tests/js/runs_failure.cjs` needs `node`; without it the test is skipped.

**Alerts tab.** The Alerts tab lists `/api/alerts` (newest first, limit 200) with a filter for posted to Discord vs. noise-suppressed (`discord=true|false`) and a span (7/30/90 days or all time). Each row links to the card's TCGplayer page (`cards.url`, only `http(s)` URLs become links; a card no longer in `cards` shows "(card removed)"). Every field is HTML-escaped. If the request fails, the tab clears the stale rows, shows an escaped "Could not load alerts" notice, and retries on a filter change or tab click. Headless regression: `tests/js/alerts_tab.cjs` (skipped without `node`).

**Auth handling in the UI (UX-2).** `/api/status` includes `auth_enabled` (a token is configured). `api()` in app.js treats a 401 as terminal: it shows `#authBanner` (link to `/login`), and every later `api()` call rejects immediately without a request, so there is no loop. The header `#logout` form (POST `/logout`) is visible only when `auth_enabled`. Headless regression: `tests/js/auth_401.cjs`.

**Watchlist in `check`.** Every `check` prints a `Watchlist` section per game: each watched card with current price and % change over the window, bypassing rarity/set/min-price filters (cards with no price at the window start show their current price with `n/a` change). Watched cards reach Discord only when |change| >= the effective Discord threshold, under the normal Discord cooldown (default Q4; may change per Q-SPEC9). `--quiet` hides the section; `--json` entries carry `"watched": true/false` and include watched cards that are not movers; those with no baseline carry the current price and null `baseline_*`/`pct_change`.

**Settings tab.** Edits `config.yaml`:

- **Global rule:** window, flag %, direction, price floors, cooldown, price fields, and
  schedule.
- **Discord:** threshold, webhook, and a test button. The webhook is never sent to the
  browser. A placeholder stands in for it, and leaving it blank keeps the saved URL.
- **Per game:** enabled, name, category, rarities, name exclusions, printings, and
  per-game overrides.
- **Sets per game:** name patterns, plus a table of every stored set where each one can be
  set to *by patterns*, *include*, or *exclude*, along with whether it's currently checked.
- **Games:** add or remove.

**Saving.** Edits are validated with the same parser as the CLI. Invalid settings return a
400 error with the reason, and the file is left unchanged. Writes use a comment-preserving
YAML round-trip (`ruamel.yaml`): only changed values are rewritten, comments and list
styles are kept, and the file is replaced atomically.

**Jobs.** Header buttons run *Snapshot now*, *Backfill 7 days*, and *Run check + Discord*.
The last one asks for confirmation because it posts to Discord. Each job runs as a `python -m tcgwatch` subprocess (so it takes the 8.1 job lock like
cron or `watch`), and the page streams its output. If the lock is held, by a web job or another
process, `POST /api/jobs/{kind}` answers 409 naming the holder's kind and start time.

API: `GET /api/status` (includes `health: {ok, detail}`, computed by the same `scheduler.health_check` as `tcgwatch health`; an empty DB is `ok=false`; the per-game stats open the DB read-only and never create it: a missing DB file reports every game with `days: 0, first/latest: null` (other fields, e.g. `auth_enabled`, unchanged; app.js renders it as "0 day(s) of prices"), an unreadable or no-schema DB is a 500 with the fixed body `cannot read status: database unreadable`, real error logged only), `GET /api/job-runs?kind=&limit=` (recent `job_runs` rows, newest first, `limit` 1..500 default 50, `kind` a lowercase slug; read-only open, `[]` if the DB file does not exist (checked by lstat, not by the SQLite message) or is initialized with no runs; an existing but unreadable file (chmod 000, directory), a corrupt file, or an existing zero-byte/no-schema file (`no such table`) is a 500 with the fixed body `cannot read job runs: database unreadable` (the real error is logged server-side only), and `health.detail` says "database unreadable", never masked as empty. Initialize the schema by running any writable command, e.g. `tcgwatch schedule`), `GET /api/alerts?game=&days=&discord=&limit=` (recorded alerts newest first with card name/set/number/rarity/url; `game` a lowercase slug, `days` 1..3650 keeps `alerted_on` within that many UTC days, `discord` true/false filters by whether it was posted, `limit` 1..500 default 100; same read-only open and missing/unreadable rules as `/api/job-runs`, with the fixed 500 body `cannot read alerts: database unreadable`), `GET|PUT /api/settings`, `GET /api/sets/{game}`,
`GET /api/cards/{game}?window=&threshold=&discord_threshold=&direction=&min_price=&all_rarities=`,
`GET /api/jobs`, `POST /api/jobs/{snapshot|backfill|run}`, `POST /api/discord-test`.
READ-2: `GET /api/sets/{game}`, `GET /api/cards/{game}`, `GET /api/history/{product_id}` and `GET /api/watchlist/{game}` are read-only, like `/api/job-runs` and `/api/alerts`: they open the DB with `mode=ro`, never create the file or its parent directory, and never run schema migrations (a legacy DB without a later column, e.g. `alerts.discord`, still loads). A missing DB returns `[]` for sets and watchlist, the message `No price data yet. Run a snapshot or backfill.` for cards, and 404 for history. An existing but unreadable or schema-less DB is a 500 with a fixed body `cannot read <sets|cards|history|watchlist>: database unreadable` (the real error is logged server-side only). Writes (`PUT|DELETE /api/watchlist`, `PUT /api/settings`, `POST /api/jobs/{kind}`) stay read-write. `GET /api/status` is read-only too (READ-3, below).

## 11. Roadmap

- v0.4 (shipped, 0.4.0): per-card watchlist (always report specific cards); price-history sparkline
  per card; `tcgwatch watchlist list|add|remove`; web health badge and Runs tab; price-history
  retention (`retention_days`, 180 by default); One Piece SEC base art counts as a regular print.
- v0.5 (shipped, 0.5.0): a non-loopback `tcgwatch web --host` requires `web.auth_token` /
  `TCGWATCH_WEB_TOKEN` (AUTH-0, `--insecure-no-auth` opt-out); Alerts history tab and `GET /api/alerts`
  (UX-1); retention extended to `alerts` and `job_runs` with compaction after prune (RET-3); batched
  watchlist queries, no N+1 (PERF-1, numbers in `docs/PERF.md`); Docker static checks and
  `scripts/smoke-install.sh` (DOCK-1); remote access by SSH tunnel documented in the README.
- v0.6 (shipped, 0.6.0): `tcgwatch backup <dest.db>` online WAL-safe copy with job lock and 0600 output
  (BACKUP-1, §8 CLI table); every caught urllib `HTTPError` is closed, including a malformed `Retry-After`
  (FIX-HTTP-1/2); the test suite fails on `ResourceWarning` and GC-time leaks (TEST-10); the Docker
  pre-release check `scripts/docker-smoke.sh` runs on a private temp fixture and requires an exact
  unhealthy exit from `tcgwatch health` (DOCK-2); SPEC §9.3/§9.4 decisions recorded (DOC-0.5.1).
- v0.7 (shipped, 0.7.0): `POST /logout` clears the token cookie (AUTH-1, same-origin guarded); the web UI shows a
  401 banner linking to `/login` and stops sending requests, with a Log out button when auth is enabled (UX-2);
  a backup/restore round-trip test with a stale-sidecar regression (TEST-11). Restore stays the documented manual
  path (RESTORE-1 open).
- v0.8 (shipped, 0.8.0): release gate `scripts/release-check.sh` (version leftovers, BACKLOG SHAs, pytest and
  `tests/js` headless checks; TOOL-1); auth-guard route inventory test (TEST-14); watchlist headless UI tests
  (TEST-15); unstar from the Cards tab and a reload-failure catch in the web UI (TEST-16). No API change; DOCK-3 (live docker run) and RESTORE-1 remain open.
- v0.9 (shipped, 0.9.0): Cards tab Download CSV with a formula-injection guard (UX-3); `tcgwatch alerts` with terminal-safe escaping of stored text (CLI-ALERTS, FIX-CLI-ALERTS, FIX2-CLI-ALERTS); the DB is opened read-only by `GET /api/sets`, `/api/cards`, `/api/history`, `/api/watchlist` (READ-2) and `GET /api/status` (READ-3). No schema or API shape change. Open: CLI-SAFE-2 (`term_safe` on the remaining CLI output; merged after 0.9.0, ships next), WL-TARGET (Q9), DOCK-3 and RESTORE-1.
- v0.10 (shipped, 0.10.0): CLI-SAFE-2 `term_safe` on the remaining CLI output of stored or upstream text (`b5f932b`); UX-4 watchlist reloads never reject unhandled, and a failure shows in the watchlist summary (`8752147`); CLI-RUNS `tcgwatch runs [--kind] [--limit] [--json]`, a read-only mirror of `GET /api/job-runs` that tolerates C1-era `job_runs` without the `game` column (`a4606d0`, `fc6cca2`); TEST-17 every `GET /api/*` route leaves no DB behind, plus HEAD and static mount probes (`5b57a58`); DOC-0.9.1 post-0.9.0 BACKLOG and SPEC drift (`9c632f3`). No schema or API shape change. Open: WL-TARGET (Q9), DOCK-3 and RESTORE-1.
- v0.11 (shipped, 0.11.0): RESTORE-1 `tcgwatch restore <backup.db> [--force] [--lock-wait S]`, the inverse of `backup` (§8 CLI table; answered yes on 2026-11-07), with FIX-RESTORE-1/2/3 (old DB kept aside until published, pre-aside WAL checkpoint, fail-closed exit codes) (`ae2610a`, `67c1741`, `dfb5111`, `56d199a`, merge `f5a0d64`); DOC-0.10.1 post-0.10.0 BACKLOG sha and recheck (`04b0bc7`); DOC-Q9 WL-TARGET declined, Q9 answered No on 2026-11-07 (`dd1d8be`, merge `121db82`); DOC-RESTORE-KB BACKLOG restore status (`313a1ce`, `b0f90be`, `51df228`, merge `0a77fc7`). No schema or API change. Open: DOCK-3.
- v0.12 (shipped, 0.12.0): no product or schema change, tests and release gates only. TEST-18 test subprocesses import the checkout under test (`34ea30c`, merge `81d81cd`); TEST-19 smoke-install and release-check tests strip an inherited PYTHONPATH (`71207ef`, merge `ac17feb`); TEST-20 scheduler crash-survival coverage (`63137bf`, merge `7bb863c`); TEST-21 web error-path coverage (`eae33a5`, merge `069c359`); TEST-22 restore partial aside-rename failure rolls back byte-identical (`0f57e4a`, merge `8c20571`); TOOL-3 `scripts/release-check.sh` runs `scripts/docker-smoke.sh` by default and fails closed, with `RELEASE_CHECK_NO_DOCKER=1` the only opt-out (`3113fdd`, `d748e93`, merge `9a79932`); DOCK-3 first live `scripts/docker-smoke.sh` pass recorded (`ce922a7`, `a02b44b`, merge `511ece4`); DOC-0.11.1, DOC-0.11.2 and DOC-0.11.3 post-0.11.0 doc passes (`75f1102`, `4cecd80`, `ef3aef6`; merges `a891e32`, `f087526`, `d5d3f40`). Open: none. The v0.11 line above keeps its `Open: DOCK-3` as history.
- v0.13 (unreleased): CI-1 GitHub Actions workflow `.github/workflows/docker-publish.yml` builds the image once in a read-only `build` job, runs `scripts/docker-smoke.sh` on that exact image (`TCGWATCH_SMOKE_IMAGE`, default behavior unchanged) as a fail-closed gate, and a `main`-only `publish` job (sole holder of `packages:write`) pushes that same image (id re-verified) to `ghcr.io/hue2/tcg-price-monitor` (`latest`, version, `sha-<short>`) using `GITHUB_TOKEN` (FIX-CI-1). No product, schema or API change.
- Deferred: optional login (full token login: user accounts, sessions). Build it only if the web app
  must be reachable from other devices in a way the token guardrail does not cover. Default: not needed.

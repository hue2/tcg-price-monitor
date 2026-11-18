# Performance baseline (PERF-0)

Measured on a seeded database at realistic scale: 3 games, 30,000 printings (15,000 cards with
Normal and Foil), 180 daily snapshots, 5,400,000 price rows, 300 watched series. The DB file is about
640 MB. This page records the **before** numbers that PERF-1 has to beat. It is not a regression gate.

## How to run

The benchmark is marked `slow` and is excluded from the default `pytest` run (`addopts` in
`pyproject.toml`). Run it from a dev install:

```bash
.venv/bin/pytest -m slow --no-cov -s
```

- `--no-cov`: a subset run would fail the 95% coverage gate, which is meant for the full suite.
- `-s`: prints the seeding time. Seeding takes about one minute.
- `TCGWATCH_PERF_DIR=/some/dir`: keep the seeded DB there and reuse it on later runs (it is only
  seeded when `tcgwatch.db` is absent).
- `TCGWATCH_PERF_REPORT=/some/file.json`: write the timings, row counts and EXPLAIN QUERY PLAN output as JSON.

Seeding lives in `tests/perf_seed.py` and can also run on its own:
`python tests/perf_seed.py OUTDIR` writes `OUTDIR/tcgwatch.db` and a matching `config.yaml`, so
`tcgwatch --config OUTDIR/config.yaml web` (or `check`) can be used by hand on the same data.
The generated config writes `database` as an absolute path. A relative path would be resolved against
the config's folder and doubled (`OUTDIR/OUTDIR/tcgwatch.db`), which points at an empty DB. Before any
timing, the fixture checks that the loaded config targets the seeded DB, and each endpoint must return
non-empty rows. A run that hits an empty DB fails instead of producing a fast baseline.
`tests/test_perf_seed.py` (fast, in the default run) checks the seeder's shape on a tiny DB.

The benchmark does not hit the network. `check` runs with `--preview`, which skips the cooldown, does
not record alerts and does not post to Discord. Each fast endpoint is timed as the median of 3 runs.
`check` is timed once.

## Seeded shape

| Item | Value |
|---|---|
| Games | `op`, `riftbound`, `pokemon` (5,000 cards each) |
| Printings | 30,000 (Normal, Foil) |
| Days | 180 (2026-05-11 to 2026-11-06) |
| `prices` rows | 5,400,000 |
| Watched series | 300 (100 per game) |
| `alerts`, `job_runs` | empty |

## Baseline timings

Machine: AMD Ryzen 9 9950X (16 cores), 29 GB RAM, Linux, Python 3.14.8, SQLite 3.53.4. The OS page
cache was warm (the DB was already read by the seeding and the earlier run), so these are warm-cache numbers.
Commit: PERF-0 on top of `main` 3e83219.

| Operation | Median / value | Rows returned | Target (PLAN-3) |
|---|---|---|---|
| `GET /api/cards/op` | 0.27 s | 9,975 | `/api/cards` < 1 s: met |
| `GET /api/cards/riftbound` | 0.27 s | 9,971 | met |
| `GET /api/cards/pokemon` | 0.27 s | 9,974 | met |
| `GET /api/watchlist/op` | 0.01 s | 100 | no target (batch the N+1 in PERF-1) |
| `tcgwatch check --preview` (all 3 games) | 1.0 s | n/a | `check` < 30 s: met |

Re-run on the fixed benchmark (one fresh seed, then two more runs on the reused DB):
the endpoint medians stayed within 0.265–0.274 s, the watchlist within 0.011–0.013 s, and `check`
within 0.98–1.01 s. An earlier run recorded 0.32 / 0.41 / 0.43 s and 1.81 s for the cards endpoints
and `check`, so the spread between sessions on this machine is wide. The row counts are identical in every run.

A first run with 60 watched series (not kept) gave `/api/cards` 0.36–0.91 s and `check` 2.18 s.

## EXPLAIN QUERY PLAN

Plans are captured from the SQL the code actually runs (sqlite's trace callback, with parameters expanded),
then explained on a separate connection. Only the distinct statement shapes are listed.

Nothing in the endpoint or `check` paths does a full scan of `prices`. The plans use the
`prices` primary key `(product_id, printing, snapshot_date)` for all per-series lookups. `cards_game`
drives the per-game filter, and `alerts_card` covers `last_alerts`. The `pick` CTE does a range search on
the date column. The only `SCAN` lines are over the small `watchlist` table and over the `pick` CTE result.

- `latest_snapshot_date`: `SEARCH c USING COVERING INDEX cards_game`, `SEARCH p USING COVERING INDEX sqlite_autoindex_prices_1`
- `observations` (`pick` CTE): `CO-ROUTINE pick`, `SEARCH prices USING COVERING INDEX sqlite_autoindex_prices_1 (ANY(product_id) AND ANY(printing) AND snapshot_date>? AND snapshot_date<?)`, then `SEARCH` on `cur` and `base` by full key
- `last_alerts`: `SEARCH c USING COVERING INDEX cards_game`, `SEARCH a USING INDEX alerts_card (product_id=?)`, `USE TEMP B-TREE FOR GROUP BY`
- `watched`: `SCAN w`, `SEARCH c USING INTEGER PRIMARY KEY`, `USE TEMP B-TREE FOR ORDER BY`
- `history` (sparkline, per series): `SEARCH prices USING INDEX sqlite_autoindex_prices_1 (product_id=? AND printing=? AND snapshot_date>?)`
- `watched_current` (check, per series): `SEARCH prices USING INDEX sqlite_autoindex_prices_1 (product_id=? AND printing=? AND snapshot_date=?)`

**Conclusion for PERF-1:** the plans show no scan that needs a new index, so none is added on this evidence.
The `prices_date` index is not used by these queries. `watchlist` gets an N+1 pattern (one `history` query
per watched card, and one `watched_current` query per card in `check`). Each query is cheap here, so
at 300 watched series the N+1 cost is small (`/api/watchlist` 0.03 s). PERF-1 should still batch it, and
its after-numbers should be compared against this baseline.

## Caveats

- Warm-cache numbers on one fast machine. A cold start or a slower disk will be slower.
- Synthetic prices: a daily random walk with a few NULL fields. Real TCGCSV data has different
  sparsity and more matching cards, so the row counts returned by `/api/cards` may differ.
- A single run of `check`. Repeat the run before comparing small changes.

## PERF-1: N+1 removed (after)

`/api/watchlist/{game}` now calls `Store.history_many` (one query per 300 series instead of one per
watched card), and `Store.watched_current` (used by `check`) fetches the current-day rows in the
same batched way. Output is identical (`history_many` is tested equal to per-series `history`, and the
existing watchlist/check tests pass unchanged). No index was added: the EXPLAIN plans above still hold
(each batch is a `SEARCH ... USING sqlite_autoindex_prices_1` per series).

Same seeded DB and machine, same session, back-to-back (before = main 2a1b621, after = this change):

| Operation | Before | After |
|---|---|---|
| `GET /api/watchlist/op` (100 series) | 0.013 s | 0.015 s |
| `GET /api/cards/op` | 0.258 s | 0.263 s |
| `tcgwatch check --preview` (3 games, 300 series) | 0.925 s | 0.942 s |

At this scale there is no measurable speedup: the per-series queries were already sub-millisecond
on a warm cache, so the differences are noise. The change removes the query count growth with the
watchlist (O(N) round trips to O(N/300)), which matters on a cold cache or slower disk, not on this benchmark.

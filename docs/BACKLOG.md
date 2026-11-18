# Backlog

Prioritized from SPEC §9 (open questions) and §11 (roadmap), plus gaps found on main.
**Refreshed for release 0.12.0** against main `d5d3f40` (TEST-18 to TEST-22, TOOL-3, DOCK-3 and DOC-0.11.1 to DOC-0.11.3 merged after 0.11.0; the 0.11.0 refresh listed all v0.6 to v0.11 cards merged, including TEST-14 to TEST-16, TOOL-1, TOOL-2, FIX-TOOL-2, UX-3, READ-2, READ-3, CLI-ALERTS and its fixes, plus CLI-SAFE-2, UX-4, TEST-17, DOC-0.9.1 and CLI-RUNS after 0.9.0, and DOC-0.10.1, DOC-Q9, RESTORE-1 and DOC-RESTORE-KB after 0.10.0). Dates are UTC calendar dates (`YYYY-MM-DD`).
Sizes: **S** ≤ half a day, **M** 1–2 days, **L** 3+ days.

An item counts as **shipped** only when it is merged to main.

## Shipped (on main)

Milestone 1, the v0.4 roadmap and the v0.5 roadmap items are merged. See `git log --merges main`.

| Area | Cards (merge commit) |
|------|----------------------|
| Data integrity | REL-1 (`8de7036`), REL-2 (`4ebc477`), REL-3 fetch retry (`1604665`), REPAIR-1, REPAIR-2 (`912502d`) |
| Security | SEC-1 Host/origin guard (`bb072f1`), SEC-2 config file mode (`3fb3f09`), AUTH-0 non-loopback web bind requires a token, POST `/login`, authenticated `web-health` (`6950534`, fix `ff51247`) |
| Jobs / ops | C1 WAL + `job_runs` (`0e82fa0`), C3 job lock (`ff02425`), C5 scheduler, C6 subprocess runner (`c2616b6`), C7 compose `schedule` + `tcgwatch health` (`782f2a8`), C7b health max-age (`99f241d`), Docker (`81b4e91`), OBS-1 job-history API + shared health, read-only DB (`8429fe5`), BUG-7 JobRunner closes child stdout (`74df91d`) |
| Docker | DOCK-1 static Dockerfile/compose tests + `scripts/smoke-install.sh` (`a44d667`, commits `8fb7c94`, `9b1b431`); DOCK-3 first live `scripts/docker-smoke.sh` run, docker 29.9.3 / compose v5.7.0, base `ac17feb`: exit 0, no image or compose bug (2026-11-07, the release gate, Q7; commits `ce922a7`, `a02b44b`, merge `511ece4`) |
| Watchlist | B4/B5/B7 API, sparklines, Watchlist tab (`3d00bf1`); WL-check (`c0d4b97`); WL-polish (`88b4aec`); DOC-WL SPEC §10 contract (`3ecc47b`); CLI-WL `tcgwatch watchlist list\|add\|remove` (`7513ecc`, commit `a265a7f`) |
| Discord | FEAT-2 markdown escaping, B13 opt-in embeds, BUG-6 2000-char limit (`c8aa70a`) |
| Config / bugs | BUG-1, BUG-2 (`87bb3f7`), BUG-4 (`b2889b8`), BUG-5 + LIQ-1 `max_fallback_spread` (`eb739c2`), CLI-WL-2 missing-DB vs. unreadable (`542dfb7`) |
| Rules | FILT-SEC: One Piece base-art SEC counts as a regular print (`4aea19a`) |
| Retention | RET-1 price-history retention, `retention_days: 180`, `tcgwatch prune [--dry-run]` (`dae3e4e`); RET-2 dry-run error classification and legacy-schema guidance (`3e83219`); RET-3 retention for `alerts` and finished `job_runs`, per-game guards, compaction after prune (`a424765`) |
| Alerts | UX-1a `GET /api/alerts` (`3aafeba`, commit `2e78d3f`); UX-1b Alerts history tab with stale-request guards (`750581a`, commits `ffd9b57`, `30831ee`) |
| Performance | PERF-0 seeded large-DB benchmark, `slow` marker (`5828e81`, fix `2a1b621`); PERF-1 batched watchlist history and current-price queries, no N+1 (`9bbc6c2`) |
| Web ops | OBS-2 health badge + Runs tab (`d698929`, fix `858374f`) |
| Tests / docs / hygiene | TEST-1..TEST-9 (TEST-4 `53cbcdb`, TEST-5 `fa0d967`, TEST-6b `0dc9d1d`, TEST-7 `0bc42bd`, TEST-8 `77f1ff8` real >300-series chunking + `/api/alerts` regressions, TEST-9 `068db81`), QA-WL, HYG-1..HYG-3, DOC-M1-sync, DOC-WL, DOC-BL (`8bd3ef2`), DOC-0.4.1 |
| Coverage | COV-1 pytest-cov gate at 90% (`81f0025`), raised to 95% in REL-0.4 (`e6f7d86`) |
| Release | REL-0.4 version 0.4.0 + docs (`e6f7d86`, merge `4230719`) |
| Release 0.6.0 | DOC-0.5.1 (`0ca3ded`); FIX-HTTP-1 close every caught `HTTPError` (`097da42`, merge `01f8c46`); FIX-HTTP-2 malformed `Retry-After` closes `HTTPError` (`f812040`, merge `c348ec3`); TEST-10 leak guard, fails on `ResourceWarning` and GC-time leaks (`a95620e`, `7979b1a`); BACKUP-1 `tcgwatch backup` (`cfb1817`, fix `2137e5a`, fix `a6b949d`, merge `afd6758`); DOCK-2 `scripts/docker-smoke.sh` (`f49f54f`, fix `e1bd0be`, merge `8f6e1a3`); REL-0.6 version 0.6.0 + docs |
| Release 0.7.0 | AUTH-1 `POST /logout` clears the token cookie (`bd82879`, merge `679dc94`); UX-2 401 banner to `/login`, stop further requests, Log out button when auth is enabled (`f255349`, merge `140e8e8`); TEST-11 backup -> documented restore round trip + stale-sidecar regression (`8cbc311`, merge `10f098e`); TEST-12 `auth_401.cjs` explicit rejection collector, malicious-detail and stale-success coverage (`0796682`, merge `5e46305`); TEST-13 `auth_401.cjs` asserts the 401 detail does not leak and an older failure does not overwrite a newer success (`e60af69`, merge `d3c0f32`); DOC-0.6.1 (`bdf44a4`); DOC-0.6.2 Docker restore steps, UTC dates, pre-release wording (`2fd7d13`, merge `7fca328`); REL-0.7 version 0.7.0 + docs (`cf8ca8d`, merge `7a24718`) |
| Release 0.8.0 | TEST-14 auth-guard route inventory, every route guarded or allowlisted (`bfb6b86`, tightened in `9104e78`, merge `f5502c7`); TEST-15 `watchlist.cjs` headless UI coverage (`e824c99`, merge `69c0777`); TEST-16 unstar from Cards tab, reload-failure catch in `app.js`, 401 second-click alert count (`a430f08`, merge `e515d40`); TOOL-1 `scripts/release-check.sh` (`dfb3309`, fix `e4d3581`, merge `852c9e3`); TOOL-2 release-check leftover check uses a positive list of version-carrying files (`5732676`); FIX-TOOL-2 README describes that list (`234d0c9`); DOC-0.8.1 BACKLOG refresh (`86346d7`); REL-0.8 version 0.8.0 + docs (`240934a`) |
| Release 0.9.0 | UX-3 Cards tab Download CSV, client-side with a formula-injection guard (`09a3a2f`, merge `6f01d95`); READ-2 `GET` sets/cards/history/watchlist open the DB read-only (`adfe6d6`, merge `bc5bdd2`); CLI-ALERTS `tcgwatch alerts` (`fb33e34`), FIX-CLI-ALERTS `term_safe` on stored text (`820df2f`), FIX2-CLI-ALERTS reversible backslash encoding (`861b52b`), merge `50dcdfc`; READ-3 `GET /api/status` opens the DB read-only (`805b19e`, merge `fa952a7`); REL-0.9 version 0.9.0 + docs (`48fb39a`) |
| Release 0.10.0 | CLI-SAFE-2 `term_safe` on the remaining CLI output of stored or upstream text (`dca6b1e`, `0092d45`, `aee19a0`, merge `b5f932b`); UX-4 watchlist reload errors (`8752147`, merge `a1c5e96`); TEST-17 GET no-create walk + HEAD/static probes (`5b57a58`, merge `9d4bb70`); DOC-0.9.1 post-0.9.0 doc drift (`224fbcd`, `9c632f3`, merge `36e896f`); CLI-RUNS `tcgwatch runs` (`a4606d0`, fix `fc6cca2`, merge `1f7be73`); REL-0.10 version 0.10.0 + docs (`6cea9b8`) |
| Release 0.11.0 | DOC-0.10.1 BACKLOG 0.10.0 sha and stale-branch recheck (`04b0bc7`); DOC-Q9 Q9 answered No, WL-TARGET declined (`dd1d8be`, merge `121db82`); RESTORE-1 `tcgwatch restore` (`ae2610a`, fixes `67c1741`, `dfb5111`, `56d199a`, merge `f5a0d64`); DOC-RESTORE-KB BACKLOG restore status (`313a1ce`, `b0f90be`, `51df228`, merge `0a77fc7`); REL-0.11 version 0.11.0 + docs (`0ddcd50`, merge `0d648ab`) |
| Release 0.12.0 | TEST-18 test subprocesses import the checkout under test (`34ea30c`, merge `81d81cd`); TEST-19 smoke-install and release-check tests strip inherited PYTHONPATH (`71207ef`, merge `ac17feb`); TEST-20 scheduler crash survival (`63137bf`, merge `7bb863c`); TEST-21 web error paths (`eae33a5`, merge `069c359`); TEST-22 restore partial-aside rollback (`0f57e4a`, merge `8c20571`); TOOL-3 release-check runs docker-smoke by default, fails closed (`3113fdd`, `d748e93`, merge `9a79932`); DOCK-3 live docker pass recorded (`ce922a7`, `a02b44b`, merge `511ece4`); DOC-0.11.1 post-release doc pass (`75f1102`, merge `a891e32`); DOC-0.11.2 borrowed-venv note and stale-branch list (`4cecd80`, merge `f087526`); DOC-0.11.3 docker gate wording (`ef3aef6`, merge `d5d3f40`); REL-0.12 version 0.12.0 + docs (`c411e8e`, fix `34fedb1`, merge `b84be63`) |

**Release template (REL-x, from 0.12.0).** Each release paragraph names `scripts/release-check.sh` as the gate. It runs `scripts/docker-smoke.sh` live inside itself by default (TOOL-3; `RELEASE_CHECK_NO_DOCKER=1` is the only opt-out), so a standalone run of `scripts/docker-smoke.sh` is optional (for example, to debug a docker failure); when run, it must end with `docker-smoke OK`. Run the gate on the release branch. The 0.11.0 paragraph below predates TOOL-3 and names only the first gate.

**Release 0.12.0 (REL-0.12).** The v0.12 cards above are merged. They add tests, the docker gate inside release-check (TOOL-3) and the live docker record (DOCK-3), with no product or schema change. REL-0.12 only updates docs and the version (pyproject, `__init__`, config default user agent, `config.example.yaml`, `tests/test_version.py`). The release gate is `scripts/release-check.sh 0.12.0 0d648ab`, run on the release branch; it runs `scripts/docker-smoke.sh` live (about 1 min, needs docker) and must pass with `docker-smoke OK`.

**Release 0.11.0 (REL-0.11).** The v0.11 cards above are merged. REL-0.11 only updates docs and the version
(pyproject, `__init__`, config default user agent, `config.example.yaml`, `tests/test_version.py`). The release gate
is `scripts/release-check.sh 0.11.0 6cea9b8`, run on the release branch.

**Release 0.10.0 (REL-0.10).** The v0.10 cards above are merged. REL-0.10 only updates docs and the version
(pyproject, `__init__`, config default user agent, `config.example.yaml`, `tests/test_version.py`). The release gate
is `scripts/release-check.sh 0.10.0 48fb39a`, run on the release branch.

**Release 0.9.0 (REL-0.9).** The v0.9 cards above are merged. REL-0.9 only updates docs and the version
(pyproject, `__init__`, config default user agent, `config.example.yaml`, `tests/test_version.py`). The release gate
is `scripts/release-check.sh 0.9.0 240934a`, run on the release branch.

**Release 0.8.0 (REL-0.8).** The v0.8 cards above are merged. REL-0.8 only updates docs and the version
(pyproject, `__init__`, config default user agent, `config.example.yaml`, `tests/test_version.py`). The release gate
is `scripts/release-check.sh 0.8.0 7a24718`, run on the release branch.

**Release 0.7.0 (REL-0.7).** The v0.7 cards above are merged. REL-0.7 only updates docs and the version
(pyproject, `__init__`, config default user agent, `config.example.yaml`, `tests/test_version.py`). RESTORE-1 was merged later (main `f5a0d64`, see below); at the time of REL-0.7 the manual restore in the README and SPEC §8 was the documented path.

**Release 0.6.0 (REL-0.6).** The v0.6 cards above are merged. REL-0.6 only updates docs and the version
(pyproject, `__init__`, config default user agent, `config.example.yaml`, `tests/test_version.py`).

**Release 0.5.0 (REL-0.5).** The v0.5 slice (PLAN-3) is merged: RET-3, PERF-1, UX-1a/b, AUTH-0, TEST-8,
DOCK-1. REL-0.5 is the last card of PLAN-3 and only updates docs and the version.

| Card | Feature | State |
|------|---------|-------|
| RET-2 / RET-3 | retention for alerts and job_runs; compaction | shipped (above) |
| PERF-1 | batched watchlist queries | shipped (above) |
| DOCK-1 | Docker static tests + `scripts/smoke-install.sh` | shipped (`a44d667`) |
| UX-1a / UX-1b | `GET /api/alerts` and the Alerts history tab | shipped (`3aafeba`, `750581a`) |
| AUTH-0 | non-loopback web bind requires a token; `POST /login` form (guardrail, not user accounts) | shipped (`6950534`) |

## Open: also in flight

No cards are open.


## Q. Product questions

| # | Question | Default in use | Affects |
|---|----------|----------------|---------|
| Q1 | One Piece: should base-art **SEC** count as "regular print"? | **Include SEC** (shipped in FILT-SEC `4aea19a`). | `rarities` in `config.example.yaml` |
| Q2 | Exclude **foils of C/UC** from "regular print"? | **Keep both.** `min_price`/`min_abs_change` drop the noise. | SPEC §9.3 |
| Q3 | Discord: plain text or rich **embeds**? | **Text by default, embeds opt-in** (shipped, B13). | `notify.discord.format` |
| Q4 | Watched cards: Discord every run, or only on threshold? | **Discord only on threshold** (shipped, WL-check). | WL-check |
| Q5 | Keep price history forever, or prune after N days? | **Prune after 6 months** (decided 2026-11-07): `retention_days: 180` by default, `0`/null disables. Shipped in RET-1 (`dae3e4e`); alerts and job_runs follow it in RET-3 (`a424765`). | RET-1, RET-3 |
| Q6 | Should alerts and job run history follow `retention_days` too? | **Yes, same knob** (shipped in RET-3). | RET-3 |
| Q7 | Docker image builder before each release? | **Answered: `scripts/docker-smoke.sh` runs live at each release, and SKIP fails the gate.** `scripts/release-check.sh` runs it by default (TOOL-3, `RELEASE_CHECK_NO_DOCKER=1` is the only opt-out). Static checks run on every branch. First live run 2026-11-07, `ac17feb`: pass (exit 0). | DOCK-1, DOCK-2, DOCK-3, TOOL-3 |
| Q8 | Does the web app need to be reachable from other devices? | **No.** Loopback stays the default; AUTH-0 adds the token guardrail; remote access is an SSH tunnel recipe. Full login is not built. | AUTH-0, README "Remote access" |
| Q9 | Should watched cards support per-card target prices ("alert me when this card drops below $Y or rises above $Z", on top of the global % threshold)? | **Answered No, 2026-11-07.** Per-card target prices are not built; the idea is in Deferred as WL-TARGET. | WL-TARGET (declined) |
| Q10 | Add a `tcgwatch restore` command, or keep the manual restore steps? | **Answered yes, 2026-11-07.** Built as RESTORE-1, merged in main `f5a0d64`, shipped in 0.11.0. | RESTORE-1 |

## Deferred

- **WL-TARGET (declined, Q9 answered No on 2026-11-07).** Per-card target-price alerts on the watchlist ("alert me when this card drops below $Y or rises above $Z", on top of the global % threshold), with CLI `--below/--above` and a Discord alert. Declined, so it is not built and not planned.
- **Full login (user accounts, sessions, per-user tokens)** (SPEC §11): only if the app must be reachable
  from other devices in a way the AUTH-0 token guardrail does not cover. Q8 default is no.
- **SPEC §9.1 cold start:** solved by `backfill`. **§9.5 promo sets:** config-only by design.

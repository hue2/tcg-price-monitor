"""`tcgwatch schedule`: wall-clock daily run with late-publish polling and resumable workflow state.

Design: docs/SCHEDULER.md (3.1 dates and the late window, 3.2 per-dataset workflow state in ``job_runs``).
Three dates are never conflated: the *expected* date E (pure function of the clock), the *remote* date R
(``last-updated.txt``) and what is *stored* (``prices``). Price completeness is derived from ``prices``
only; ``job_runs`` records which workflow stages were attempted and how they ended.
"""

from __future__ import annotations

import sqlite3
import sys
import time as _time
import traceback
from collections.abc import Callable
from typing import NamedTuple
from datetime import date, datetime, time, timedelta, timezone

from .config import AppConfig, GameConfig
from .joblock import JobLockedError, job_lock
from .store import Store, is_missing_db
from .textsafe import term_safe

LATE_POLL = timedelta(minutes=10)
RETRY_DELAYS = (60.0, 300.0)  # between the 3 attempts of a snapshot/catch-up stage (R3: never `check`)
SLEEP_CHUNK = 300.0
UTC = timezone.utc


def expected_date(now: datetime, at: time) -> date:
    """UTC date of the most recent `at` instant that has passed (yesterday's before today's `at`)."""
    now = now.astimezone(UTC)
    return now.date() if now.time() >= at else now.date() - timedelta(days=1)


def fire_instant(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=UTC)


def next_fire(now: datetime, at: time) -> datetime:
    """Next `at` instant strictly after `now`."""
    today = fire_instant(now.astimezone(UTC).date(), at)
    return today if now < today else today + timedelta(days=1)


class Scheduler:
    def __init__(self, cfg: AppConfig, *, snapshot_fn: Callable, backfill_fn: Callable,
                 check_fn: Callable, source_for: Callable[[GameConfig], object],
                 now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 sleep: Callable[[float], None] = _time.sleep, lock_wait: float = 600.0,
                 retry_delays: tuple[float, ...] = RETRY_DELAYS, game_keys: list[str] | None = None,
                 repair_fn: Callable | None = None, window_for: Callable[[str], int] | None = None):
        self.window_for = window_for  # game_key -> effective check window (CLI overrides); floors pruning
        self.repair_fn = repair_fn  # (game_key, days): one-shot `backfill --force`; must raise on failure
        self.game_keys = game_keys
        self.cfg, self.snapshot_fn, self.backfill_fn, self.check_fn = cfg, snapshot_fn, backfill_fn, check_fn
        self.source_for, self.now, self.sleep = source_for, now, sleep
        self.lock_wait, self.retry_delays = lock_wait, retry_delays

    def log(self, msg: str) -> None:
        print(f"[{self.now().strftime('%Y-%m-%d %H:%M:%SZ')}] {term_safe(msg)}", flush=True)

    # --- long-running loop -------------------------------------------------

    def run_forever(self) -> None:
        self.log(f"scheduler: daily at {self.cfg.at:%H:%M} UTC, late window {self.cfg.late_window}, "
                 f"catch-up {self.cfg.catch_up_days}d; Ctrl+C to stop")
        self._guarded_tick(startup=True)  # resume unfinished work, process an already-published dataset
        while True:
            self._sleep_until(next_fire(self.now(), self.cfg.at))
            self._guarded_tick(startup=False)

    def _guarded_tick(self, *, startup: bool) -> None:
        try:
            self.tick(startup=startup)
        except Exception as e:  # keep the loop alive across unexpected errors; leave a visible failure row
            traceback.print_exc()
            try:
                with Store(self.cfg.database) as store:
                    store.job_record("schedule", False, f"crashed: {type(e).__name__}: {e}")
            except Exception:
                traceback.print_exc()

    def _sleep_until(self, target: datetime) -> None:
        while (remaining := (target - self.now()).total_seconds()) > 0:
            self.sleep(min(remaining, SLEEP_CHUNK))

    # --- one tick ------------------------------------------------------------

    def _remote_dates(self, games: list[GameConfig]) -> dict[str, date | None]:
        out: dict[str, date | None] = {}
        for g in games:
            try:
                out[g.key] = self.source_for(g).snapshot_date()
            except Exception as e:  # network/parse trouble: treated like "not published yet"
                self.log(f"{g.name}: cannot read remote date: {e}")
                out[g.key] = None
        return out

    def tick(self, *, startup: bool = False) -> bool:
        games = self.cfg.selected_games(self.game_keys)
        if not games:
            return True
        at = self.cfg.at
        expected = expected_date(self.now(), at)
        deadline = fire_instant(expected, at) + self.cfg.late_window

        remote = self._remote_dates(games)
        stale = lambda: [g for g in games if remote[g.key] is None or remote[g.key] < expected]  # noqa: E731
        while stale() and self.now() < deadline:
            self.log(f"remote not at {expected} yet ({', '.join(str(remote[g.key]) for g in stale())}); "
                     f"polling again in {LATE_POLL}")
            self.sleep(min(LATE_POLL, deadline - self.now()).total_seconds())
            remote = self._remote_dates(games)
        expired = stale()

        try:
            with job_lock(self.cfg.database, "schedule", self.lock_wait):
                with Store(self.cfg.database) as store:
                    return self._locked_tick(store, games, remote, expected, expired)
        except JobLockedError as e:
            self.log(f"skipping tick: {e}")
            with Store(self.cfg.database) as store:
                store.job_record("schedule", True, f"skipped: busy ({e})")
            return True

    def _locked_tick(self, store: Store, games: list[GameConfig], remote: dict[str, date | None],
                     expected: date, expired: list[GameConfig]) -> bool:
        ok, ran, notes = True, False, []
        for r in store.close_interrupted():
            ok = False
            notes.append(f"closed interrupted {r['kind']} {r['dataset_date'] or ''} {r['game'] or ''}".rstrip()
                         + (", not auto-retried (REL-1)" if r["kind"] == "check" else ""))
        for game in games:
            r_date = remote[game.key]
            if r_date is None:
                continue
            g_ok, g_ran, g_notes = self._workflow(store, game, r_date)
            ok, ran = ok and g_ok, ran or g_ran
            notes += g_notes
        if expired:
            ok = False
            seen = sorted({str(remote[g.key]) if remote[g.key] else "unreachable" for g in expired})
            notes.append(f"stale: remote {', '.join(seen)}, expected {expected}, window expired")
        dates = [d for d in remote.values() if d]
        if ok and not ran:
            notes.append("nothing to do")
        note = "; ".join(notes)
        self.log(f"tick {'ok' if ok else 'FAILED'}: {note}")
        store.job_record("schedule", ok, note, max(dates) if dates else None)
        return ok

    # --- per-dataset workflow (SCHEDULER.md 3.2) -------------------------------

    def _workflow(self, store: Store, game: GameConfig, day: date) -> tuple[bool, bool, list[str]]:
        """Idempotent, resumable: returns (ok, ran_anything, notes)."""
        source = self.source_for(game)
        ok, ran, notes = True, False, []
        snapshotted = False

        # One-shot repair (SCHEDULER.md 4.1): only a closed ok=1 `repair` row for this game suppresses it.
        if self.repair_fn is not None and not store.job_ok_any("repair", game.key):
            ran = True
            n = self.cfg.catch_up_days
            if not self._stage(store, "repair", day, game, lambda: self.repair_fn(game.key, n),
                               retry=False, notes=notes):
                ok = False  # retried on the next tick/start; the normal workflow continues below

        if not store.has_prices(game.key, day):
            ran = True
            if not self._stage(store, "snapshot", day, game, lambda: self.snapshot_fn(store, source, game),
                               retry=True, notes=notes):
                return False, ran, notes  # no data for `day`: nothing more to do for this game
            snapshotted = True

        day = self._reconcile(store, game, day, notes)

        n = self.cfg.catch_up_days
        missing = [d for i in range(1, n + 1) if not store.has_prices(game.key, d := day - timedelta(days=i))]
        if missing:
            ran = True
            if self._stage(store, "catchup", day, game, lambda: self.backfill_fn(game.key, n),
                           retry=True, notes=notes):
                left = [d for d in missing if not store.has_prices(game.key, d)]
                if left:
                    notes.append(f"{game.key} catch-up: no archive for {', '.join(map(str, left))}")
            else:
                ok = False  # still try the check below with the history we have

        # Catch-up (even a failed one) may itself have stored a newer publication: re-key before the check.
        day = self._reconcile(store, game, day, notes)

        previous = store.job_rows("check", day, game.key)
        if previous:  # attempted already (ok, failed or interrupted): never auto-retried
            if not any(r["ok"] == 1 for r in previous):
                ok = False  # stays failed on every restart/tick until a check succeeds (SCHEDULER.md 3.2/R6)
                notes.append(f"{game.key} check {day} failed earlier, not auto-retried; rerun `tcgwatch check`")
        else:
            ran = True
            ok = self._stage(store, "check", day, game, lambda: self.check_fn(game.key),
                             retry=False, notes=notes) and ok
        if snapshotted and self.cfg.retention_days is not None:  # RET-1: after a successful snapshot, last
            ok = self._stage(store, "prune", day, game, lambda: self._prune(store, game),
                             retry=False, notes=notes) and ok
        return ok, ran, notes

    def _prune(self, store: Store, game: GameConfig) -> str:
        r = prune_history(store, self.cfg, [game], windows={game.key: self.window_for(game.key)} if self.window_for else None)
        store.compact()  # cheap PRAGMA optimize; the full VACUUM is left to `tcgwatch prune`
        msg = f"removed {r.prices} prices, {r.cards} cards, {r.alerts} alerts, {r.job_runs} job runs"
        self.log(f"{game.key} prune: {msg}")
        return msg

    @staticmethod
    def _reconcile(store: Store, game: GameConfig, day: date, notes: list[str]) -> date:
        """The publication may have advanced since the gate (lock wait, slow fetch, catch-up's own snapshot):
        the check processes the newest stored dataset, so key the workflow row under that same day."""
        latest = store.latest_snapshot_date(game.key)
        if latest is not None and latest > day:
            notes.append(f"{game.key} dataset advanced {day} -> {latest}")
            return latest
        return day

    def _stage(self, store: Store, kind: str, day: date, game: GameConfig, action: Callable,
               *, retry: bool, notes: list[str]) -> bool:
        """Persist a start row (committed before any side effect), run, then close it."""
        run_id = store.job_start(kind, day, game.key)
        attempts = 1 + (len(self.retry_delays) if retry else 0)
        for attempt in range(attempts):
            try:
                result = action()
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                self.log(f"{game.key} {kind} {day} attempt {attempt + 1}/{attempts} failed: {msg}")
                if attempt + 1 < attempts:
                    self.sleep(self.retry_delays[attempt])
                    continue
                store.job_finish(run_id, False, f"failed after {attempts} attempt(s): {msg}")
                notes.append(f"{game.key} {kind} {day} failed: {msg}")
                return False
            store.job_finish(run_id, True, result if isinstance(result, str) else
                             (f"attempt {attempt + 1}" if attempt else None))
            return True
        return False  # pragma: no cover


class Pruned(NamedTuple):
    prices: int
    cards: int
    notes: list[str]
    alerts: int = 0
    job_runs: int = 0


def prune_history(store: Store, cfg: AppConfig, games: list[GameConfig], *,
                  dry_run: bool = False, windows: dict[str, int] | None = None) -> Pruned:
    """Apply `retention_days` per game, relative to that game's newest stored day (never the wall clock,
    so a stalled feed cannot eat its own history). Alerts older than the cutoff go too, but never any
    still inside the cooldown window (the largest configured cooldown). `job_runs` older than the
    earliest cutoff go, except the newest row of each kind. Returns counts plus per-game notes."""
    n_prices = n_cards = n_alerts = 0
    notes: list[str] = []
    cutoffs: dict[str, date] = {}
    live: dict[str, date] = {}
    cooldown = max(r.effective_cooldown_days for r in [cfg.alert, *(g.alert for g in cfg.games.values())])
    for g in games:
        days, latest = cfg.retention_days_for(g, (windows or {}).get(g.key)), store.latest_snapshot_date(g.key)
        if days is None or latest is None:
            continue
        cutoff = latest - timedelta(days=days)
        cutoffs[g.key], live[g.key] = cutoff, latest
        p, c, a = store.prune_game(g.key, cutoff, dry_run=dry_run, alerts_cutoff=min(cutoff, latest - timedelta(days=cooldown)))
        n_prices, n_cards, n_alerts = n_prices + p, n_cards + c, n_alerts + a
        notes.append(f"{g.key}: {p} prices, {c} cards, {a} alerts before {cutoff}")
    n_jobs = store.prune_job_runs(cutoffs, dry_run=dry_run, live_from=live)
    return Pruned(n_prices, n_cards, notes, n_alerts, n_jobs)


def health_check(cfg: AppConfig, max_age: timedelta | None = None) -> tuple[bool, str]:
    """Is the scheduler alive? Shared by `tcgwatch health` and GET /api/status.

    Healthy = an ok `schedule` tick (skips count) finished within `max_age` (default: 24h cadence +
    the late-publication window + 1h margin). Never creates the database."""
    if max_age is None:
        max_age = timedelta(hours=24) + cfg.late_window + timedelta(hours=1)
    try:
        with Store(cfg.database, readonly=True) as store:
            row = store.last_job("schedule", ok=True)
    except sqlite3.DatabaseError as e:
        if is_missing_db(e, cfg.database):
            return False, f"no database at {cfg.database}"
        return False, f"database unreadable: {e}"
    if row is None:
        return False, "no ok schedule run recorded"
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["finished"])
    if age > max_age:
        return False, f"last ok schedule run {row['finished']} is older than {max_age}"
    return True, f"last ok schedule run {row['finished']}"

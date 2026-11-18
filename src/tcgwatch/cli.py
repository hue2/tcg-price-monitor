"""Command-line entry point: games, sets, snapshot, backfill, check, run, schedule (watch), web, discord-test."""

from __future__ import annotations

import argparse
import errno
import json
import os
import sqlite3
import sys
import tempfile
import time
import traceback
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import AlertRule, AppConfig, ConfigError, load_config, parse_days, parse_duration, parse_sets
from .filters import CardFilter
from .joblock import JobLockedError, holder_path, job_lock, lock_path
from .models import Mover, Unpaired
from .movement import compare, find_movers_ex
from .notify import DiscordPartialError, describe_rule, post_discord, print_console, print_watchlist, send_discord, write_json
from .scheduler import Scheduler
from .sources import build_source
from .store import Store, is_missing_db
from .textsafe import term_safe
from .watchlist import validate_watch


def _rule_overrides(args: argparse.Namespace, rule: AlertRule) -> AlertRule:
    changes = {}
    if args.window:
        try:
            changes["window_days"] = parse_days(args.window)
        except ConfigError as e:
            raise ConfigError(f"--window: {e}") from None
    if args.threshold is not None:
        changes["threshold_pct"] = args.threshold
    if args.direction:
        changes["direction"] = args.direction
    if args.min_price is not None:
        changes["min_price"] = args.min_price
    if args.discord_threshold is not None:
        changes["discord_threshold_pct"] = args.discord_threshold
    return replace(rule, **changes).validated() if changes else rule


def _fmt_sets(sets: tuple[int | str, ...]) -> str:
    return term_safe(", ".join(str(s) for s in sets) or "all")


def cmd_games(cfg: AppConfig, args: argparse.Namespace) -> int:
    discord = "on" if cfg.discord_webhook else "off"
    with Store(cfg.database) as store:
        for g in cfg.games.values():
            category = g.category_id if g.category_id is not None else f"name={g.category_name!r}"
            print(f"{term_safe(g.key):<12} {'on ' if g.enabled else 'off'}  {term_safe(g.name)}  (category {term_safe(category)}, source {term_safe(g.source)})")
            print(f"{'':<17}rarities: {term_safe(', '.join(g.rarities) or 'all')}"
                  f" | printings: {term_safe(', '.join(g.printings) or 'all')}"
                  f" | {len(g.exclude_name_patterns)} variant exclusion(s)")
            print(f"{'':<17}sets: {_fmt_sets(g.include_sets)}"
                  + (f" | excluding: {_fmt_sets(g.exclude_sets)}" if g.exclude_sets else ""))
            print(f"{'':<17}rule: {describe_rule(g.alert)} | history: {store.snapshot_days(g.key)} day(s)")
            print(f"{'':<17}discord ({discord}): posts moves >= {g.alert.effective_discord_threshold:g}%")
    return 0


def cmd_sets(cfg: AppConfig, args: argparse.Namespace) -> int:
    """List sets seen in snapshots, so they can be copied into include_sets / exclude_sets."""
    with Store(cfg.database) as store:
        for game in cfg.selected_games(args.game):
            rows = store.sets(game.key)
            print(f"\n== {term_safe(game.name)} ({term_safe(game.key)}): {len(rows)} set(s)")
            if not rows:
                print("  none yet - run `tcgwatch snapshot` first")
            card_filter = CardFilter.for_game(game)
            for r in rows:
                mark = "+" if card_filter.accepts_set(r["group_id"], r["group_name"]) else "-"
                print(f"  {mark} {r['group_id']:>6}  {term_safe(r['group_name'])}  ({r['singles']} singles)")
    print("\n+ = checked, - = filtered out by include_sets / exclude_sets")
    return 0


def cmd_discord_test(cfg: AppConfig, args: argparse.Namespace) -> int:
    if not cfg.discord_webhook:
        print("no Discord webhook configured (notify.discord.webhook_url or webhook_env)", file=sys.stderr)
        return 1
    post_discord(cfg.discord_webhook, "tcg-price-watch: test message, the webhook works.")
    print("posted test message to Discord")
    return 0


SNAPSHOT_ATTEMPTS = 3


# Upper bound for `backfill --force`: each day is a ~4 MB archive download plus a full rewrite.
# Matches the scheduler's catch_up_days cap (docs/SCHEDULER.md).
MAX_FORCE_DAYS = 30


class SnapshotError(RuntimeError):
    pass


def _publication(source):
    """Full publication token for stability checks (falls back to the date if unsupported)."""
    published_at = getattr(source, "published_at", None)
    return published_at() if published_at is not None else source.snapshot_date()


def _snapshot_game(store: Store, source, game, *, replace: bool = False, quiet: bool = False) -> None:
    """Fetch every set into memory, then store the game in one transaction.

    A failure mid-fetch writes nothing, so a partial day is never mistaken for a complete
    one. If the source publishes a new dataset during the fetch (the full last-updated.txt
    timestamp changes, even within the same day), the mixed result is discarded and refetched.
    Only the date of the publication is used as the DB snapshot key.
    """
    for _ in range(SNAPSHOT_ATTEMPTS):
        token = _publication(source)
        batches = list(source.fetch_singles(game))
        if _publication(source) == token:
            break
        if not quiet:
            print(f"{term_safe(game.name)}: source updated during fetch, refetching", flush=True)
    else:
        raise SnapshotError(f"{term_safe(game.name)}: source kept changing during fetch, nothing stored")
    snap = token.date() if isinstance(token, datetime) else token
    if replace and not any(points for _, points in batches):
        # Never replace a stored day with nothing: an empty (but stable) response would
        # otherwise DELETE the game's prices. Keep them; still upsert any cards returned.
        n_cards, _ = store.write_snapshot(snap, batches)
        if not quiet:
            print(f"{term_safe(game.name)}: kept stored prices for {snap} (live snapshot has no prices, "
                  f"{n_cards} singles)", flush=True)
        return
    n_cards, n_prices = store.write_snapshot(snap, batches, replace_game=game.key if replace else None)
    if not quiet:
        print(f"{term_safe(game.name)}: stored {n_prices} prices for {n_cards} singles (snapshot {snap})", flush=True)


def _lock_wait(args: argparse.Namespace) -> float:
    return float(getattr(args, "lock_wait", 0) or 0)


def cmd_snapshot(cfg: AppConfig, args: argparse.Namespace) -> int:
    with job_lock(cfg.database, "snapshot", _lock_wait(args)):
        return _snapshot(cfg, args)


def _snapshot(cfg: AppConfig, args: argparse.Namespace) -> int:
    with Store(cfg.database) as store:
        for game in cfg.selected_games(args.game):
            _snapshot_game(store, build_source(game.source, cfg), game, quiet=getattr(args, "quiet", False))
    return 0


def cmd_watchlist(cfg: AppConfig, args: argparse.Namespace) -> int:
    if args.wl_action == "list":
        games = cfg.selected_games(args.game)
        try:
            with Store(cfg.database, readonly=True) as store:
                rows = [(g, r) for g in games for r in store.watched(g.key)]
        except sqlite3.DatabaseError as e:
            if not is_missing_db(e, cfg.database):
                print(f"error: cannot read watchlist from {term_safe(cfg.database)}: {term_safe(e)}", file=sys.stderr)
                return 1
            rows = []  # no database yet: nothing watched
        if not rows:
            print("watchlist is empty")
        for g, r in rows:
            print(f"{r['product_id']}\t{term_safe(r['printing'])}\t{term_safe(g.key)}\t{term_safe(r['name'])}\t"
                  f"{term_safe(r['group_name'])}\t{term_safe(r['added_at'])}")
        return 0
    if (err := validate_watch(args.product_id, args.printing)):
        print(f"error: {err}", file=sys.stderr)
        return 2
    with job_lock(cfg.database, f"watchlist-{args.wl_action}", _lock_wait(args)):
        with Store(cfg.database) as store:
            if args.wl_action == "add":
                if not store.watch(args.product_id, args.printing):
                    print("error: no prices for this printing", file=sys.stderr)
                    return 1
                print(f"watching {args.product_id} ({term_safe(args.printing)})")
            else:
                if not store.unwatch(args.product_id, args.printing):
                    print(f"{args.product_id} ({term_safe(args.printing)}) was not on the watchlist", file=sys.stderr)
                    return 1
                print(f"removed {args.product_id} ({term_safe(args.printing)})")
    return 0


def _int_range(lo: int, hi: int):
    def conv(text: str) -> int:
        try:
            n = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid integer: {text!r}") from None
        if not lo <= n <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}")
        return n
    return conv


def _game_key(text: str) -> str:
    import re
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", text):
        raise argparse.ArgumentTypeError(f"invalid game key: {text!r}")
    return text


def cmd_alerts(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Alert history, newest first; same filters and ranges as GET /api/alerts. Read-only, never creates the DB."""
    since = datetime.now(timezone.utc).date() - timedelta(days=args.days) if args.days else None
    try:
        with Store(cfg.database, readonly=True) as store:
            rows = [dict(r) for r in store.recent_alerts(args.game, since, args.discord, args.limit)]
    except sqlite3.DatabaseError as e:
        if not is_missing_db(e, cfg.database):
            print(f"error: cannot read alerts from {term_safe(cfg.database)}: {term_safe(e)}", file=sys.stderr)
            return 1
        print("no database yet")
        return 0
    if not rows:
        print("no alerts")
        return 0
    print("date\tgame\tproduct_id\tprinting\tname\twindow\tbaseline\tcurrent\tchange%\tdiscord")
    for r in rows:
        print(f"{term_safe(r['alerted_on'])}\t{term_safe(r['game'] or '?')}\t{r['product_id']}\t"
              f"{term_safe(r['printing'])}\t{term_safe(r['name'] or '?')}\t"
              f"{r['window_days']}d\t{r['baseline']:.2f}\t{r['current']:.2f}\t{r['pct_change']:+.1f}\t"
              f"{'yes' if r['discord'] else 'no'}")
    return 0


def _kind(text: str) -> str:
    import re
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", text):
        raise argparse.ArgumentTypeError(f"invalid job kind: {text!r}")
    return text


def cmd_runs(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Job history, newest first; mirrors GET /api/job-runs. Read-only, never creates the DB."""
    try:
        with Store(cfg.database, readonly=True) as store:
            rows = [dict(r) for r in store.recent_jobs(args.kind, args.limit)]
    except sqlite3.DatabaseError as e:
        if not is_missing_db(e, cfg.database):
            print(f"error: cannot read job runs from {term_safe(cfg.database)}: {term_safe(e)}", file=sys.stderr)
            return 1
        rows = []
    if args.json:
        print(json.dumps(rows))  # raw rows, identical to the API body
        return 0
    if not rows:
        print("no job runs yet")
        return 0
    print("id\tkind\tgame\tstarted\tfinished\tok\tdataset_date\tnote")
    for r in rows:  # `game` is absent on a C1-era table (readonly never migrates), so .get
        ok = "?" if r["ok"] is None else ("yes" if r["ok"] else "no")
        print("\t".join([str(r["id"]), term_safe(r["kind"]), term_safe(r.get("game") or "-"), term_safe(r["started"]),
                         term_safe(r["finished"] or "-"), ok, term_safe(r["dataset_date"] or "-"),
                         term_safe(r["note"] or "")]))
    return 0


def cmd_backfill(cfg: AppConfig, args: argparse.Namespace) -> int:
    with job_lock(cfg.database, "backfill", _lock_wait(args)):
        return _backfill(cfg, args)


def _repair_game(cfg: AppConfig, key: str, days: int) -> None:
    """Scheduler's one-shot repair: `backfill --force` for one game; a non-zero exit is a failure.

    ``days`` is capped at MAX_FORCE_DAYS: schedule.catch_up_days may be 1-90 for ordinary catch-up,
    but a forced rewrite is bounded, so repair covers min(catch_up_days, 30) days."""
    rc = _backfill(cfg, argparse.Namespace(game=[key], days=min(days, MAX_FORCE_DAYS), force=True))
    if rc:
        raise RuntimeError(f"backfill --force exited {rc}")


def _backfill(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Fill the last N days from TCGCSV archives so checks work without waiting a week.

    ``--force`` is the bounded repair for days stored by pre-atomic versions (possibly partial):
    it re-fetches the latest snapshot and every archived day in range and rewrites each
    game/day in one transaction, replacing the stored rows. It never touches Discord.
    """
    if args.days < 1:
        raise ConfigError("--days must be at least 1")
    if args.force and args.days > MAX_FORCE_DAYS:
        raise ConfigError(f"--force rewrites at most {MAX_FORCE_DAYS} days (got --days {args.days})")
    with Store(cfg.database) as store:
        games, source = [], None
        for game in cfg.selected_games(args.game):
            src = build_source(game.source, cfg)
            if not hasattr(src, "archive_prices"):
                print(f"{term_safe(game.name)}: source {game.source!r} has no history archive, skipping")
                continue
            source = source or src
            games.append(game)
        if not games:
            return 0
        latest = source.snapshot_date()
        # Archives carry prices only, so the card catalog must come from a current snapshot.
        for game in games:
            if args.force or store.latest_snapshot_date(game.key) != latest:
                _snapshot_game(store, source, game, replace=args.force)
        categories = {g.key: source.resolve_category(g) for g in games}
        for back in range(1, args.days + 1):
            day = latest - timedelta(days=back)
            needed = [g for g in games if args.force or not store.has_prices(g.key, day)]
            if not needed:
                print(f"{day}: already stored")
                continue
            by_category = source.archive_prices(day, {categories[g.key] for g in needed})
            if by_category is None:
                print(f"{day}: no archive available")
                continue
            parts = []
            for g in needed:
                known = store.product_ids(g.key)
                points = [p for p in by_category[categories[g.key]] if p.product_id in known]
                if args.force and not points:
                    # Never replace a stored day with nothing: keep what is there.
                    parts.append(f"{term_safe(g.name)} kept (archive has no prices)")
                    continue
                _, n = store.write_snapshot(day, [([], points)],
                                            replace_game=g.key if args.force else None)
                parts.append(f"{term_safe(g.name)} {n}")
            verb = "rewrote" if args.force else "stored"
            print(f"{day}: {verb} prices - {', '.join(parts)}", flush=True)
    return 0


def cmd_web(cfg: AppConfig, args: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import allowed_hosts, create_app, is_loopback

    token = cfg.web_token
    if not is_loopback(args.host) and not token:
        if not args.insecure_no_auth:
            print(f"error: refusing to bind {args.host}: the app (and your Discord webhook) would be reachable "
                  "without authentication. Set web.auth_token in config.yaml or TCGWATCH_WEB_TOKEN, "
                  "or pass --insecure-no-auth if the port is only published on loopback (e.g. Docker).",
                  file=sys.stderr)
            return 2
        print(f"WARNING: --insecure-no-auth: {args.host} is served without authentication.", file=sys.stderr)
    hosts = allowed_hosts(args.host, args.allowed_host)
    print(f"TCG Price Watch on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(args.config, hosts, token), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_check(cfg: AppConfig, args: argparse.Namespace) -> int:
    with job_lock(cfg.database, "check", _lock_wait(args)):
        return _check(cfg, args)


class DeliveryFailed(Exception):
    """Discord delivery failed (total or partial). Delivered movers are already recorded."""


def _check(cfg: AppConfig, args: argparse.Namespace, *, strict: bool = False) -> int:
    """strict (scheduler): raise DeliveryFailed after accounting, so the check row closes ok=0."""
    failures: list[str] = []
    all_movers: list[Mover] = []
    all_watched: list[Mover] = []
    all_unpaired: list[Unpaired] = []
    webhook = None if args.preview else cfg.discord_webhook
    sets = parse_sets(args.set, "--set")

    def key(m: Mover) -> tuple[int, str]:
        return m.card.product_id, m.printing

    with Store(cfg.database) as store:
        for game in cfg.selected_games(args.game):
            rule = _rule_overrides(args, game.alert)
            current = store.latest_snapshot_date(game.key)
            if current is None:
                print(f"{term_safe(game.name)}: no snapshots yet - run `tcgwatch snapshot` first")
                continue
            card_filter = CardFilter.for_game(game, all_rarities=args.all_rarities, sets=sets)
            all_obs = list(store.observations(game.key, rule.window_days))
            observations = [o for o in all_obs if card_filter.accepts(o.card, o.printing)]
            # Watched cards bypass every filter: always reported, posted only past the Discord threshold.
            watched_rows = store.watched(game.key)
            watched_keys = {(r["product_id"], r["printing"]) for r in watched_rows}
            watch_movers = sorted((m for o in all_obs if (o.card.product_id, o.printing) in watched_keys
                                   and (m := compare(o, rule))),
                                  key=lambda m: abs(m.pct_change), reverse=True)
            have = {key(m) for m in watch_movers}
            no_baseline = [u for u in store.watched_current(game.key, rule.price_fields)
                           if (u.card.product_id, u.printing) not in have]
            all_unpaired.extend(no_baseline)
            all_watched.extend(watch_movers)
            if watched_rows and cfg.console and not args.quiet:
                print_watchlist(game, rule, watch_movers, no_baseline)
            if not observations and not watched_rows:
                print(f"{term_safe(game.name)}: nothing to compare for a {rule.window_days}d window "
                      f"({store.snapshot_days(game.key)} snapshot day(s) stored; check sets/filters)")
                continue
            movers, skipped = find_movers_ex(observations, rule)
            if skipped and cfg.console and not args.quiet:
                print(f"{term_safe(game.name)}: skipped {skipped} card(s) with a noisy mid fallback "
                      f"(high/low > {rule.max_fallback_spread:g})")

            # Console and Discord keep separate cooldowns, so a card first flagged below the
            # Discord threshold still gets posted if it later climbs past it.
            use_cooldown = not args.preview and rule.effective_cooldown_days > 0
            since = current - timedelta(days=rule.effective_cooldown_days - 1)
            seen = store.recently_alerted(since) if use_cooldown else set()
            new = [m for m in movers if key(m) not in seen]
            if cfg.console and not args.quiet:
                print_console(game, rule, new)

            posted: list[Mover] = []
            if webhook:
                already = store.recently_alerted(since, discord=True) if use_cooldown else set()
                to_post = [m for m in movers if abs(m.pct_change) >= rule.effective_discord_threshold
                           and key(m) not in already]
                queued = {key(m) for m in to_post}
                to_post += [m for m in watch_movers if abs(m.pct_change) >= rule.effective_discord_threshold
                            and key(m) not in already and key(m) not in queued]
                try:
                    send_discord(webhook, game, rule, to_post,
                                 **({"fmt": cfg.discord_format} if cfg.discord_format != "text" else {}))
                    posted = to_post
                    if to_post and not args.quiet:
                        print(f"  posted {len(to_post)} to Discord (>= {rule.effective_discord_threshold:g}%)")
                except DiscordPartialError as e:  # record only what was really delivered
                    posted = e.posted
                    print(f"{term_safe(game.name)}: Discord post failed after {len(posted)} of {len(to_post)} "
                          f"card(s): {term_safe(e)}", file=sys.stderr)
                    failures.append(f"{game.key}: {len(posted)} of {len(to_post)} delivered: {e}")
                except OSError as e:  # network/HTTP errors; don't lose the console results
                    print(f"{term_safe(game.name)}: Discord post failed: {term_safe(e)}", file=sys.stderr)
                    failures.append(f"{game.key}: {e}")

            if not args.preview:
                posted_keys = {key(m) for m in posted}
                store.record_alerts([m for m in new if key(m) not in posted_keys], current, rule.window_days)
                store.record_alerts(posted, current, rule.window_days, discord=True)
            all_movers.extend(new)
    if args.json:
        write_json(Path(args.json), all_movers, all_watched, all_unpaired)
    if strict and failures:
        raise DeliveryFailed("Discord delivery failed: " + "; ".join(failures))
    return 0


def cmd_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    with job_lock(cfg.database, "run", _lock_wait(args)):
        cmd_snapshot(cfg, args)
        return cmd_check(cfg, args)


def _interval_loop(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Deprecated fixed-interval mode (schedule.check_every): run, sleep, repeat."""
    interval = cfg.check_every.total_seconds()
    print("WARNING: schedule.check_every is deprecated; remove it to use the wall-clock `schedule.at` "
          "scheduler (daily, late-publish aware, resumable).", file=sys.stderr)
    print(f"watching every {cfg.check_every}; Ctrl+C to stop")
    while True:
        try:
            cmd_run(cfg, args)
        except JobLockedError as e:  # another job holds the DB: skip this tick
            print(f"skipping tick: {term_safe(e)}")
        except Exception:  # keep the loop alive across transient network errors
            traceback.print_exc()
        time.sleep(interval)


def _scheduled_check_args(args: argparse.Namespace, game_key: str) -> argparse.Namespace:
    """The user's check options (threshold, window, sets, ...) applied to one game's scheduled check."""
    return argparse.Namespace(**{**vars(args), "game": [game_key]})


def cmd_schedule(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Daily at `schedule.at` (UTC): snapshot, catch-up, check; see docs/SCHEDULER.md."""
    if cfg.check_every is not None:
        return _interval_loop(cfg, args)
    if args.preview:  # a preview never posts or records, so it cannot be a standing daily job
        raise ConfigError("--preview is not supported by `schedule`/`watch`; use `tcgwatch check --preview`")
    # Validate the effective rules (incl. --window/--threshold overrides) BEFORE any side effect, and keep
    # each game's effective window: it floors retention so pruning never deletes the override's baseline.
    windows = {g.key: _rule_overrides(args, g.alert).window_days for g in cfg.selected_games(args.game)}
    sources: dict[str, object] = {}

    def source_for(game):
        if game.source not in sources:
            sources[game.source] = build_source(game.source, cfg)
        return sources[game.source]

    Scheduler(
        cfg,
        snapshot_fn=lambda store, source, game: _snapshot_game(store, source, game),
        backfill_fn=lambda key, days: _backfill(cfg, argparse.Namespace(game=[key], days=days, force=False)),
        repair_fn=lambda key, days: _repair_game(cfg, key, days),
        check_fn=lambda key: _check(cfg, _scheduled_check_args(args, key), strict=True),
        game_keys=args.game,
        window_for=lambda key: windows.get(key, 0),
        source_for=source_for,
        lock_wait=_lock_wait(args) or 600.0,
    ).run_forever()
    return 0


cmd_watch = cmd_schedule  # alias kept for existing scripts


def cmd_prune(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Delete price history beyond `retention_days` (see SPEC 6/8). `--dry-run` only reports."""
    from .scheduler import Pruned, prune_history
    if cfg.retention_days is None:
        print("retention_days is off (0/null): nothing to prune")
        return 0
    games = cfg.selected_games(args.game)
    if args.dry_run:  # a report changes nothing: read-only open never creates, migrates or switches journal mode
        try:
            with Store(cfg.database, readonly=True) as store:
                r = prune_history(store, cfg, games, dry_run=True)
        except sqlite3.DatabaseError as e:
            if not is_missing_db(e, cfg.database):  # only a truly absent DB reads as empty
                print(f"error: cannot read {term_safe(cfg.database)}: {term_safe(e)}", file=sys.stderr)
                if str(e).startswith("no such table:"):  # older schema; dry-run is read-only so it cannot upgrade it
                    print("hint: --dry-run opens the database read-only, so it cannot create or migrate missing "
                          "tables. Run `tcgwatch games` once (it migrates the "
                          "schema offline, no live fetch), then retry `prune --dry-run`. Nothing was changed.", file=sys.stderr)
                return 1
            r = Pruned(0, 0, [])
        print("\n".join(f"would remove {term_safe(n)}" for n in r.notes) or "nothing stored")
        print(f"dry run: {r.prices} prices, {r.cards} cards, {r.alerts} alerts, {r.job_runs} job runs would be removed")
        return 0
    with job_lock(cfg.database, "prune", _lock_wait(args)):
        with Store(cfg.database) as store:
            if store.readonly:
                print("database is read-only; cannot prune", file=sys.stderr)
                return 1
            run_id = store.job_start("prune")
            try:
                r = prune_history(store, cfg, games)
                store.compact(vacuum=True)  # we hold the job lock, so the VACUUM can't race another job
            except Exception as e:
                store.job_finish(run_id, False, f"{type(e).__name__}: {e}")
                raise
            store.job_finish(run_id, True, f"removed {r.prices} prices, {r.cards} cards, "
                                           f"{r.alerts} alerts, {r.job_runs} job runs")
    print("\n".join(f"removed {term_safe(n)}" for n in r.notes))
    print(f"pruned {r.prices} prices, {r.cards} cards, {r.alerts} alerts, {r.job_runs} job runs")
    return 0


def cmd_backup(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Online, WAL-safe copy of the database via sqlite3's backup API (SPEC 8). Takes the job lock."""
    src, dest = Path(cfg.database), Path(args.dest)
    if str(cfg.database) == ":memory:":
        print("error: cannot back up an in-memory database", file=sys.stderr)
        return 1
    if not src.exists():
        print(f"error: no database at {term_safe(src)}; nothing to back up", file=sys.stderr)
        return 1
    live = os.path.realpath(src)
    reserved = {live, f"{live}-wal", f"{live}-shm", f"{live}-journal",
                str(lock_path(src)), str(holder_path(src))}  # live DB, its sidecars and the job lock files
    if os.path.realpath(dest) in reserved:
        print(f"error: destination {term_safe(dest)} is the live database or one of its sidecar/lock files", file=sys.stderr)
        return 2
    if dest.is_dir():
        print(f"error: {term_safe(dest)} is a directory", file=sys.stderr)
        return 2
    exists_msg = f"error: {term_safe(dest)} already exists; pass --force to overwrite"
    if os.path.lexists(dest) and not args.force:
        print(exists_msg, file=sys.stderr)
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    with job_lock(cfg.database, "backup", _lock_wait(args)):  # no job writes while we copy
        if os.path.lexists(dest) and not args.force:  # it may have appeared while we waited for the lock
            print(exists_msg, file=sys.stderr)
            return 1
        # exclusive, unpredictable, 0600-from-creation temp (O_EXCL never follows a planted symlink)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            ro = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
            try:
                out = sqlite3.connect(str(tmp))
                try:
                    ro.backup(out)
                    out.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm sidecars
                    if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise sqlite3.DatabaseError("integrity_check failed on the copy")
                finally:
                    out.close()
            finally:
                ro.close()
            os.chmod(tmp, 0o600)
            if args.force:
                os.replace(tmp, dest)  # atomic: a failed backup never clobbers an existing good one
            else:
                try:
                    published = _publish_no_clobber(tmp, dest)
                except OSError as e:  # no atomic no-replace primitive here: fail safe, never touch dest
                    print(f"error: cannot publish {term_safe(dest)} atomically without overwriting ({term_safe(e.strerror or e)}); "
                          "use a filesystem with hard links, or pass --force", file=sys.stderr)
                    return 2
                if not published:
                    print(exists_msg, file=sys.stderr)
                    return 1
        finally:
            tmp.unlink(missing_ok=True)
    print(f"backed up {term_safe(src)} -> {term_safe(dest)} ({dest.stat().st_size} bytes)")
    return 0


_HAS_DIR_FSYNC = os.name != "nt"  # Windows cannot open a directory for fsync: skipped deliberately


def _open_dir_fd(path: Path) -> int | None:
    """fd of a directory for fsync, or None where directories cannot be opened (Windows: no dir fsync)."""
    if not _HAS_DIR_FSYNC:
        return None
    return os.open(path, os.O_RDONLY)


_RESTORE_SUFFIXES = ("", "-wal", "-shm", "-journal")


class _RollbackIncomplete(OSError):
    """A restore step failed AND putting the old files back failed: `pending` are the (original, aside) pairs still aside."""

    def __init__(self, pending: list[tuple[str, str]]):
        super().__init__(errno.EIO, "rollback incomplete")
        self.pending = pending


def _restore_aside(live: str, main: bool) -> tuple[str, list[tuple[str, str]]]:
    """Move the old DB and its sidecars to the recoverable set `<db>.pre-restore[-wal|-shm|-journal]`.

    Renames are atomic and keep the files' relative names. `main=False` (no --force) leaves the main file alone,
    so a file that appeared meanwhile is never moved. Returns (aside base, [(original, aside)...]) so a failure can
    roll back; a failed rollback raises _RollbackIncomplete naming what is still aside.
    """
    base = f"{live}.pre-restore"
    moved: list[tuple[str, str]] = []
    try:
        for suf in _RESTORE_SUFFIXES if main else _RESTORE_SUFFIXES[1:]:
            if os.path.lexists(live + suf):
                os.replace(live + suf, base + suf)
                moved.append((live + suf, base + suf))
    except OSError as e:
        pending = _restore_rollback(moved)
        if pending:
            raise _RollbackIncomplete(pending) from e
        raise
    return base, moved


def _restore_rollback(moved: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Put the aside files back. Returns the (original, aside) pairs that are STILL aside (empty = fully restored).

    Sidecars go back first and the main file last, and only if every sidecar made it: a main file is never put
    back under the live name without its WAL."""
    pending: list[tuple[str, str]] = []
    for orig, aside in reversed(moved):
        if orig == moved[0][0] and pending:  # main file: its sidecars did not all return, leave it aside too
            pending.append((orig, aside))
            continue
        try:
            os.replace(aside, orig)
        except OSError:
            pending.append((orig, aside))
    return pending


def _rollback_failed_msg(what: str, pending: list[tuple[str, str]]) -> str:
    names = ", ".join(term_safe(a) for _, a in pending)
    return (f"error: {what}, and putting the old database back FAILED: the live database is NOT fully restored. "
            f"Nothing was deleted; the old files are still at: {names}. Keep the web app and scheduler STOPPED "
            "and rename each of these back to its original name (drop the .pre-restore) before starting anything "
            "(README 'Recover from an interrupted restore')")


_CORRUPT_CODES = frozenset({11, 26})  # SQLITE_CORRUPT, SQLITE_NOTADB (primary codes)


def _restore_checkpoint(live: str) -> tuple[int, str]:
    """Fold the live WAL into the main file so a split live/aside state never strands committed rows.

    Fails CLOSED: returns (exit_code, reason), 0 meaning go ahead. Busy/incomplete -> 3; any other operational
    failure (read-only file, I/O error, disk full, cannot open) -> 2. The ONLY skip is a live file SQLite itself
    reports as corrupt or not a database (SQLITE_CORRUPT/SQLITE_NOTADB), the usual reason to restore."""
    if not os.path.lexists(live):
        return 0, ""
    c = None
    try:
        c = sqlite3.connect(f"{Path(live).resolve().as_uri()}?mode=rw", uri=True, timeout=5)
        busy, log, done = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy == 0 and log == done:
            return 0, ""
        return 3, "its WAL could not be fully checkpointed (database in use)"
    except sqlite3.Error as e:
        code = (getattr(e, "sqlite_errorcode", None) or 0) & 0xFF
        if code in _CORRUPT_CODES:
            return 0, ""
        if code in (5, 6):  # SQLITE_BUSY, SQLITE_LOCKED
            return 3, "its WAL could not be fully checkpointed (database in use)"
        return 2, f"its WAL could not be checkpointed ({term_safe(e)})"
    finally:
        if c is not None:
            c.close()


def cmd_restore(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Replace the database with a backup (inverse of `backup`, SPEC 8). Takes the job lock.

    The web app does not hold the job lock: stop it first (docs say so). The old database and its sidecars
    are never deleted before the new file is published: they are renamed to `<db>.pre-restore*` and removed
    only once the restored file and its directory entry are durable (a crash leaves a recoverable set)."""
    if str(cfg.database) == ":memory:":
        print("error: cannot restore into an in-memory database", file=sys.stderr)
        return 1
    backup = Path(args.backup)
    # One canonical target (symlinks resolved once): sidecars, publication and the lock all use the real file.
    target = Path(os.path.realpath(cfg.database))
    if not backup.exists():
        print(f"error: no backup at {term_safe(backup)}", file=sys.stderr)
        return 1
    live = str(target)
    sidecars = (f"{live}-wal", f"{live}-shm", f"{live}-journal")
    aside_names = {f"{live}.pre-restore{suf}" for suf in _RESTORE_SUFFIXES}
    reserved = {live, *sidecars, *aside_names, str(lock_path(target)), str(holder_path(target))}
    if os.path.realpath(backup) in reserved:
        print(f"error: {term_safe(backup)} is the live database or one of its sidecar/lock files", file=sys.stderr)
        return 2
    if backup.is_dir():
        print(f"error: {term_safe(backup)} is a directory", file=sys.stderr)
        return 2
    exists_msg = f"error: {term_safe(target)} already exists; pass --force to replace it with the backup"
    if os.path.lexists(target) and not args.force:
        print(exists_msg, file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    try:  # preflight, before any mutation: durability of the rename needs a directory fd
        dfd = _open_dir_fd(target.parent)
    except OSError as e:
        print(f"error: cannot open {term_safe(target.parent)} to fsync the restored database "
              f"({term_safe(e.strerror or e)}); database left untouched", file=sys.stderr)
        return 2
    try:
        with job_lock(target, "restore", _lock_wait(args)):  # no job writes while we swap
            return _restore_locked(target, backup, args, exists_msg, dfd)
    finally:
        if dfd is not None:
            os.close(dfd)


def _restore_locked(target: Path, backup: Path, args: argparse.Namespace, exists_msg: str, dfd: int | None) -> int:
    live = str(target)
    if os.path.lexists(target) and not args.force:  # it may have appeared while we waited for the lock
        print(exists_msg, file=sys.stderr)
        return 1
    if any(os.path.lexists(f"{live}.pre-restore{suf}") for suf in _RESTORE_SUFFIXES):
        print(f"error: {term_safe(live)}.pre-restore* from an earlier restore exists; recover or remove it first "
              "(database left untouched)", file=sys.stderr)
        return 1
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        try:
            ro = sqlite3.connect(f"{backup.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
            try:
                if ro.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("integrity_check failed on the backup")
                out = sqlite3.connect(str(tmp))
                try:
                    ro.backup(out)
                    out.execute("PRAGMA journal_mode=DELETE")  # one self-contained file
                    if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise sqlite3.DatabaseError("integrity_check failed on the copy")
                finally:
                    out.close()
            finally:
                ro.close()
        except sqlite3.Error as e:
            print(f"error: {term_safe(backup)} is not a usable backup ({term_safe(e)}); database left untouched",
                  file=sys.stderr)
            return 1
        os.chmod(tmp, 0o600)
        tfd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(tfd)
        finally:
            os.close(tfd)
        if args.force:  # committed rows could still be only in the WAL: refuse unless folded into the main file
            ck, why = _restore_checkpoint(live)
            if ck:
                print(f"error: {term_safe(live)}: {why}; "
                      + ("stop the web app and scheduler and retry " if ck == 3 else "fix the cause and retry ")
                      + "(database left untouched)", file=sys.stderr)
                return ck
        # Old DB + WAL go aside (recoverable), never deleted; a stale -wal must not be replayed into the copy.
        try:
            base, moved = _restore_aside(live, bool(args.force))
        except _RollbackIncomplete as e:
            print(_rollback_failed_msg("cannot move the old database aside", e.pending), file=sys.stderr)
            return 2
        except OSError as e:
            print(f"error: cannot move the old database aside ({term_safe(e.strerror or e)}); database left untouched",
                  file=sys.stderr)
            return 1
        try:
            if args.force:
                os.replace(tmp, target)
            elif not _publish_no_clobber(tmp, target):  # appeared after the checks above: never overwrite
                pending = _restore_rollback(moved)
                if pending:
                    print(_rollback_failed_msg(f"{term_safe(target)} appeared during the restore", pending), file=sys.stderr)
                    return 2
                print(exists_msg, file=sys.stderr)
                return 1
        except OSError as e:
            pending = _restore_rollback(moved)
            hint = "" if args.force else "; use a filesystem with hard links, or pass --force"
            what = f"cannot publish {term_safe(target)} ({term_safe(e.strerror or e)}){hint}"
            if pending:
                print(_rollback_failed_msg(what, pending), file=sys.stderr)
            else:
                print(f"error: {what}; old database restored in place", file=sys.stderr)
            return 2
        if dfd is not None:
            try:
                os.fsync(dfd)
            except OSError as e:  # genuine durability failure: report truthfully, keep the aside set
                print(f"error: {term_safe(target)} was replaced but the directory fsync failed "
                      f"({term_safe(e.strerror or e)}); the previous database is kept at {term_safe(base)}*",
                      file=sys.stderr)
                return 1
        for _, aside in moved:  # published and durable: the previous set is no longer needed
            Path(aside).unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"restored {term_safe(backup)} -> {term_safe(target)} ({target.stat().st_size} bytes)")
    return 0


def _publish_no_clobber(tmp: Path, dest: Path) -> bool:
    """Atomically give `tmp` the name `dest`; False if `dest` exists at that instant.

    Uses a hard link (fails with EEXIST rather than replacing). Any other OSError (EPERM/ENOTSUP/EXDEV...)
    propagates: there is no safe fallback, so the caller fails without creating or replacing `dest`.
    """
    try:
        os.link(tmp, dest)
        return True
    except FileExistsError:
        return False


def cmd_health(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Exit 0 if the scheduler recorded an ok `schedule` tick (skips count) within --max-age, else 1."""
    from .scheduler import health_check
    max_age = parse_duration(args.max_age, allow_hours=True) if args.max_age else None
    ok, msg = health_check(cfg, max_age)
    print(("ok: " if ok else "unhealthy: ") + term_safe(msg), file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def cmd_web_health(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Exit 0 if the local web app answers GET /api/status (sends the effective token; never prints it)."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{args.port}/api/status")
    if cfg.web_token:
        req.add_header("Authorization", f"Bearer {cfg.web_token}")
    try:
        with urllib.request.urlopen(req, timeout=args.timeout):
            pass
    except urllib.error.HTTPError as e:
        e.close()
        print(f"unhealthy: web app answered HTTP {e.code}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print(f"unhealthy: web app unreachable ({type(e).__name__})", file=sys.stderr)
        return 1
    print("ok: web app answered /api/status")
    return 0


def _add_check_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--window", help="lookback override, e.g. 7d, 2w")
    p.add_argument("--threshold", type=float, help="percent threshold override, e.g. 30")
    p.add_argument("--direction", choices=["up", "down", "both"])
    p.add_argument("--min-price", type=float)
    p.add_argument("--set", action="append",
                   help="only these sets this run: group ID or name regex (repeatable); see `tcgwatch sets`")
    p.add_argument("--discord-threshold", type=float,
                   help="only post moves at least this many percent to Discord")
    p.add_argument("--all-rarities", action="store_true",
                   help="ignore rarity and variant filters (include epic, showcase, manga, alt art)")
    p.add_argument("--preview", action="store_true",
                   help="ignore cooldown, don't record alerts, don't post to Discord")
    p.add_argument("--json", help="also write movers to this JSON file")
    p.add_argument("--quiet", action="store_true", help="no console output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tcgwatch", description=__doc__)
    parser.add_argument("--config", type=Path, help="path to config.yaml (default: $TCGWATCH_CONFIG or ./config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("games", help="list configured games and rules").set_defaults(func=cmd_games)
    sub.add_parser("discord-test", help="post a test message to the Discord webhook") \
        .set_defaults(func=cmd_discord_test)
    p = sub.add_parser("sets", help="list each game's sets (IDs and names) and whether they're checked")
    p.add_argument("--game", action="append", help="game key (repeatable); default: all enabled")
    p.set_defaults(func=cmd_sets)
    p = sub.add_parser("backfill", help="load the last N days of prices from TCGCSV archives")
    p.add_argument("--game", action="append", help="game key (repeatable); default: all enabled")
    p.add_argument("--days", type=int, default=7, help="days back from the latest dataset (default 7)")
    p.add_argument("--force", action="store_true", help="repair: re-fetch and rewrite stored days atomically, replacing their rows "
                        f"(max --days {MAX_FORCE_DAYS})")
    p.add_argument("--lock-wait", type=float, default=0, metavar="SECONDS",
                   help="wait this long for another running job to finish (default: fail fast)")
    p.set_defaults(func=cmd_backfill)
    p = sub.add_parser("watchlist", help="list, add or remove watched card printings")
    wsub = p.add_subparsers(dest="wl_action", required=True)
    q = wsub.add_parser("list", help="show watched cards")
    q.add_argument("--game", action="append", help="game key (repeatable); default: all enabled")
    for act in ("add", "remove"):
        q = wsub.add_parser(act, help=f"{act} a card printing (e.g. `{act} 12345 Normal`)")
        q.add_argument("product_id", type=int)
        q.add_argument("printing")
        q.add_argument("--lock-wait", type=float, default=0, metavar="SECONDS",
                       help="wait this long for another running job to finish (default: fail fast)")
    p.set_defaults(func=cmd_watchlist)
    p = sub.add_parser("alerts", help="recent alert history, newest first (read-only; matches GET /api/alerts)")
    p.add_argument("--game", type=_game_key, help="only this game key")
    p.add_argument("--days", type=_int_range(1, 3650), help="only the last N days (1-3650; default: all)")
    p.add_argument("--discord", action=argparse.BooleanOptionalAction, default=None,
                   help="only alerts posted (--discord) or not posted (--no-discord) to Discord")
    p.add_argument("--limit", type=_int_range(1, 500), default=100, help="max rows (1-500, default 100)")
    p.set_defaults(func=cmd_alerts)
    p = sub.add_parser("runs", help="recent job history, newest first (read-only; matches GET /api/job-runs)")
    p.add_argument("--kind", type=_kind, help="only this job kind (lowercase slug, e.g. snapshot)")
    p.add_argument("--limit", type=_int_range(1, 500), default=50, help="max rows (1-500, default 50)")
    p.add_argument("--json", action="store_true", help="print the rows as a JSON array (same as the API; raw text)")
    p.set_defaults(func=cmd_runs)
    p = sub.add_parser("web", help="local web app: settings and the full card list")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    p.add_argument("--allowed-host", action="append", default=[], metavar="NAME",
                   help="extra Host header to accept (repeatable; also env TCGWATCH_ALLOWED_HOSTS, comma-separated)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--insecure-no-auth", action="store_true",
                   help="allow a non-loopback --host with no token (only when the port is published on loopback)")
    p.set_defaults(func=cmd_web)
    p = sub.add_parser("health", help="exit 0 if the scheduler has an ok job_runs row within --max-age")
    p.add_argument("--max-age", default=None, help="e.g. 28h, 2d (default: 24h + schedule.late_window + 1h)")
    p.set_defaults(func=cmd_health)
    p = sub.add_parser("web-health", help="exit 0 if the local web app answers /api/status (uses the web token)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--timeout", type=float, default=4.0)
    p.set_defaults(func=cmd_web_health)
    p = sub.add_parser("backup", help="online, WAL-safe copy of the database to DEST (mode 0600)")
    p.add_argument("dest", help="destination .db file")
    p.add_argument("--force", action="store_true", help="overwrite DEST if it exists")
    p.add_argument("--lock-wait", type=float, default=0, metavar="SECONDS",
                   help="wait this long for another running job to finish (default: fail fast)")
    p.set_defaults(func=cmd_backup)
    p = sub.add_parser("restore", help="replace the database with BACKUP (stop the web app first; it does not take the job lock)")
    p.add_argument("backup", help="a file made by `tcgwatch backup`")
    p.add_argument("--force", action="store_true", help="replace the database if it exists")
    p.add_argument("--lock-wait", type=float, default=0, metavar="SECONDS",
                   help="wait this long for another running job to finish (default: fail fast)")
    p.set_defaults(func=cmd_restore)
    p = sub.add_parser("prune", help="delete price history older than retention_days (default 180)")
    p.add_argument("--game", action="append", help="game key (repeatable); default: all enabled")
    p.add_argument("--dry-run", action="store_true", help="report what would be removed; change nothing")
    p.add_argument("--lock-wait", type=float, default=0, metavar="SECONDS",
                   help="wait this long for another running job to finish (default: fail fast)")
    p.set_defaults(func=cmd_prune)
    for name, func, help_ in [
        ("snapshot", cmd_snapshot, "fetch and store today's prices"),
        ("check", cmd_check, "flag cards whose price moved past the threshold"),
        ("run", cmd_run, "snapshot, then check (use this from a scheduler)"),
        ("schedule", cmd_schedule, "long-running daily scheduler (at schedule.at UTC; late-publish aware)"),
        ("watch", cmd_schedule, "alias of `schedule`"),
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--game", action="append", help="game key (repeatable); default: all enabled")
        p.add_argument("--lock-wait", type=float, default=0, metavar="SECONDS",
                       help="wait this long for another running job to finish (default: fail fast)")
        if name != "snapshot":
            _add_check_args(p)
        p.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; don't crash on card names it can't encode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        return args.func(cfg, args)
    except ConfigError as e:
        print(f"config error: {term_safe(e)}", file=sys.stderr)
        return 2
    except JobLockedError as e:
        print(f"busy: {term_safe(e)}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

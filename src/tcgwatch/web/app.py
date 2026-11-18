"""FastAPI app: settings editor (writes config.yaml) and the full card list.

Binds to localhost by default; a non-loopback bind requires a token (web.auth_token / TCGWATCH_WEB_TOKEN).
"""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import json
import logging
import os
import re
import sqlite3
import stat
import tempfile
import subprocess
import sys
import threading
import traceback
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from ..config import AlertRule, AppConfig, ConfigError, default_config_path, parse_config, parse_days
from ..filters import CardFilter
from ..joblock import JobLockedError, job_lock
from ..watchlist import MAX_PRODUCT_ID, validate_watch
from ..movement import compare, is_flagged
from ..notify import describe_rule, post_discord
from ..scheduler import health_check

log = logging.getLogger(__name__)
from ..store import Store, is_missing_db

STATIC = Path(__file__).parent / "static"
# Sent instead of the saved webhook URL; sending it back means "keep the saved one".
WEBHOOK_MASK = "__saved__"
GAME_KEY = re.compile(r"^[a-z0-9_]+$")
JOBS = {
    "snapshot": ["snapshot"],
    "backfill": ["backfill", "--days", "7"],
    "run": ["run"],  # posts to Discord
}


# --- config file round-trip -------------------------------------------------

def _is_block(node: Any) -> bool:
    return len(node) > 0 and node.fa.flow_style() is False


def _dash_indent(lines: list[str], line: int) -> int | None:
    """Indent of the '-' that opens the sequence item whose content starts on `line` (0-based)."""
    while 0 <= line < len(lines):
        body = lines[line].lstrip(" ")
        if body == "-" or body.startswith(("- ", "-\t")):
            return len(lines[line]) - len(body)
        line -= 1
    return None


def _local(node: CommentedMap, key: Any) -> bool:
    """True if `key` is written in this mapping itself (merge-inherited keys have no position)."""
    data = getattr(node.lc, "data", None) or {}
    pos = data.get(key)
    return bool(pos) and len(pos) >= 2 and pos[0] is not None and pos[1] is not None


def _guess_indent(root: Any, text: str) -> tuple[int, int]:
    """(mapping indent, block-list dash offset under its key) from the loaded document's node positions.

    Walks the parsed structure (not the text), so anchors, tags, bare dashes and block scalars can't
    fool it. Defaults to 2, 2 for whatever the file doesn't show.
    """
    lines = text.splitlines()
    found: dict[str, int] = {}

    def walk(node: Any) -> None:
        if len(found) == 2:
            return
        if isinstance(node, CommentedMap):
            for key, val in node.items():
                if not _local(node, key):  # inherited via a `<<` merge: no position here
                    walk(val)
                    continue
                kline, kcol = node.lc.key(key)
                if isinstance(val, CommentedMap) and _is_block(val) and "mapping" not in found:
                    first = next((k for k in val if _local(val, k)), None)
                    if first is not None:
                        cline, ccol = val.lc.key(first)
                        if cline > kline and ccol > kcol:
                            found["mapping"] = ccol - kcol
                elif isinstance(val, CommentedSeq) and _is_block(val) and "offset" not in found:
                    iline, _ = val.lc.item(0)
                    dash = _dash_indent(lines, iline)
                    if iline > kline and dash is not None and dash >= kcol:
                        found["offset"] = dash - kcol
                walk(val)
        elif isinstance(node, CommentedSeq):
            for item in node:
                walk(item)

    walk(root)
    return found.get("mapping", 2), found.get("offset", 2)


class ConfigFile:
    """Reads and writes config.yaml, keeping comments and formatting where values didn't change."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.yaml.width = 4096
        self.yaml.indent(mapping=2, sequence=4, offset=2)

    def doc(self) -> CommentedMap:
        text = self.path.read_text(encoding="utf-8")
        # Reuse the file's own mapping indent and block-list dash offset so a save doesn't restyle lists.
        doc = self.yaml.load(text) or CommentedMap()
        mapping, offset = _guess_indent(doc, text)
        self.yaml.indent(mapping=mapping, sequence=offset + 2, offset=offset)
        return doc

    def plain(self) -> dict[str, Any]:
        return _plain(self.doc())

    def app_config(self) -> AppConfig:
        return parse_config(self.plain(), base_dir=self.path.parent)

    def save(self, new: dict[str, Any]) -> None:
        for key in (new.get("games") or {}):
            if not GAME_KEY.match(str(key)):
                raise ConfigError(f"game key {key!r} must be lowercase letters, digits and _")
        doc = self.doc()
        _sync(doc, new)
        parse_config(_plain(doc), base_dir=self.path.parent)  # validate before touching the file
        try:  # keep the original mode/group; a new file defaults to owner-only (holds the webhook)
            st = self.path.stat()
            mode, gid = stat.S_IMODE(st.st_mode), st.st_gid
        except FileNotFoundError:
            mode, gid = 0o600, None
        # unique, exclusively created (O_EXCL, never follows a symlink), 0600 from creation
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                if hasattr(os, "fchmod"):  # absent on Windows
                    os.fchmod(fh.fileno(), mode)
                if gid is not None and hasattr(os, "fchown"):
                    try:
                        os.fchown(fh.fileno(), -1, gid)
                    except OSError:
                        pass  # not permitted to join that group; best effort
                self.yaml.dump(doc, fh)
            if not hasattr(os, "fchmod"):
                os.chmod(tmp_name, mode)
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


def _plain(node: Any) -> Any:
    if isinstance(node, dict):
        return {str(k): _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    return str(node)


def _sync(target: CommentedMap, src: dict[str, Any]) -> None:
    """Make `target` equal `src`, only touching keys whose values changed."""
    for key in [k for k in target if k not in src]:
        del target[key]
    for key, value in src.items():
        old = target.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            _sync(old, value)
        elif key not in target or _plain(old) != value or type(_plain(old)) is not type(value):
            if isinstance(value, list) and isinstance(old, CommentedSeq) and old.fa.flow_style():
                seq = CommentedSeq(value)
                seq.fa.set_flow_style()
                value = seq
            target[key] = value


# --- background jobs ------------------------------------------------------------

class JobRunner:
    """Runs one CLI job at a time as a ``python -m tcgwatch`` subprocess, capturing its output.

    The subprocess takes the cross-process job lock (joblock.py), so a web job also excludes
    ``watch``/cron runs. Starting while the lock is held answers 409 with the holder's kind and start time.
    """

    def __init__(self, config_path: Path, db_path: Callable[[], Path] | None = None):
        self.config_path = config_path
        self.db_path = db_path
        self.lock = threading.Lock()
        self.state: dict[str, Any] | None = None
        self.proc_lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.closing = False

    def _busy_detail(self, kind: str) -> str | None:
        if self.state and self.state["running"]:
            return f"{self.state['kind']} is already running (started {self.state['started']})"
        if self.db_path is None:
            return None
        db = self.db_path()
        try:
            with job_lock(db, kind):  # non-blocking probe; released immediately
                return None
        except JobLockedError as e:
            h = e.holder or {}
            return (f"{h.get('kind', 'another job')} is already running"
                    + (f" (started {h['started_at']})" if h.get("started_at") else ""))

    def start(self, kind: str) -> dict[str, Any]:
        if kind not in JOBS:
            raise HTTPException(404, f"unknown job {kind!r}")
        with self.lock:
            busy = self._busy_detail(kind)
            if busy:
                raise HTTPException(409, busy)
            self.state = {"kind": kind, "running": True, "ok": None, "log": "",
                          "started": datetime.now().isoformat(timespec="seconds"), "finished": None}
            argv = [sys.executable, "-m", "tcgwatch", "--config", str(self.config_path), *JOBS[kind]]
            self.thread = threading.Thread(target=self._run, args=(self.state, argv), daemon=True)
            self.thread.start()
            return dict(self.state)

    def _run(self, state: dict[str, Any], argv: list[str]) -> None:
        ok = False
        proc = None
        try:
            with self.proc_lock:
                if self.closing:
                    raise RuntimeError("web app is shutting down; job not started")
                # -u: unbuffered, so progress lines stream into state["log"] while the job runs
                proc = subprocess.Popen([argv[0], "-u", *argv[1:]], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, errors="replace")
                self.proc = proc
            for line in proc.stdout:
                state["log"] = (state["log"] + line)[-20000:]
            ok = proc.wait() == 0
        except BaseException:
            state["log"] += traceback.format_exc()
        finally:
            if proc is not None and proc.poll() is None:  # reader died early: do not leave an orphan
                self._reap(proc)
            if proc is not None and proc.stdout is not None:
                proc.stdout.close()  # BUG-7: do not leak the pipe fd per job
            with self.proc_lock:
                if self.proc is proc:
                    self.proc = None
        state.update(running=False, ok=ok, finished=datetime.now().isoformat(timespec="seconds"))

    @staticmethod
    def _reap(proc: subprocess.Popen, grace: float = 5.0) -> None:
        """Bounded terminate -> wait -> kill -> wait, so the child cannot keep the job lock."""
        try:
            proc.terminate()
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=grace)
        except OSError:
            pass

    def shutdown(self, grace: float = 5.0) -> None:
        """Stop accepting jobs and reap any running child (called on app lifespan shutdown)."""
        with self.proc_lock:
            self.closing = True
            proc = self.proc
        if proc is not None and proc.poll() is None:
            self._reap(proc, grace)
        t = self.thread
        if t is not None and t.is_alive():
            t.join(timeout=grace)

    def current(self) -> dict[str, Any] | None:
        return dict(self.state) if self.state else None


# --- app ------------------------------------------------------------------------------

def _rule(base: AlertRule, window: str | None, threshold: float | None, discord_threshold: float | None,
          direction: str | None, min_price: float | None) -> AlertRule:
    changes: dict[str, Any] = {}
    if window:
        changes["window_days"] = parse_days(window)
    if threshold is not None:
        changes["threshold_pct"] = threshold
    if discord_threshold is not None:
        changes["discord_threshold_pct"] = discord_threshold
    if direction:
        changes["direction"] = direction
    if min_price is not None:
        changes["min_price"] = min_price
    return replace(base, **changes).validated()


PID_PATH = PathParam(..., ge=1, le=MAX_PRODUCT_ID)


def _span_field(pts: list[dict], fields: list[str]) -> tuple[str, list[float]]:
    """Pick the price field like movement.compare: first one positive at both ends of the span.

    Returns (field, [baseline, current]); falls back to the first field present at the latest
    sample (empty values, so no percent change) and finally "market".
    """
    if len(pts) > 1:
        for f in fields:
            a, b = pts[0][f], pts[-1][f]
            if a and b and a > 0:
                return f, [a, b]
    last = pts[-1] if pts else {}
    return next((f for f in fields if last.get(f) is not None), "market"), []


def _latest(pts: list[dict], field: str) -> float | None:
    return pts[-1][field] if pts else None


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
ALLOWED_HOSTS_ENV = "TCGWATCH_ALLOWED_HOSTS"
TOKEN_COOKIE = "tcgwatch_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DEFAULT_PORTS = {"http": 80, "https": 443}


def _bare_host(host: str) -> str:
    """Lowercase host with IPv6 brackets and a trailing dot removed."""
    host = host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.rstrip(".") if not _is_ip(host) else host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def is_loopback(host: str) -> bool:
    """True for localhost or a loopback IP address (127.0.0.0/8, ::1) - never judged by DNS-name prefix."""
    host = _bare_host(host)
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_host_header(value: str | None) -> tuple[str, int | None] | None:
    """Split a Host header into (lowercase bare host, port or None); None when malformed.

    Handles ``[::1]:8765`` itself instead of relying on the framework (old Starlette splits on ':').
    """
    if not value or any(c in value for c in "/?#@\\ \t"):
        return None
    try:
        parts = urlsplit("//" + value)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host or parts.netloc != value:
        return None
    return _bare_host(host), port


def origin_of(source: str | None) -> tuple[str, str, int] | None:
    """(scheme, host, effective port) of an Origin/Referer value; None for malformed/opaque ones."""
    if not source:
        return None
    try:
        parts = urlsplit(source.strip())
        port = parts.port
        host = parts.hostname
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not host:
        return None
    return scheme, _bare_host(host), port if port is not None else DEFAULT_PORTS[scheme]


def allowed_hosts(bind_host: str | None = None, extra: list[str] | None = None) -> list[str]:
    """Host names the app answers to: loopback, the bound host, env/flag additions."""
    hosts = list(LOOPBACK_HOSTS)
    if bind_host and bind_host not in ("0.0.0.0", "::"):
        hosts.append(bind_host)
    env = os.environ.get(ALLOWED_HOSTS_ENV, "")
    hosts += [h.strip() for h in env.split(",") if h.strip()]
    hosts += [h.strip() for h in (extra or []) if h.strip()]
    return list(dict.fromkeys(hosts))


def _host_allowed(host: str, allowed: set[str]) -> bool:
    return host in allowed or any(a.startswith("*.") and host.endswith(a[1:]) for a in allowed)


def token_ok(request: Request, token: str) -> bool:
    """Bearer header or cookie matches `token` (constant-time compare)."""
    auth = request.headers.get("authorization", "")
    given = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    cookie = request.cookies.get(TOKEN_COOKIE, "")
    want = token.encode()
    return any(hmac.compare_digest(c.encode(), want) for c in (given, cookie) if c)


LOGIN_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
LOGIN_PAGE = (
    "<!doctype html><meta charset=utf-8><meta name=referrer content=no-referrer>"
    "<title>TCG Price Watch login</title><form method=post action=/login>"
    "<label>Access token <input type=password name=token autofocus autocomplete=current-password></label> "
    "<button>Log in</button></form>"
)


async def _login_token(request: Request) -> str:
    """Token from a JSON or urlencoded POST body (never the URL)."""
    raw = (await request.body())[:4096]
    if "json" in request.headers.get("content-type", ""):
        try:
            val = json.loads(raw).get("token", "")
        except (ValueError, AttributeError):
            return ""
        return val if isinstance(val, str) else ""
    return parse_qs(raw.decode("utf-8", "replace")).get("token", [""])[0]


async def _login(request: Request, token: str) -> Response:
    """GET /login = form; POST /login = set the cookie. Same-origin is enforced by same_origin_only."""
    if request.method == "GET":
        return HTMLResponse(LOGIN_PAGE, headers=LOGIN_HEADERS)
    if request.method == "POST":
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403, headers=LOGIN_HEADERS)
        given = await _login_token(request)
        if given and hmac.compare_digest(given.encode(), token.encode()):
            resp = Response(status_code=303, headers={"location": "/", **LOGIN_HEADERS})
            resp.set_cookie(TOKEN_COOKIE, given, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https")
            return resp
        return JSONResponse({"detail": "authentication required"}, status_code=401, headers=LOGIN_HEADERS)
    return JSONResponse({"detail": "method not allowed"}, status_code=405, headers=LOGIN_HEADERS)


def _logout(request: Request) -> Response:
    """POST /logout = drop the token cookie (same flags as login) and go back to /login. Bearer is unaffected."""
    if request.method != "POST":
        return JSONResponse({"detail": "method not allowed"}, status_code=405, headers=LOGIN_HEADERS)
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403, headers=LOGIN_HEADERS)
    resp = Response(status_code=303, headers={"location": "/login", **LOGIN_HEADERS})
    resp.delete_cookie(TOKEN_COOKIE, httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return resp


def create_app(config_path: Path | None = None, hosts: list[str] | None = None,
               token: str | None = None) -> FastAPI:
    config_file = ConfigFile(config_path or default_config_path())
    jobs = JobRunner(config_file.path, lambda: config_file.app_config().database)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            jobs.shutdown()

    app = FastAPI(title="TCG Price Watch", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    allowed = {_bare_host(h) for h in (hosts if hosts is not None else allowed_hosts())}

    if token:
        @app.middleware("http")
        async def require_token(request: Request, call_next):
            """Every request needs the token (bearer or cookie); POST /login sets the cookie (never a URL)."""
            if request.url.path == "/login":
                return await _login(request, token)
            if request.url.path == "/logout":
                return _logout(request)
            if token_ok(request, token):
                return await call_next(request)
            return JSONResponse({"detail": "authentication required"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """Reject mutating requests from another origin (CSRF); curl-style clients send neither header."""
        if request.method not in SAFE_METHODS:
            source = request.headers.get("origin") or request.headers.get("referer")
            if source is not None:
                theirs = origin_of(source)
                me = parse_host_header(request.headers.get("host"))
                scheme = request.url.scheme
                mine = (scheme, me[0], me[1] if me[1] is not None else DEFAULT_PORTS.get(scheme, 0)) if me else None
                if theirs is None or theirs != mine:
                    return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def trusted_host(request: Request, call_next):
        """Refuse a forged Host (DNS rebinding) before anything else runs; added last = outermost."""
        parsed = parse_host_header(request.headers.get("host"))
        if parsed is None or not _host_allowed(parsed[0], allowed):
            return JSONResponse({"detail": "Invalid host header"}, status_code=400)
        return await call_next(request)

    @app.exception_handler(ConfigError)
    def config_error(request: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def game_or_404(cfg: AppConfig, key: str):
        if key not in cfg.games:
            raise HTTPException(404, f"unknown game {key!r}")
        return cfg.games[key]

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        cfg = config_file.app_config()
        games = []
        try:
            with Store(cfg.database, readonly=True) as store:
                for g in cfg.games.values():
                    dates = store.snapshot_dates(g.key)
                    games.append({"key": g.key, "name": g.name, "enabled": g.enabled, "days": len(dates),
                                  "first": dates[0] if dates else None, "latest": dates[-1] if dates else None})
        except sqlite3.DatabaseError as e:
            if not is_missing_db(e, cfg.database):
                log.error("cannot read status from %s: %s", cfg.database, e)
                raise HTTPException(500, "cannot read status: database unreadable")
            # no database yet: report every game with zero days, without creating the file
            games = [{"key": g.key, "name": g.name, "enabled": g.enabled, "days": 0, "first": None, "latest": None}
                     for g in cfg.games.values()]
        ok, detail = health_check(cfg)
        return {"games": games, "discord": bool(cfg.discord_webhook), "job": jobs.current(),
                "config_path": str(config_file.path), "auth_enabled": bool(token), "health": {"ok": ok, "detail": detail}}

    @app.get("/api/job-runs")
    def job_runs(kind: str | None = Query(None, pattern=r"^[a-z][a-z0-9_-]{0,31}$"),
                 limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
        """Recent job_runs rows, newest first. Read-only; never creates the database."""
        cfg = config_file.app_config()
        try:
            with Store(cfg.database, readonly=True) as store:
                return [dict(r) for r in store.recent_jobs(kind, limit)]
        except sqlite3.DatabaseError as e:
            if is_missing_db(e, cfg.database):
                return []  # no database yet
            log.error("cannot read job runs from %s: %s", cfg.database, e)
            raise HTTPException(500, "cannot read job runs: database unreadable")

    @app.get("/api/alerts")
    def alerts(game: str | None = Query(None, pattern=r"^[a-z][a-z0-9_-]{0,31}$"),
               days: int | None = Query(None, ge=1, le=3650), discord: bool | None = None,
               limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
        """Recorded alerts, newest first. Read-only; never creates the database."""
        cfg = config_file.app_config()
        since = datetime.now(timezone.utc).date() - timedelta(days=days) if days else None
        try:
            with Store(cfg.database, readonly=True) as store:
                return [dict(r) for r in store.recent_alerts(game, since, discord, limit)]
        except sqlite3.DatabaseError as e:
            if is_missing_db(e, cfg.database):
                return []  # no database yet
            log.error("cannot read alerts from %s: %s", cfg.database, e)
            raise HTTPException(500, "cannot read alerts: database unreadable")

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        raw = config_file.plain()
        discord = (raw.get("notify") or {}).get("discord") or {}
        if discord.get("webhook_url"):
            discord["webhook_url"] = WEBHOOK_MASK
        web = raw.get("web")
        if isinstance(web, dict) and web.get("auth_token"):
            web["auth_token"] = WEBHOOK_MASK
        return {"config": raw}

    @app.put("/api/settings")
    def put_settings(body: dict[str, Any]) -> dict[str, Any]:
        new = body.get("config")
        if not isinstance(new, dict):
            raise HTTPException(400, "body must be {config: {...}}")
        discord = (new.get("notify") or {}).get("discord") or {}
        if discord.get("webhook_url") == WEBHOOK_MASK:
            saved = ((config_file.plain().get("notify") or {}).get("discord") or {}).get("webhook_url")
            discord["webhook_url"] = saved or ""
        web = new.get("web")
        if isinstance(web, dict) and web.get("auth_token") == WEBHOOK_MASK:
            web["auth_token"] = (config_file.plain().get("web") or {}).get("auth_token") or ""
        config_file.save(new)
        return {"ok": True}

    @app.get("/api/sets/{game_key}")
    def sets(game_key: str) -> list[dict[str, Any]]:
        """Sets seen for a game. Read-only; never creates the database."""
        cfg = config_file.app_config()
        game = game_or_404(cfg, game_key)
        card_filter = CardFilter.for_game(game)
        try:
            with Store(cfg.database, readonly=True) as store:
                rows = store.sets(game.key)
        except sqlite3.DatabaseError as e:
            if is_missing_db(e, cfg.database):
                return []  # no database yet
            log.error("cannot read sets from %s: %s", cfg.database, e)
            raise HTTPException(500, "cannot read sets: database unreadable")
        return [{"id": r["group_id"], "name": r["group_name"], "singles": r["singles"],
                 "checked": card_filter.accepts_set(r["group_id"], r["group_name"])} for r in rows]

    @app.get("/api/cards/{game_key}")
    def cards(game_key: str, window: str | None = None, threshold: float | None = None,
              discord_threshold: float | None = None, direction: str | None = None,
              min_price: float | None = None, all_rarities: bool = False) -> dict[str, Any]:
        """Every stored series with a comparable baseline, flagged or not."""
        cfg = config_file.app_config()
        game = game_or_404(cfg, game_key)
        rule = _rule(game.alert, window, threshold, discord_threshold, direction, min_price)
        card_filter = CardFilter.for_game(game, all_rarities=all_rarities)
        out: dict[str, Any] = {"rule": describe_rule(rule), "window_days": rule.window_days,
                               "threshold_pct": rule.threshold_pct,
                               "discord_threshold_pct": rule.effective_discord_threshold,
                               "current_date": None, "rows": [], "message": None}
        NO_DATA = "No price data yet. Run a snapshot or backfill."
        try:
            with Store(cfg.database, readonly=True) as store:
                current = store.latest_snapshot_date(game.key)
                if current is None:
                    out["message"] = NO_DATA
                    return out
                out["current_date"] = current.isoformat()
                alerts = store.last_alerts(game.key)
                watched = {(w["product_id"], w["printing"]) for w in store.watched(game.key)}
                for obs in store.observations(game.key, rule.window_days):
                    m = compare(obs, rule)
                    if m is None:
                        continue
                    in_scope = card_filter.accepts(obs.card, obs.printing)
                    flagged = in_scope and is_flagged(m, rule)
                    last = alerts.get((obs.card.product_id, obs.printing))
                    c = obs.card
                    out["rows"].append({
                        "id": f"{c.product_id}:{obs.printing}", "name": c.name, "number": c.number,
                        "rarity": c.rarity, "printing": obs.printing, "set": c.group_name, "set_id": c.group_id,
                        "url": c.url, "field": m.price_field, "base_date": m.baseline_date,
                        "base": m.baseline_price, "cur": m.current_price, "pct": round(m.pct_change, 2),
                        "in_scope": in_scope, "flagged": flagged,
                        "discord": flagged and abs(m.pct_change) >= rule.effective_discord_threshold,
                        "product_id": c.product_id, "watched": (c.product_id, obs.printing) in watched,
                        "last_alert": last[0] if last else None, "posted": bool(last and last[1]),
                    })
                if not out["rows"]:
                    out["message"] = (f"Not enough history for a {rule.window_days}-day window "
                                      f"({store.snapshot_days(game.key)} day(s) stored). Try Backfill.")
        except sqlite3.DatabaseError as e:
            if not is_missing_db(e, cfg.database):
                log.error("cannot read cards from %s: %s", cfg.database, e)
                raise HTTPException(500, "cannot read cards: database unreadable")
            out["message"] = NO_DATA  # no database yet
        return out

    @app.get("/api/history/{product_id}")
    def history(product_id: int = PID_PATH, printing: str = Query(...), days: int | None = Query(None, ge=1, le=3650)) -> dict[str, Any]:
        """Daily prices for one card printing, oldest first (for the sparkline). Read-only; never creates the database."""
        cfg = config_file.app_config()
        try:
            with Store(cfg.database, readonly=True) as store:
                points = store.history(product_id, printing, days)
        except sqlite3.DatabaseError as e:
            if not is_missing_db(e, cfg.database):
                log.error("cannot read history from %s: %s", cfg.database, e)
                raise HTTPException(500, "cannot read history: database unreadable")
            points = []  # no database yet
        if not points:
            raise HTTPException(404, "no price history for that card")
        return {"product_id": product_id, "printing": printing, "points": points}

    @app.get("/api/watchlist/{game_key}")
    def get_watchlist(game_key: str, days: int = Query(30, ge=1, le=3650)) -> list[dict[str, Any]]:
        """Watched cards with their latest price, change over the span, and history for sparklines.
        Read-only; never creates the database."""
        cfg = config_file.app_config()
        game = game_or_404(cfg, game_key)
        try:
            with Store(cfg.database, readonly=True) as store:
                rows = store.watched(game.key)
                hist = store.history_many([(w["product_id"], w["printing"]) for w in rows], days)
        except sqlite3.DatabaseError as e:
            if is_missing_db(e, cfg.database):
                return []  # no database yet
            log.error("cannot read watchlist from %s: %s", cfg.database, e)
            raise HTTPException(500, "cannot read watchlist: database unreadable")
        out = []
        for w in rows:
            pts = hist[(w["product_id"], w["printing"])]
            field, vals = _span_field(pts, game.alert.price_fields)
            pct = round((vals[1] - vals[0]) / vals[0] * 100, 2) if len(vals) == 2 else None
            out.append({"product_id": w["product_id"], "printing": w["printing"], "name": w["name"],
                        "number": w["number"], "rarity": w["rarity"], "set": w["group_name"],
                        "url": w["url"], "field": field, "points": pts,
                        "cur": _latest(pts, field), "pct": pct})
        return out

    @app.put("/api/watchlist")
    def watch(body: dict[str, Any]) -> dict[str, Any]:
        pid, printing = body.get("product_id"), body.get("printing")
        if (err := validate_watch(pid, printing)):
            raise HTTPException(400, err)
        with Store(config_file.app_config().database) as store:
            if not store.watch(pid, printing):
                raise HTTPException(404, "no prices for this printing")
        return {"ok": True}

    @app.delete("/api/watchlist/{product_id}")
    def unwatch(product_id: int = PID_PATH, printing: str = Query(...)) -> dict[str, Any]:
        if not printing.strip():
            raise HTTPException(400, "printing must be a non-empty string")
        with Store(config_file.app_config().database) as store:
            store.unwatch(product_id, printing)
        return {"ok": True}

    @app.get("/api/jobs")
    def job_status() -> dict[str, Any] | None:
        return jobs.current()

    @app.post("/api/jobs/{kind}")
    def start_job(kind: str) -> dict[str, Any]:
        return jobs.start(kind)

    @app.post("/api/discord-test")
    def discord_test() -> dict[str, Any]:
        cfg = config_file.app_config()
        if not cfg.discord_webhook:
            raise HTTPException(400, "No Discord webhook configured")
        try:
            post_discord(cfg.discord_webhook, "tcg-price-watch: test message from the web app.")
        except OSError as e:
            raise HTTPException(502, f"Discord rejected the post: {e}") from e
        return {"ok": True}

    return app

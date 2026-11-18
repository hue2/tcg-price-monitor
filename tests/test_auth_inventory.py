"""TEST-14: auth-guard inventory. A new route cannot ship unguarded.

The token guard is an app-wide middleware (web/app.py `require_token`), so every route is
covered by default. This test walks `app.routes` with TCGWATCH_WEB_TOKEN set and checks each
route behaviourally: no token -> 401, correct token -> not 401. A route that is neither
guarded nor on the explicit allowlist below fails the test. Adding a public route means
adding it to PUBLIC_ROUTES here, in the same change, on purpose.
"""
import re

import pytest
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
from starlette.routing import Mount, Route

from tcgwatch.config import load_config
from tcgwatch.web.app import TOKEN_COOKIE, create_app

# Literal allowlist of routes that are intentionally reachable without a token. Empty on
# purpose: with a token set, the UI shell ("/") and its assets ("/static") stay behind the
# guard (unauthenticated users go to /login), so they are checked like every other route.
# /login and /logout are AUTH-0 exemptions handled inside the middleware, not in app.routes;
# they are pinned by test_login_and_logout_are_the_only_middleware_exemptions below.
PUBLIC_ROUTES: frozenset[str] = frozenset()

TOKEN = "inventory-token"
PARAM = re.compile(r"{([a-zA-Z_][a-zA-Z0-9_]*)}")
# Sample values that are safe to send: unknown kind (404, never spawns a job), a missing
# product (no write), and an empty-ish body (400 before any write).
SAMPLE = {"game_key": "tcg", "product_id": "1", "kind": "__probe__"}


@pytest.fixture
def guarded_app(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGWATCH_WEB_TOKEN", TOKEN)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: t.db\ngames: {}\n", encoding="utf-8")
    token = load_config(cfg).web_token
    assert token == TOKEN, "env TCGWATCH_WEB_TOKEN must reach the app"
    return create_app(cfg, token=token)


def _api_routes(app):
    """(path, method) for every endpoint reachable through the app, skipping mounts.

    Starlette adds HEAD to plain Route GETs but FastAPI's APIRoute does not, so HEAD is probed
    explicitly for every GET: a HEAD must meet the same guard as the GET it mirrors."""
    out = []
    for route in app.routes:
        if isinstance(route, Mount):
            continue
        if isinstance(route, (APIRoute, Route)) and getattr(route, "methods", None):
            methods = set(route.methods)
            if "GET" in methods:
                methods.add("HEAD")
            for method in sorted(methods):
                out.append((route.path, method))
    return out


def _concrete(path):
    return PARAM.sub(lambda m: SAMPLE.get(m.group(1), "1"), path)


def test_inventory_covers_routes(guarded_app):
    """Sanity: the walk actually sees the API surface, so an empty walk cannot pass silently."""
    paths = {p for p, _ in _api_routes(guarded_app)}
    assert "/api/settings" in paths and "/api/watchlist" in paths and "/api/jobs/{kind}" in paths


def test_every_route_is_guarded_or_allowlisted(guarded_app):
    failures = []
    for path, method in _api_routes(guarded_app):
        if path in PUBLIC_ROUTES:
            continue
        # Fresh client per request: no cookie jar carry-over between probes.
        kwargs = {"json": {}} if method not in ("GET", "HEAD") else {}
        client = TestClient(guarded_app, base_url="http://localhost")
        url = _concrete(path)
        no_token = client.request(method, url, **kwargs)
        if no_token.status_code != 401:
            failures.append(f"{method} {path}: no token -> {no_token.status_code}, expected 401")
            continue
        with_token = client.request(method, url, headers={"Authorization": f"Bearer {TOKEN}"}, **kwargs)
        if with_token.status_code == 401:
            failures.append(f"{method} {path}: correct token still 401")
    assert not failures, "unguarded or unreachable routes:\n  " + "\n  ".join(failures)


def test_allowlist_entries_are_real_routes(guarded_app):
    """A stale allowlist entry must not silently exempt a future route at that path."""
    paths = {p for p, _ in _api_routes(guarded_app)} | {
        r.path for r in guarded_app.routes if isinstance(r, Mount)
    }
    assert PUBLIC_ROUTES <= paths, f"allowlist names routes that no longer exist: {PUBLIC_ROUTES - paths}"


def _bearer():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_ui_shell_and_static_assets_are_guarded(guarded_app):
    """'/' and '/static' are not public: no token -> 401, token -> served. Pins the real behaviour."""
    client = TestClient(guarded_app, base_url="http://localhost")
    for url in ("/", "/static/app.js"):
        assert client.get(url).status_code == 401, f"{url} must be guarded without a token"
        assert client.get(url, headers=_bearer()).status_code == 200, f"{url} must serve with a token"


def test_login_and_logout_are_the_only_middleware_exemptions(guarded_app):
    """/login and /logout answer without a token (AUTH-0/AUTH-1). They live in the middleware,
    so the app.routes walk cannot see them; these asserts stop a future change guarding or
    unguarding them silently. The 401s here come from the login handler (no-store header, no
    WWW-Authenticate), not from the guard, which is what distinguishes exemption from guarding."""
    client = TestClient(guarded_app, base_url="http://localhost", follow_redirects=False)

    form = client.get("/login")
    assert form.status_code == 200, "GET /login must serve the form without a token"
    assert "name=token" in form.text and "action=/login" in form.text

    bad = client.post("/login", data={"token": "wrong"})
    assert bad.status_code == 401
    assert bad.headers.get("cache-control") == "no-store"
    assert "www-authenticate" not in bad.headers, "a bad login is the handler's 401, not the guard's"

    ok = client.post("/login", data={"token": TOKEN})
    assert ok.status_code == 303, "POST /login with the right token must set the cookie"
    assert ok.headers.get("location") == "/"
    assert ok.headers.get("set-cookie", "").startswith(f"{TOKEN_COOKIE}={TOKEN}"), "login must set the token cookie"

    out = client.post("/logout")
    assert out.status_code == 303, "POST /logout must answer without a token"
    assert out.headers.get("location") == "/login"

    assert client.get("/logout").status_code == 405, "GET /logout is the handler's 405, not a guard 401"


def test_no_other_middleware_path_is_exempt(guarded_app):
    """Any other non-route path still gets the guard 401 (no accidental prefix exemption)."""
    client = TestClient(guarded_app, base_url="http://localhost")
    for url in ("/login-helper", "/logout/x", "/loginx", "/api/status/"):
        r = client.get(url)
        assert r.status_code == 401, f"{url} must be guarded, got {r.status_code}"


def test_static_mount_serves_only_files(guarded_app):
    """TEST-16: the /static Mount serves files and nothing else.

    Every shipped file is served (GET and HEAD) with a token. A directory gets no listing, a
    missing file is a plain 404, and traversal attempts never reach a file outside the static
    folder. Stays on exact status codes so a change to StaticFiles' html/listing behaviour fails here."""
    from tcgwatch.web.app import STATIC

    client = TestClient(guarded_app, base_url="http://localhost", follow_redirects=False)
    files = sorted(p.relative_to(STATIC).as_posix() for p in STATIC.rglob("*") if p.is_file())
    assert files, "static folder is empty; the mount check would pass vacuously"
    for name in files:
        url = f"/static/{name}"
        assert client.get(url, headers=_bearer()).status_code == 200, f"{url} must be served"
        assert client.request("HEAD", url, headers=_bearer()).status_code == 200, f"HEAD {url} must be served"

    for url in ("/static/", "/static/nope.js", "/static/../config.yaml",
                "/static/..%2fconfig.yaml", "/static/%2e%2e/config.yaml"):
        r = client.get(url, headers=_bearer())
        assert r.status_code == 404, f"{url} must be 404, got {r.status_code}"
        assert "<a href" not in r.text and "Index of" not in r.text, f"{url} leaked a directory listing"

    bare = client.get("/static", headers=_bearer())
    assert bare.status_code != 200, "bare /static must not serve a listing"

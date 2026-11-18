"""TEST-16: every GET /api/* route leaves no database behind.

READ-2/READ-3 made the read routes open the database read-only, so a GET on a fresh install
never creates a file or a directory. Those guarantees were pinned route by route. This test
walks `app.routes` (no hand-kept list), so a future GET that opens `Store()` read-write, or
reaches a write path, fails here without anyone remembering to add it. Each route is called
against a configured game and a database path whose parent directory does not exist; after the
call neither the file nor the directory may exist.

The walk is parametrized by path, so a new GET route is picked up automatically. Required query
parameters are filled with a neutral value (inferred from FastAPI's own metadata), and a 422 is
rejected so a route cannot pass vacuously by failing validation before its handler runs.
"""
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tcgwatch.web.app import create_app

TOKEN = "no-create-token"
PARAM = re.compile(r"{([a-zA-Z_][a-zA-Z0-9_]*)}")
# Safe path values: a configured game (so the handler reaches its database code), a product id
# that matches nothing, and a job kind that never spawns a job.
SAMPLE = {"game_key": "op", "product_id": "1", "kind": "__probe__"}
# Neutral value for any required query parameter (e.g. history's `printing`).
REQUIRED_QUERY_VALUE = "Normal"

CONFIG = """\
database: {db}
alert:
  window: 7d
  threshold_pct: 30
games:
  op:
    name: One Piece
    category_id: 68
    rarities: [C, R]
"""


def _get_api_routes(app):
    """(path, required query names) for every GET endpoint under /api/, straight from app.routes."""
    out = []
    for route in app.routes:
        if isinstance(route, APIRoute) and "GET" in route.methods and route.path.startswith("/api/"):
            required = [f.name for f in route.dependant.query_params if f.field_info.is_required()]
            out.append((route.path, required))
    return out


# Built once at collection time from a throwaway config: create_app never touches the database
# path at construction, so the route list does not depend on any per-test state.
_PROBE = create_app(Path("/nonexistent/tcg-price-watch/config.yaml"), token=TOKEN)
GET_API_ROUTES = _get_api_routes(_PROBE)
GET_API_PATHS = [path for path, _ in GET_API_ROUTES]


def test_walk_sees_the_get_api_surface():
    """Sanity: an empty walk would make the parametrized test below pass silently."""
    assert {"/api/status", "/api/cards/{game_key}", "/api/history/{product_id}", "/api/watchlist/{game_key}"} <= set(GET_API_PATHS)


@pytest.mark.parametrize("path", GET_API_PATHS)
def test_get_api_route_creates_no_database(tmp_path, path):
    db = tmp_path / "fresh" / "dir" / "tcg.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.format(db=db.as_posix()), encoding="utf-8")
    app = create_app(cfg, token=TOKEN)
    required = dict(GET_API_ROUTES)[path]
    url = PARAM.sub(lambda m: SAMPLE.get(m.group(1), "1"), path)
    query = {name: REQUIRED_QUERY_VALUE for name in required}

    client = TestClient(app, base_url="http://localhost")
    resp = client.get(url, params=query, headers={"Authorization": f"Bearer {TOKEN}"})

    assert resp.status_code not in (401, 422), f"GET {url} never reached its handler: {resp.status_code} {resp.text[:200]}"
    assert not db.exists(), f"GET {url} created {db}"
    assert not db.parent.exists(), f"GET {url} created directory {db.parent}"

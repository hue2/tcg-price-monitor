"""TCGCSV singles parsing: sealed skip, catalog/price filtering, category resolution (no network)."""

import json

import pytest

from tcgwatch.config import GameConfig, AlertRule
from tcgwatch.sources.tcgcsv import TcgCsvError, TcgCsvSource

GAME = GameConfig(key="op", name="One Piece", alert=AlertRule(), category_id=68)
CATEGORIES = [
    {"categoryId": 68, "name": "One Piece Card Game", "displayName": "One Piece"},
    {"categoryId": 3, "name": "Pokemon", "displayName": "Pokémon"},
]
GROUPS = [{"groupId": 10, "name": "OP-01"}, {"groupId": 11, "name": "Sealed only"}]
PRODUCTS = {
    10: [
        {"productId": 1, "name": "Luffy", "url": "https://t.test/1", "extendedData": [
            {"name": "Rarity", "value": "L"}, {"name": "Number", "value": "OP01-001"}]},
        {"productId": 2, "name": "Zoro", "extendedData": [{"name": "Rarity", "value": "C"}]},
        {"productId": 3, "name": "Booster Box", "extendedData": [{"name": "Number", "value": "X"}]},
    ],
    11: [
        {"productId": 50, "name": "Starter Deck", "extendedData": []},
    ],
}
PRICES = {
    10: [
        {"productId": 1, "subTypeName": "Normal", "marketPrice": 12.5, "lowPrice": 10.0,
         "midPrice": 11.0, "highPrice": 20.0},
        {"productId": 2, "subTypeName": "Foil", "marketPrice": 0.5},
        # product 3 (sealed) and 999 (not in catalog) must be dropped
        {"productId": 3, "subTypeName": "Normal", "marketPrice": 99.0},
        {"productId": 999, "subTypeName": "Normal", "marketPrice": 1.0},
    ],
    11: [{"productId": 50, "subTypeName": "Normal", "marketPrice": 30.0}],
}


@pytest.fixture
def source(monkeypatch):
    src = TcgCsvSource(user_agent="test", request_delay_seconds=0)
    routes = {
        "/tcgplayer/categories": {"success": True, "results": CATEGORIES},
        "/tcgplayer/68/groups": {"success": True, "results": GROUPS},
    }
    for gid, products in PRODUCTS.items():
        routes[f"/tcgplayer/68/{gid}/products"] = {"success": True, "results": products}
        routes[f"/tcgplayer/68/{gid}/prices"] = {"success": True, "results": PRICES[gid]}

    calls = []

    def fake_get(path):
        calls.append(path)
        return json.dumps(routes[path]).encode()

    monkeypatch.setattr(src, "_get", fake_get)
    src.calls = calls
    return src


def test_singles_are_parsed_with_group_and_extended_fields(source):
    batches = list(source.fetch_singles(GAME))
    cards, prices = batches[0]
    assert [(c.product_id, c.name, c.rarity, c.number, c.group_name, c.group_id, c.url)
            for c in cards] == [
        (1, "Luffy", "L", "OP01-001", "OP-01", 10, "https://t.test/1"),
        (2, "Zoro", "C", None, "OP-01", 10, None),
    ]
    assert all(c.game_key == "op" for c in cards)


def test_sealed_products_are_skipped_and_empty_groups_yield_nothing(source):
    batches = list(source.fetch_singles(GAME))
    all_ids = {c.product_id for cards, _ in batches for c in cards}
    assert 3 not in all_ids  # sealed: no Rarity
    assert 50 not in all_ids
    assert len(batches) == 1  # group 11 has only sealed product, so no batch at all
    assert "/tcgplayer/68/11/prices" not in source.calls  # and no price request for it


def test_prices_only_kept_for_catalogued_singles(source):
    (cards, prices), = list(source.fetch_singles(GAME))
    assert {p.product_id for p in prices} == {1, 2}
    luffy = next(p for p in prices if p.product_id == 1)
    assert (luffy.printing, luffy.market, luffy.low, luffy.mid, luffy.high) == (
        "Normal", 12.5, 10.0, 11.0, 20.0)
    zoro = next(p for p in prices if p.product_id == 2)
    assert (zoro.printing, zoro.market, zoro.low, zoro.mid, zoro.high) == ("Foil", 0.5, None, None, None)


def test_category_resolved_by_name_case_insensitively_and_cached(source):
    game = GameConfig(key="op", name="OP", alert=AlertRule(), category_name="one piece card GAME")
    assert source.resolve_category(game) == 68
    assert source.resolve_category(game) == 68
    assert source.calls.count("/tcgplayer/categories") == 1


def test_category_matches_display_name(source):
    game = GameConfig(key="pk", name="Pokemon", alert=AlertRule(), category_name="Pokémon")
    assert source.resolve_category(game) == 3


def test_explicit_category_id_skips_the_lookup(source):
    assert source.resolve_category(GAME) == 68
    assert source.calls == []  # no network at all


def test_unknown_category_name_is_an_error(source):
    game = GameConfig(key="x", name="X", alert=AlertRule(), category_name="Nonexistent")
    with pytest.raises(TcgCsvError, match="no TCGplayer category named 'Nonexistent'"):
        source.resolve_category(game)


def test_api_failure_flag_raises_with_path_and_errors(monkeypatch):
    src = TcgCsvSource(user_agent="test", request_delay_seconds=0)
    monkeypatch.setattr(src, "_get", lambda path: json.dumps(
        {"success": False, "errors": ["bad group"], "results": []}).encode())
    with pytest.raises(TcgCsvError, match=r"/tcgplayer/68/groups: \['bad group'\]"):
        src._results("/tcgplayer/68/groups")

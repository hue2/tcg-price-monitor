from dataclasses import replace

import pytest

from tcgwatch.config import parse_sets
from tcgwatch.filters import CardFilter, set_selected
from tcgwatch.models import Card


def card(name, rarity, game="one_piece"):
    return Card(product_id=1, game_key=game, group_id=1, group_name="Set", name=name,
                number=None, rarity=rarity)


# Names below are real TCGCSV product names.
@pytest.mark.parametrize("name,rarity,expected", [
    ("Nico Robin", "SR", True),
    ("Franky", "C", True),
    ("Nico Robin (Manga)", "SEC", False),
    ("Saint Shamrock (Alternate Art)", "SR", False),
    ("Loki (OP17-119) (Alternate Art)", "SEC", False),
    ("Monkey.D.Luffy", "SEC", True),  # SEC base art is a regular print (Q-SPEC9 Q1)
    ("Nami (Parallel)", "SEC", False),
    ("Nami (SP)", "SEC", False),
])
def test_one_piece_regular_print(example_cfg, name, rarity, expected):
    f = CardFilter.for_game(example_cfg.games["one_piece"])
    assert f.accepts(card(name, rarity), "Normal") is expected


@pytest.mark.parametrize("name,rarity,expected", [
    ("Jinx - Rebel", "Rare", True),
    ("Jinx - Rebel", "Epic", False),
    ("Jinx - Rebel (Overnumbered)", "Showcase", False),
    ("Viktor - Herald (Signature)", "Showcase", False),
    ("Lee Sin (Alternate Art)", "Rare", True),  # alt art counts as regular print for Riftbound
])
def test_riftbound_regular_print(example_cfg, name, rarity, expected):
    f = CardFilter.for_game(example_cfg.games["riftbound"])
    assert f.accepts(card(name, rarity, "riftbound"), "Foil") is expected


def test_all_rarities_includes_higher_tiers(example_cfg):
    f = CardFilter.for_game(example_cfg.games["one_piece"], all_rarities=True)
    assert f.accepts(card("Nico Robin (Manga)", "SEC"), "Normal")


def test_printing_filter(example_cfg):
    game = replace(example_cfg.games["one_piece"], printings=("Normal",))
    f = CardFilter.for_game(game)
    assert f.accepts(card("Franky", "C"), "normal")
    assert not f.accepts(card("Franky", "C"), "Foil")


def in_set(group_id, group_name, rarity="C"):
    return Card(product_id=1, game_key="one_piece", group_id=group_id, group_name=group_name,
                name="Franky", number=None, rarity=rarity)


def test_default_set_exclusions(example_cfg):
    f = CardFilter.for_game(example_cfg.games["one_piece"])
    assert f.accepts_set(24833, "The Dominance of God")
    assert not f.accepts_set(1, "One Piece Promotion Cards")
    assert not f.accepts_set(2, "Royal Blood Release Event Cards")
    # set scoping still applies with --all-rarities
    assert not CardFilter.for_game(example_cfg.games["one_piece"], all_rarities=True) \
        .accepts(in_set(1, "One Piece Promotion Cards", "R"), "Foil")


def test_include_sets_by_id_and_name(example_cfg):
    game = replace(example_cfg.games["one_piece"], include_sets=(24833, "royal blood"))
    f = CardFilter.for_game(game)
    assert f.accepts(in_set(24833, "The Dominance of God"), "Normal")
    assert f.accepts(in_set(99, "Royal Blood"), "Normal")
    assert not f.accepts(in_set(98, "Emperors in the New World"), "Normal")
    # exclusions win over inclusions
    assert not f.accepts(in_set(97, "Royal Blood Release Event Cards"), "Normal")


def test_cli_sets_replace_configured_include(example_cfg):
    game = replace(example_cfg.games["one_piece"], include_sets=("Royal Blood",))
    f = CardFilter.for_game(game, sets=(24833,))
    assert f.accepts_set(24833, "The Dominance of God")
    assert not f.accepts_set(99, "Royal Blood")


def test_set_selected_numeric_strings():
    assert parse_sets(["24833", "OP-1[0-9]", 5]) == (24833, "OP-1[0-9]", 5)
    assert set_selected((24833,), (), 24833, "anything")
    assert not set_selected((24833,), (), 1, "24833")  # IDs match IDs, not names


def test_full_art_is_not_regular_print(example_cfg):
    f = CardFilter.for_game(example_cfg.games["one_piece"])
    assert not f.accepts(card("Tsuru (Full Art)", "UC"), "Foil")

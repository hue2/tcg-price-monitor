"""Decide which cards and printings a game's check should consider."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .config import GameConfig
from .models import Card


def _matches_any(selectors: Iterable[int | str], group_id: int, group_name: str) -> bool:
    for s in selectors:
        if isinstance(s, int):
            if s == group_id:
                return True
        elif re.search(s, group_name, re.IGNORECASE):
            return True
    return False


def set_selected(include: tuple[int | str, ...], exclude: tuple[int | str, ...],
                 group_id: int, group_name: str) -> bool:
    if include and not _matches_any(include, group_id, group_name):
        return False
    return not _matches_any(exclude, group_id, group_name)


@dataclass(frozen=True)
class CardFilter:
    include_sets: tuple[int | str, ...]  # empty = all sets
    exclude_sets: tuple[int | str, ...]
    rarities: frozenset[str]  # casefolded; empty = all
    exclude_name: tuple[re.Pattern[str], ...]
    printings: frozenset[str]  # casefolded; empty = all

    @classmethod
    def for_game(cls, game: GameConfig, *, all_rarities: bool = False,
                 sets: tuple[int | str, ...] = ()) -> CardFilter:
        """`all_rarities` drops the rarity and variant filters (higher rarities included).

        `sets` (from --set) replaces the game's include_sets for this run. Set exclusions
        still apply in both cases: they scope the game.
        """
        return cls(
            include_sets=sets or game.include_sets,
            exclude_sets=game.exclude_sets,
            rarities=frozenset() if all_rarities else frozenset(r.casefold() for r in game.rarities),
            exclude_name=() if all_rarities else tuple(
                re.compile(p, re.IGNORECASE) for p in game.exclude_name_patterns),
            printings=frozenset(p.casefold() for p in game.printings),
        )

    def accepts_set(self, group_id: int, group_name: str) -> bool:
        return set_selected(self.include_sets, self.exclude_sets, group_id, group_name)

    def accepts(self, card: Card, printing: str) -> bool:
        if not self.accepts_set(card.group_id, card.group_name):
            return False
        if self.rarities and card.rarity.casefold() not in self.rarities:
            return False
        if any(p.search(card.name) for p in self.exclude_name):
            return False
        if self.printings and printing.casefold() not in self.printings:
            return False
        return True

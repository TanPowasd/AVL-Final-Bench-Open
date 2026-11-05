"""Simultaneous bilateral relations. Alliances do not silently create wars."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import RuleViolation


class Relation(str, Enum):
    PEACE = "PEACE"
    WAR = "WAR"
    TRUCE = "TRUCE"


@dataclass
class Pair:
    state: Relation = Relation.PEACE
    aggressors: frozenset[str] = frozenset()
    expiry_tick: int | None = None
    war_id: int = 0


@dataclass(frozen=True)
class AcceptedPeace:
    a: str
    b: str
    mode: str
    duration_cycles: int = 10


class Diplomacy:
    def __init__(self, actors: frozenset[str]):
        self.actors = actors
        self.pairs: dict[tuple[str, str], Pair] = {}
        self.next_war = 1

    def _key(self, a: str, b: str) -> tuple[str, str]:
        if a == b or a not in self.actors or b not in self.actors:
            raise RuleViolation("ERR_SCHEMA", "invalid bilateral pair")
        return tuple(sorted((a, b)))

    def relation(self, a: str, b: str) -> Relation:
        if a == b:
            return Relation.PEACE
        return self.pairs.get(self._key(a, b), Pair()).state

    def phase1(self, tick: int, *, treaties: tuple[AcceptedPeace, ...] = (),
               declarations: tuple[tuple[str, str], ...] = ()) -> tuple[tuple[str, str], ...]:
        """Expire → accepted treaties → simultaneous declarations; return breaks."""
        declared: dict[tuple[str, str], set[str]] = {}
        for a, b in declarations:
            declared.setdefault(self._key(a, b), set()).add(a)
        treaty_keys = set()
        for treaty in treaties:
            key = self._key(treaty.a, treaty.b)
            if treaty.mode not in ("armistice", "peace", "cession") or not 10 <= treaty.duration_cycles <= 720:
                raise RuleViolation("ERR_SCHEMA", "invalid accepted peace treaty")
            if key in treaty_keys:
                raise RuleViolation("ERR_SCHEMA", "conflicting accepted peace treaties")
            treaty_keys.add(key)
        # No mutation before the entire batch has been validated.
        for pair in self.pairs.values():
            if pair.state == Relation.TRUCE and pair.expiry_tick is not None and tick >= pair.expiry_tick:
                pair.state, pair.expiry_tick, pair.aggressors = Relation.PEACE, None, frozenset()
        for treaty in treaties:
            pair = self.pairs.setdefault(self._key(treaty.a, treaty.b), Pair())
            pair.state = Relation.TRUCE if treaty.mode == "armistice" else Relation.PEACE
            pair.expiry_tick = tick + 10 * treaty.duration_cycles if pair.state == Relation.TRUCE else None
            pair.aggressors = frozenset()
        breaks = []
        for key, aggressors in sorted(declared.items()):
            pair = self.pairs.setdefault(key, Pair())
            if pair.state == Relation.WAR:
                continue
            if pair.state == Relation.TRUCE:
                breaks.extend((actor, next(p for p in key if p != actor)) for actor in sorted(aggressors))
            pair.state, pair.expiry_tick, pair.aggressors = Relation.WAR, None, frozenset(aggressors)
            pair.war_id = self.next_war
            self.next_war += 1
        return tuple(breaks)

    def validate_kinetic(self, attacker: str, target: str) -> None:
        if self.relation(attacker, target) != Relation.WAR:
            raise RuleViolation("ERR_NOT_AT_WAR")

    def active_war_interest_pp(self, actor: str) -> int:
        return max((2 if actor in pair.aggressors else 1
                    for key, pair in self.pairs.items() if actor in key and pair.state == Relation.WAR), default=0)

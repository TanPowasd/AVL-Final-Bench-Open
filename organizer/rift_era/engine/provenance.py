"""Origin-preserving, globally loop-erased terminal-use receipts (07/11).

Transfers and sales do not recognize trade. Recognition happens exactly once
when a real, eligible terminal use consumes a quantity from a physical lot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from collections.abc import Mapping

from .errors import InvariantViolation
from .numeric import Q


class Use(str, Enum):
    CIVILIAN = "civilian"
    CIVIL_LOGISTICS = "civil_logistics"
    INDUSTRY = "industry"
    RESEARCH = "research"
    REMEDIATION = "remediation"
    MAINTENANCE = "maintenance"
    ACTION_LOSS = "action_loss"
    REPAIR = "repair"
    CONSTRUCTION = "construction"
    DESTRUCTION = "destruction"
    EXTRACTION = "extraction"
    RECYCLING = "recycling"
    MILITARY_SUPPORT = "military_support"


ELIGIBLE = frozenset({Use.CIVILIAN, Use.CIVIL_LOGISTICS, Use.INDUSTRY,
                      Use.RESEARCH, Use.REMEDIATION, Use.MAINTENANCE})


@dataclass(frozen=True)
class Leg:
    origin: str
    destination: str
    carrier: str
    length: int  # L1 path cells, never L2 hops
    eligible: bool = True  # military movement carries location, never civilian transit credit

    def __post_init__(self):
        if self.length <= 0 or self.origin == self.destination:
            raise ValueError("invalid physical transport leg")


def erase_loops(path: tuple[Leg, ...]) -> tuple[Leg, ...]:
    if not path:
        return ()
    nodes = [path[0].origin]
    kept: list[Leg] = []
    for leg in path:
        if leg.origin != nodes[-1]:
            raise InvariantViolation("discontinuous provenance path")
        if leg.destination in nodes:
            index = nodes.index(leg.destination)
            nodes = nodes[:index + 1]
            kept = kept[:index]
        else:
            nodes.append(leg.destination)
            kept.append(leg)
    return tuple(kept)


@dataclass(frozen=True)
class Lot:
    id: str
    origin_nation: str
    origin_hub: str
    good: str
    quantity_mU: int | Fraction
    path: tuple[Leg, ...] = ()
    already_recognized: bool = False  # salvaged/recycled material cannot restart rewards

    def split(self, quantity_mU: int | Fraction) -> tuple[Lot, Lot]:
        if not 0 < quantity_mU < self.quantity_mU:
            raise ValueError("split must leave both portions positive")
        args = (self.origin_nation, self.origin_hub, self.good)
        return (Lot(self.id + "/0", *args, quantity_mU, self.path, self.already_recognized),
                Lot(self.id + "/1", *args, self.quantity_mU - quantity_mU, self.path, self.already_recognized))


@dataclass(frozen=True)
class TerminalReceipt:
    event_id: str
    cycle: int
    good: str
    quantity_mU: int | Fraction
    origin: str
    consumer: str
    value: Fraction
    transit: Mapping[str, Fraction]
    credit_weighted_export: Fraction
    credit_weighted_aid: Fraction


def terminal_receipt(event_id: str, cycle: int, lot: Lot, quantity_mU: int | Fraction, *,
                     consumer: str, final_hub: str, use: Use, weight: Fraction,
                     origin_credit: Fraction, initial_shortest_length: int,
                     aid: bool = False) -> TerminalReceipt | None:
    if not 0 < quantity_mU <= lot.quantity_mU or weight < 0 or not 0 <= origin_credit <= 1 or initial_shortest_length < 0:
        raise ValueError("invalid terminal use")
    if lot.path:
        if lot.path[0].origin != lot.origin_hub or lot.path[-1].destination != final_hub:
            raise InvariantViolation("terminal-use location does not match actual route")
    elif final_hub != lot.origin_hub:
        raise InvariantViolation("goods cannot appear at another hub without transport")
    if use not in ELIGIBLE or lot.already_recognized:
        return None
    value = Q(quantity_mU, 1000) * weight
    path = erase_loops(lot.path)
    length = sum(leg.length for leg in path)
    transit: dict[str, Fraction] = {}
    if length and final_hub != lot.origin_hub:
        for leg in path:
            if not leg.eligible:
                continue
            transit[leg.carrier] = transit.get(leg.carrier, Q(0)) + value * min(initial_shortest_length, length) * leg.length / length
    contribution = value * origin_credit if consumer != lot.origin_nation else Q(0)
    return TerminalReceipt(event_id, cycle, lot.good, quantity_mU, lot.origin_nation,
                           consumer, value, transit, Q(0) if aid else contribution,
                           contribution if aid else Q(0))


class ReceiptBook:
    """Bounded 20-cycle statistics. Consume lot remainders to prevent replay.

    Event IDs are tick-local and must be committed in nondecreasing ticks. Old
    events cannot be reintroduced after pruning the deduplication set.
    """

    def __init__(self):
        self.tick = -1
        self.seen: set[str] = set()
        self._windows: dict[int, dict[tuple[str, str], Fraction]] = {}
        self.lots: dict[str, Lot] = {}

    def add_lot(self, lot: Lot) -> None:
        if lot.id in self.lots or lot.quantity_mU <= 0:
            raise InvariantViolation("duplicate or empty physical lot")
        self.lots[lot.id] = lot

    def consume(self, tick: int, lot_id: str, quantity_mU: int | Fraction, *, event_id: str,
                cycle: int, consumer: str, final_hub: str, use: Use, weight: Fraction,
                origin_credit: Fraction, initial_shortest_length: int, aid: bool = False) -> TerminalReceipt | None:
        if tick < self.tick:
            raise InvariantViolation("backdated receipt")
        if tick == self.tick and event_id in self.seen:
            raise InvariantViolation("duplicate consumption event")
        lot = self.lots[lot_id]
        receipt = terminal_receipt(event_id, cycle, lot, quantity_mU, consumer=consumer,
            final_hub=final_hub, use=use, weight=weight, origin_credit=origin_credit,
            initial_shortest_length=initial_shortest_length, aid=aid)
        if tick > self.tick:
            self.tick, self.seen = tick, set()
        self.seen.add(event_id)
        if quantity_mU == lot.quantity_mU:
            del self.lots[lot_id]
        else:
            self.lots[lot_id] = Lot(lot.id, lot.origin_nation, lot.origin_hub, lot.good,
                                    lot.quantity_mU - quantity_mU, lot.path, lot.already_recognized)
        if receipt is not None:
            row = self._windows.setdefault(cycle, {})
            contributions = {(receipt.origin, 'export'): receipt.credit_weighted_export,
                             (receipt.origin, 'aid'): receipt.credit_weighted_aid}
            contributions.update({(p, 'transit'): v for p, v in receipt.transit.items()})
            for key, value in contributions.items():
                row[key] = row.get(key, Q(0)) + value
        self._windows = {c: row for c, row in self._windows.items() if c >= cycle - 19}
        return receipt

    def mean20(self, cycle: int, owner: str, kind: str) -> Fraction:
        return sum((row.get((owner, kind), Q(0)) for c, row in self._windows.items()
                    if cycle - 19 <= c <= cycle), Q(0)) / 20

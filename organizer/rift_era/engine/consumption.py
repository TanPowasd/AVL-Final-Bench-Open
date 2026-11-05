"""P7 material-use statistics; queries never advance or recognize receipts.

Military/destructive uses increase permanent per-owner, per-good SinkDebt.
Normal use contributes to the twenty-cycle denominator. Sale, custody and
prepayment produce no MaterialUse and therefore cannot earn consumption credit.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from fractions import Fraction
import heapq

from .errors import InvariantViolation
from .materials import MaterialBook, MaterialUse
from .numeric import Q
from .provenance import ELIGIBLE, Lot, Use, terminal_receipt


class InitialPaths:
    """Frozen initial legal cargo graph; caches are derivable, not game state."""
    def __init__(self, edges: tuple[tuple[str, str, int], ...]):
        graph = defaultdict(list)
        for a, b, length in edges:
            if length <= 0:
                raise ValueError('nonpositive initial route length')
            graph[a].append((b, length))
            graph[b].append((a, length))
        self._graph = {a: tuple(sorted(neighbors)) for a, neighbors in graph.items()}
        self._cache: dict[str, dict[str, int]] = {}

    def distance(self, origin: str, destination: str) -> int:
        if origin == destination:
            return 0
        if origin not in self._cache:
            distances, queue = {origin: 0}, [(0, origin)]
            while queue:
                distance, node = heapq.heappop(queue)
                if distances[node] != distance:
                    continue
                for neighbor, length in self._graph.get(node, ()):
                    candidate = distance + length
                    if candidate < distances.get(neighbor, candidate + 1):
                        distances[neighbor] = candidate
                        heapq.heappush(queue, (candidate, neighbor))
            self._cache[origin] = distances
        return self._cache[origin].get(destination, 0)


class Consumption:
    def __init__(self):
        self.tick = -1
        self.cycle = -1
        self.sink_debt: dict[tuple[str, str], Fraction] = {}
        self._eligible: dict[int, dict[tuple[str, str], Fraction]] = {}
        self._receipts: dict[int, dict[tuple[str, str], Fraction]] = {}
        self._synthetic: dict[int, dict[tuple[str, str], Fraction]] = {}
        self._construction: dict[int, dict[tuple[str, str], Fraction]] = {}

    def advance(self, tick: int, cycle: int, events: tuple[MaterialUse, ...], *,
                weights: Mapping[str, Fraction],
                shortest: Callable[[str, str], int], services: tuple = ()) -> None:
        if tick <= self.tick or cycle < self.cycle:
            raise InvariantViolation('duplicate or backdated consumption settlement')
        if any(event.tick != tick for event in events) or len({e.id for e in events}) != len(events):
            raise InvariantViolation('consumption events from another tick or duplicate event')
        eligible, receipts, synthetic, debt, construction = (defaultdict(Fraction) for _ in range(5))
        for event in events:
            if event.quantity_mU <= 0:
                raise InvariantViolation('empty consumption event')
            key = (event.owner, event.good)
            if event.use in ELIGIBLE or event.use == Use.RECYCLING:
                eligible[key] += event.quantity_mU
            elif event.use == Use.CONSTRUCTION and event.commissioned:
                construction[key] += event.quantity_mU
            elif event.use in {Use.ACTION_LOSS, Use.MILITARY_SUPPORT, Use.DESTRUCTION, Use.REPAIR}:
                debt[key] += event.quantity_mU
            if event.use == Use.CIVILIAN and event.good == 'food':
                synthetic[(event.hub, 'food')] += event.quantity_mU
                if event.origin.synthetic_food:
                    synthetic[(event.hub, 'synthetic')] += event.quantity_mU
            if event.use not in ELIGIBLE or event.origin.recognized:
                continue
            origin = event.origin
            receipt = terminal_receipt(event.id, cycle,
                Lot(event.id, origin.nation, origin.hub, event.good, event.quantity_mU, origin.path),
                event.quantity_mU, consumer=event.owner, final_hub=event.hub, use=event.use,
                weight=weights[event.good], origin_credit=event.origin_credit,
                initial_shortest_length=shortest(origin.hub, event.hub))
            receipts[(origin.nation, 'export')] += receipt.credit_weighted_export
            for carrier, value in receipt.transit.items():
                receipts[(carrier, 'transit')] += value
        # Flow services have no physical origin batch or transferable stock.
        # Their unique completed delivery and real payment are the evidence.
        if (any(e.tick!=tick or e.quantity_mU<=0 or e.paid_ru_mU<0 or not 0<=e.credit<=1 for e in services)
                or len({e.id for e in services})!=len(services)
                or {e.id for e in services}&{e.id for e in events}):
            raise InvariantViolation('invalid or duplicate inference-service receipt')
        for event in services:
            if event.buyer==event.seller or event.use not in {'research','production','finance','npc_civilian'}:
                continue
            if event.paid_ru_mU:
                eligible[event.buyer,'compute']+=event.quantity_mU
                receipts[event.seller,'export']+=Q(event.quantity_mU,1000)*weights['compute']*event.credit
        # All event/route/weight checks precede writes to the score inputs.
        for key, value in debt.items():
            self.sink_debt[key] = self.sink_debt.get(key, Q(0)) + value
        for table, updates in ((self._eligible, eligible), (self._receipts, receipts), (self._synthetic, synthetic),
                               (self._construction, construction)):
            current = table.setdefault(cycle, {})
            for key, value in updates.items():
                if value:
                    current[key] = current.get(key, Q(0)) + value
            for old in tuple(table):
                if old < cycle - 19:
                    del table[old]
        self.tick, self.cycle = tick, cycle

    def consumption20(self, owner: str, good: str) -> Fraction:
        key = (owner, good)
        return sum((row.get(key, Q(0)) + min(row.get(key, Q(0)) / 4,
                    self._construction.get(cycle, {}).get(key, Q(0))) for cycle, row in self._eligible.items()), Q(0))

    def receipt_mean20(self, owner: str, kind: str) -> Fraction:
        return sum((row.get((owner, kind), Q(0)) for row in self._receipts.values()), Q(0)) / 20

    def synthetic_share(self, hub: str, cycle: int) -> Fraction:
        row = self._synthetic.get(cycle, {})
        quantity = row.get((hub, 'food'), Q(0))
        return row.get((hub, 'synthetic'), Q(0)) / quantity if quantity else Q(0)

    def eta(self, materials: MaterialBook, owner: str, good: str) -> Fraction:
        from .scoring import eta_sink
        return eta_sink(materials.physical_by_owner(owner).get(good, Q(0)),
                        self.sink_debt.get((owner, good), Q(0)), self.consumption20(owner, good))

    def audit(self) -> None:
        if any(q < 0 for q in self.sink_debt.values()):
            raise InvariantViolation('negative permanent sink debt')
        for table in (self._eligible, self._receipts, self._synthetic, self._construction):
            if len(table) > 20 or any(not self.cycle - 19 <= cycle <= self.cycle for cycle in table):
                raise InvariantViolation('unbounded or future consumption window')
            if any(q < 0 for row in table.values() for q in row.values()):
                raise InvariantViolation('negative material recognition')

    def snapshot(self) -> dict:
        def rows(values):
            return [[*key, value] for key, value in sorted(values.items())]
        return {'tick': self.tick, 'cycle': self.cycle, 'sink_debt': rows(self.sink_debt),
                'eligible': [[c, rows(row)] for c, row in sorted(self._eligible.items())],
                'receipts': [[c, rows(row)] for c, row in sorted(self._receipts.items())],
                'synthetic': [[c, rows(row)] for c, row in sorted(self._synthetic.items())],
                'construction': [[c, rows(row)] for c, row in sorted(self._construction.items())]}

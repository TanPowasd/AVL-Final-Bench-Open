"""Plant state machines and an exact integer reference dispatcher (05 §1–4).

Dispatch receipts are authoritative. Hub average prices never reprice receipts.
This reference implementation prioritizes transparent integer semantics; its
work counter is exposed for optimization and throughput acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from heapq import heappop, heappush
from collections.abc import Mapping

from .determinism import digest
from .errors import InvariantViolation
from .numeric import Q, ceil, clip, floor, fair_service


class PlantMode(str, Enum):
    OFF = "OFF"
    STARTING = "STARTING"
    ON = "ON"
    TRIPPED = "TRIPPED"
    MAINTENANCE = "MAINTENANCE"
    SCRAM = "SCRAM"


@dataclass(frozen=True)
class PlantSpec:
    recipe: str
    nameplate_mU: int  # per cycle
    minimum: Fraction
    ramp: Fraction
    start_ticks: int
    nuclear: bool = False
    ignition_mU_tick: int = 0
    maximum: Fraction = Q(1)


PLANTS = {
    "P070": PlantSpec("P070", 10000, Q(2, 5), Q(1, 10), 5),
    "P071": PlantSpec("P071", 12000, Q(1, 5), Q(1, 4), 2),
    "P111": PlantSpec("P111", 14000, Q(1, 5), Q(1, 2), 1),
    "P112": PlantSpec("P112", 15000, Q(0), Q(1), 1, maximum=Q(6, 5)),
    "P113": PlantSpec("P113", 8000, Q(0), Q(1), 0),
    "P114": PlantSpec("P114", 7000, Q(0), Q(1), 0),
    "P072": PlantSpec("P072", 9000, Q(0), Q(1), 0),
    "P074": PlantSpec("P074", 3000, Q(0), Q(1), 0),
    "P073": PlantSpec("P073", 20000, Q(7, 10), Q(1, 20), 100, True),
    "P083": PlantSpec("P083", 30000, Q(7, 10), Q(1, 20), 100, True),
    "P086": PlantSpec("P086", 80000, Q(1, 2), Q(1, 10), 100, False, 4000),
}

# Chapter 05: outage basis points/cycle, planned interval and stop cycles.
PLANT_CALENDAR = {
    'P070': (50, 100, 2), 'P071': (40, 100, 2), 'P111': (30, 100, 2),
    'P112': (20, 200, 2), 'P113': (10, 0, 0), 'P114': (10, 0, 0),
    'P072': (10, 0, 0), 'P074': (30, 0, 0), 'P073': (20, 200, 10),
    'P083': (20, 300, 10), 'P086': (100, 200, 5),
}


@dataclass
class Plant:
    id: str
    spec: PlantSpec
    mode: PlantMode = PlantMode.OFF
    online: bool = False
    output_fraction: Fraction = Q(0)
    timer: int = 0
    shortage_streak: int = 0
    thermal_stress: Fraction = Q(0)
    pump_previous: Fraction | None = None
    commissioned_cycle: int = 0
    maintenance_shift: int = 0
    next_maintenance_cycle: int | None = None

    def __post_init__(self):
        period = PLANT_CALENDAR.get(self.spec.recipe, (0, 0, 0))[1]
        if period and self.next_maintenance_cycle is None:
            self.next_maintenance_cycle = self.commissioned_cycle + period

    def prepare(self, availability: Fraction, fuel: Fraction, water: Fraction, *,
                start_power_paid: bool = True, scram_materials_paid: bool = False) -> tuple[Fraction, Fraction]:
        """P3a. Previous pump result is immutable; no retroactive SOC rescue."""
        availability, fuel, water = (clip(v, Q(0), self.spec.maximum) for v in (availability, fuel, water))
        if self.spec.nuclear and self.mode != PlantMode.SCRAM and self.pump_previous is not None and self.pump_previous < 1:
            return self.scram()
        if self.mode in (PlantMode.TRIPPED, PlantMode.MAINTENANCE, PlantMode.SCRAM):
            self.timer = max(0, self.timer - 1)
            if self.timer or (self.mode == PlantMode.SCRAM and not scram_materials_paid):
                self.output_fraction = Q(0)
                return Q(0), Q(0)
            if self.mode == PlantMode.SCRAM:
                self.thermal_stress = Q(0)
                self.pump_previous = None
                self.mode = PlantMode.OFF
                return Q(0), Q(0)
            self.mode = PlantMode.OFF
        if not self.online:
            self.mode, self.output_fraction = PlantMode.OFF, Q(0)
            return Q(0), Q(0)
        if self.mode == PlantMode.OFF:
            self.mode = PlantMode.STARTING
            self.timer = self.spec.start_ticks
        if self.spec.nuclear:
            if water < Q(1, 2) or (self.pump_previous is not None and self.pump_previous < 1):
                return self.scram()
            self.thermal_stress = max(Q(0), self.thermal_stress - Q(1, 100)) if water == 1 else self.thermal_stress + 1 - water
            if self.thermal_stress >= 2:
                return self.scram()
        ceiling = min(availability, fuel, water)
        if self.mode == PlantMode.STARTING:
            if self.timer:
                if fuel < 1 or water < 1 or not start_power_paid:
                    self.shortage_streak += 1
                    if self.spec.ignition_mU_tick and not start_power_paid:
                        self.timer = self.spec.start_ticks
                    if self.shortage_streak >= 10:
                        self.mode = PlantMode.OFF
                    return Q(0), Q(0)
                self.shortage_streak = 0
                self.timer -= 1
                return Q(0), Q(0)
            if ceiling < Q(3, 10) * self.spec.minimum or ceiling == 0:
                return Q(0), Q(0)
            self.mode = PlantMode.ON
            self.output_fraction = self.spec.minimum * ceiling
            if self.spec.minimum == 0:
                return Q(0), min(ceiling, self.spec.ramp)
            return self.output_fraction, self.output_fraction
        if ceiling == 0 or ceiling < Q(3, 10) * self.spec.minimum:
            self.mode, self.output_fraction = PlantMode.OFF, Q(0)
            return Q(0), Q(0)
        high = min(ceiling, self.output_fraction + self.spec.ramp)
        low = min(high, max(self.spec.minimum * ceiling, self.output_fraction - self.spec.ramp))
        return low, high

    def scram(self) -> tuple[Fraction, Fraction]:
        self.mode, self.timer, self.output_fraction = PlantMode.SCRAM, 100, Q(0)
        return Q(0), Q(0)

    def commit(self, fraction: Fraction, bounds: tuple[Fraction, Fraction]) -> None:
        if not bounds[0] <= fraction <= bounds[1]:
            raise InvariantViolation("plant output outside feasible interval")
        self.output_fraction = fraction


@dataclass
class GridRecovery:
    recovering: bool = False

    def update(self, capacity_mU: int, critical_demand_mU: int) -> bool:
        """Return True exactly on a new blackout, with recovery hysteresis."""
        if self.recovering:
            if capacity_mU * 5 >= critical_demand_mU * 4:
                self.recovering = False
        elif capacity_mU * 10 < critical_demand_mU * 7:
            self.recovering = True
            return True
        return False


def storage_auto(history: tuple[Fraction, ...], average_charge_cost: Fraction,
                 efficiency: Fraction) -> tuple[str, Fraction]:
    if not history:
        return "idle", Q(0)
    prices = sorted(history[-10:])
    n = len(prices)
    median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
    if history[-1] < Q(85, 100) * median:
        return "charge", Q(85, 100) * median
    if history[-1] > Q(115, 100) * median:
        return "discharge", max(average_charge_cost / efficiency, Q(115, 100) * median)
    return "idle", Q(0)


@dataclass(frozen=True)
class PowerEdge:
    id: str
    a: str
    b: str
    capacity_mU: int
    survival: Fraction
    owner: str
    toll: Fraction = Q(1, 100)

    def __post_init__(self):
        if self.capacity_mU < 0 or not 0 < self.survival <= 1 or self.toll < 0 or self.a == self.b:
            raise ValueError("invalid power edge")


@dataclass(frozen=True)
class Supply:
    id: str
    hub: str
    owner: str
    quantity_mU: int
    ask_ru: Fraction


@dataclass(frozen=True)
class Load:
    id: str
    hub: str
    owner: str
    demand_mU: int
    rank: int
    limit_ru: Fraction
    budget_ru_mU: Fraction
    band: str = ''
    user_budget_ru_mU: Fraction | None = None
    user_cap_ru: Fraction | None = None

    def __post_init__(self):
        if (self.user_cap_ru is None) != (self.user_budget_ru_mU is None):
            raise ValueError('incomplete power funding policy')
        if self.user_cap_ru is not None and (self.user_cap_ru < 0 or not 0 <= self.user_budget_ru_mU <= self.budget_ru_mU):
            raise ValueError('invalid power funding policy')

    def affordable(self, received: int, spent: Fraction) -> bool:
        if spent > self.budget_ru_mU:
            return False
        if self.user_cap_ru is None or self.user_budget_ru_mU is None:
            return True
        subsidy = self.budget_ru_mU - self.user_budget_ru_mU
        return spent <= min(self.user_budget_ru_mU, self.user_cap_ru * received) + subsidy

    def local_capacity(self, price: Fraction, received: int, spent: Fraction) -> int:
        quantity = self.demand_mU - received
        if price:
            quantity = min(quantity, floor((self.budget_ru_mU - spent) / price))
        if self.user_cap_ru is not None and self.user_budget_ru_mU is not None and price > self.user_cap_ru:
            subsidy = self.budget_ru_mU - self.user_budget_ru_mU
            quantity = min(quantity, floor((self.user_cap_ru * received + subsidy - spent) / (price - self.user_cap_ru)))
        return max(0, quantity)


@dataclass(frozen=True)
class PowerReceipt:
    source: str
    load: str
    sent_mU: int
    received_mU: int
    edges: tuple[tuple[str, int], ...]
    source_payment_ru_mU: Fraction
    tolls_ru_mU: tuple[tuple[str, Fraction], ...]

    @property
    def cost_ru_mU(self) -> Fraction:
        return self.source_payment_ru_mU + sum((v for _, v in self.tolls_ru_mU), Q(0))


@dataclass(frozen=True)
class Dispatch:
    receipts: tuple[PowerReceipt, ...]
    received: Mapping[str, int]
    spent: Mapping[str, Fraction]
    source_remaining: Mapping[str, int]
    edge_remaining: Mapping[str, int]
    iterations: int


def dispatch_reference(seed: int, tick: int, supplies: tuple[Supply, ...], loads: tuple[Load, ...],
             edges: tuple[PowerEdge, ...]) -> Dispatch:
    """Executable oracle for the specified 1mU sequence, never floating flow."""
    if len({s.id for s in supplies}) != len(supplies) or len({l.id for l in loads}) != len(loads) or len({e.id for e in edges}) != len(edges):
        raise ValueError("duplicate grid ID")
    if any(s.quantity_mU < 0 or s.ask_ru < 0 for s in supplies) or any(
            l.demand_mU < 0 or l.budget_ru_mU < 0 or not 0 <= l.rank <= 5 or l.limit_ru < 0 for l in loads):
        raise ValueError("invalid grid bid")
    sources = {s.id: s for s in supplies}
    edge_map = {e.id: e for e in edges}
    graph: dict[str, list[tuple[str, PowerEdge]]] = {}
    for edge in edges:
        graph.setdefault(edge.a, []).append((edge.b, edge))
        graph.setdefault(edge.b, []).append((edge.a, edge))
    source_left = {s.id: s.quantity_mU for s in supplies}
    edge_left = {e.id: e.capacity_mU for e in edges}
    received = {l.id: 0 for l in loads}
    spent = {l.id: Q(0) for l in loads}
    receipt_map: dict[tuple[str, str, tuple[str, ...]], PowerReceipt] = {}
    iterations = 0

    def options(load: Load, local: bool):
        """Enumerate simple paths by nondecreasing nonnegative delivered cost.

        Carry visited sets: cycles, including cheap cyclic rounding artifacts,
        cannot become candidate paths. Infeasible paths are skipped, not loads.
        """
        queue = []
        for source in supplies:
            if source_left[source.id] <= 0:
                continue
            if local and source.hub != load.hub:
                continue
            heappush(queue, (source.ask_ru, 0, digest(seed, "power_path", tick, source.id),
                             source.id, source.hub, (), (source.hub,)))
        while queue:
            cost, hops, tie, sid, hub, path, visited = heappop(queue)
            if cost > load.limit_ru:
                continue
            if hub == load.hub:
                yield sid, path
                continue
            if local:
                continue
            for dest, edge in graph.get(hub, ()):
                if dest in visited or edge_left[edge.id] == 0:
                    continue
                next_path = path + (edge.id,)
                next_cost = (cost + edge.toll) / edge.survival
                heappush(queue, (next_cost, hops + 1,
                                 digest(seed, "power_path", tick, sid, *next_path),
                                 sid, dest, next_path, visited + (dest,)))

    def candidate(load: Load, local: bool):
        for sid, path in options(load, local):
            previous = receipt_map.get((sid, load.id, path))
            old_flows = dict(previous.edges) if previous else {}
            needed, flows = (previous.received_mU + 1 if previous else 1), []
            for eid in reversed(path):
                needed = ceil(Q(needed) / edge_map[eid].survival)
                flows.append((eid, needed - old_flows.get(eid, 0)))
            flows.reverse()
            needed -= previous.sent_mU if previous else 0
            if needed > source_left[sid] or any(q > edge_left[e] for e, q in flows):
                continue
            tolls = tuple((edge_map[e].owner, edge_map[e].toll * q) for e, q in flows)
            receipt = PowerReceipt(sid, load.id, needed, 1, tuple(flows),
                                   needed * sources[sid].ask_ru, tolls)
            cumulative_cost = receipt.cost_ru_mU + (previous.cost_ru_mU if previous else 0)
            cumulative_received = 1 + (previous.received_mU if previous else 0)
            if (cumulative_cost > load.limit_ru * cumulative_received or
                    not load.affordable(received[load.id] + 1, spent[load.id] + receipt.cost_ru_mU)):
                continue
            return path, receipt
        return None

    for rank in range(6):
        ranked = [l for l in loads if l.rank == rank and l.demand_mU]
        bands = sorted({l.limit_ru for l in ranked}, reverse=True) if rank == 5 else [None]
        for band in bands:
            group = [l for l in ranked if band is None or l.limit_ru == band]
            for local in (True, False):
                frozen = set()
                while True:
                    active = [l for l in group if l.id not in frozen and received[l.id] < l.demand_mU]
                    if not active:
                        break
                    load = min(active, key=lambda l: (Q(received[l.id], l.demand_mU),
                                                      digest(seed, "power_load", tick, rank, l.id), l.id))
                    choice = candidate(load, local)
                    iterations += 1
                    if choice is None:
                        frozen.add(load.id)
                        continue
                    path, receipt = choice
                    source_left[receipt.source] -= receipt.sent_mU
                    for eid, amount in receipt.edges:
                        edge_left[eid] -= amount
                    received[load.id] += 1
                    spent[load.id] += receipt.cost_ru_mU
                    key = (receipt.source, load.id, path)
                    previous = receipt_map.get(key)
                    if previous:
                        receipt = PowerReceipt(receipt.source, load.id, previous.sent_mU + receipt.sent_mU,
                            previous.received_mU + 1,
                            tuple((eid, a + b) for (eid, a), (_, b) in zip(previous.edges, receipt.edges)),
                            previous.source_payment_ru_mU + receipt.source_payment_ru_mU,
                            tuple((owner, a + b) for (owner, a), (_, b) in zip(previous.tolls_ru_mU, receipt.tolls_ru_mU)))
                    receipt_map[key] = receipt
    return Dispatch(tuple(receipt_map[k] for k in sorted(receipt_map)), received, spent,
                    source_left, edge_left, iterations)


def dispatch(seed: int, tick: int, supplies: tuple[Supply, ...], loads: tuple[Load, ...],
             edges: tuple[PowerEdge, ...]) -> Dispatch:
    if edges:
        return dispatch_reference(seed, tick, supplies, loads, edges)
    if len({s.id for s in supplies}) != len(supplies) or len({l.id for l in loads}) != len(loads):
        raise ValueError('duplicate grid ID')
    if any(s.quantity_mU < 0 or s.ask_ru < 0 for s in supplies) or any(
            l.demand_mU < 0 or l.budget_ru_mU < 0 or not 0 <= l.rank <= 5 or l.limit_ru < 0 for l in loads):
        raise ValueError('invalid grid bid')
    remaining = {s.id: s.quantity_mU for s in supplies}
    received, spent = {l.id: 0 for l in loads}, {l.id: Q(0) for l in loads}
    receipts, iterations = [], 0
    for hub in sorted({l.hub for l in loads}):
        sources = sorted((s for s in supplies if s.hub == hub),
                         key=lambda s: (s.ask_ru, digest(seed, 'power_path', tick, s.id), s.id))
        for rank in range(6):
            ranked = [l for l in loads if l.hub == hub and l.rank == rank and l.demand_mU]
            bands = sorted({l.limit_ru for l in ranked}, reverse=True) if rank == 5 else [None]
            for band in bands:
                group = [l for l in ranked if band is None or l.limit_ru == band]
                for source in sources:
                    eligible = [l for l in group if l.limit_ru >= source.ask_ru]
                    caps = {l.id: l.local_capacity(source.ask_ru, received[l.id], spent[l.id])
                            for l in eligible}
                    allocation = fair_service(remaining[source.id], {l.id: l.demand_mU for l in eligible},
                        received, caps, key=(seed, 'power_load', tick, rank))
                    iterations += 1
                    for lid, amount in allocation.items():
                        if amount:
                            price = amount * source.ask_ru
                            receipts.append(PowerReceipt(source.id, lid, amount, amount, (), price, ()))
                            remaining[source.id] -= amount
                            received[lid] += amount
                            spent[lid] += price
    return Dispatch(tuple(sorted(receipts, key=lambda r: (r.source, r.load))), received, spent, remaining, {}, iterations)

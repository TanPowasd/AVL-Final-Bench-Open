"""Local compute budgets, dated accelerator cohorts and path-independent training."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from fractions import Fraction

from .catalog import Catalog
from .errors import RuleViolation
from .numeric import Q, clip, proportional, floor
from .research import rational_power


def log10_q(value: Fraction) -> Fraction:
    with localcontext() as ctx:
        ctx.prec = 50
        return Q((Decimal(value.numerator) / Decimal(value.denominator)).log10())


@dataclass(frozen=True)
class Model:
    id: str
    owner: str
    created_cycle: int
    raw_compute: Fraction
    effective_compute: Fraction

    def capability(self, algorithm: Fraction) -> Fraction:
        return 10 * log10_q(1 + self.effective_compute / 1000) * algorithm

    def copy_for(self, model_id: str, buyer: str, cycle: int, gamma: Fraction) -> Model:
        if not 0 < gamma <= 1:
            raise ValueError("invalid model transfer factor")
        effective = 1000 * (rational_power(1 + self.effective_compute / 1000, gamma) - 1)
        return Model(model_id, buyer, cycle, effective, effective)


def data_efficiency(raw: Fraction, data: Fraction) -> Fraction:
    if raw <= 0:
        return Q(0)
    return min(Q(1), rational_power(max(Q(0), data) / (20 * rational_power(raw, Q(1, 2))), Q(1, 2)))


def train_model(model_id: str, owner: str, cycle: int, compute: Fraction,
                data: Fraction, base: Model | None = None) -> Model:
    if compute < 0 or data < 0:
        raise ValueError("negative training resources")
    raw0 = base.raw_compute if base else Q(0)
    eff0 = base.effective_compute if base else Q(0)
    raw = raw0 + compute
    effective = max(eff0, eff0 + data_efficiency(raw, data) * raw - data_efficiency(raw0, data) * raw0)
    return Model(model_id, owner, cycle, raw, effective)


@dataclass
class DataBook:
    raw: Fraction = Q(0)
    curated_human: Fraction = Q(0)
    synthetic: Fraction = Q(0)
    licensed_from: dict[str, Fraction] = field(default_factory=dict)

    @property
    def effective(self) -> Fraction:
        return (self.curated_human + Q(3, 10) * self.raw + Q(4, 5) * sum(self.licensed_from.values(), Q(0)) +
                Q(1, 2) * min(self.synthetic, self.curated_human))

    def license(self, source: str, source_human_data: Fraction) -> None:
        if source_human_data < 0:
            raise ValueError("negative licensed data")
        self.licensed_from[source] = max(self.licensed_from.get(source, Q(0)), source_human_data / 2)


@dataclass(frozen=True)
class AcceleratorCohort:
    installed_cycle: int
    quantity: int

    def online(self, cycle: int) -> Fraction:
        return self.quantity * clip(1 - Q(cycle - self.installed_cycle, 400), Q(0), Q(1))


@dataclass
class DataCenter:
    id: str
    hub: str
    generation: int
    cohorts: list[AcceleratorCohort] = field(default_factory=list)
    availability: Fraction = Q(1)

    def capacity_mU(self, cycle: int) -> int:
        return floor(sum((c.online(cycle) for c in self.cohorts), Q(0)) * (1, 3, 8)[self.generation - 1] * 1000 * self.availability)

    def refill(self, cycle: int, quantity: int) -> None:
        if type(quantity) is not int or not 1 <= quantity <= 50:
            raise RuleViolation("ERR_SCHEMA")
        if sum((c.online(cycle) for c in self.cohorts), Q(0)) + quantity > 50:
            raise RuleViolation("ERR_CAPACITY_EXCEEDED")
        self.cohorts = [c for c in self.cohorts if c.online(cycle) > 0]
        self.cohorts.append(AcceleratorCohort(cycle, quantity))


TASK_TECH = {'research': 'AI04', 'production': 'AI06', 'finance': 'AI07',
             'cyber': 'AI09', 'market': 'AI04', 'grid': 'AI06', 'align': 'AI03', 'train': 'AI02'}


def validate_budget(hub: str, allocations: Mapping[str, int], centers: tuple[DataCenter, ...],
                     cycle: int, known: frozenset[str]) -> None:
    if set(allocations) - TASK_TECH.keys() or any(type(a) is not int or a < 0 for a in allocations.values()):
        raise RuleViolation("ERR_SCHEMA")
    if any(a and TASK_TECH[k] not in known for k, a in allocations.items()):
        raise RuleViolation("ERR_TECH_LOCKED")
    if sum(allocations.values()) > sum(d.capacity_mU(cycle) for d in centers if d.hub == hub):
        raise RuleViolation("ERR_CAPACITY_EXCEEDED")


def shrink_existing_budget(seed: int, cycle: int, hub: str, allocations: Mapping[str, int],
                            capacity_mU: int) -> dict[str, int]:
    return proportional(capacity_mU, allocations, key=(seed, 'compute_budget', cycle, hub))


def unlock_milestones(catalog: Catalog, known: frozenset[str], capability: Fraction) -> tuple[frozenset[str], Fraction]:
    result = set(known)
    algorithm_gain, breakthrough = Q(0), False
    for tid, node in sorted(catalog.milestones.items()):
        if tid in result or not set(node['requires_all']) <= result or capability < node['cap_threshold']:
            continue
        algorithm = node['milestone'] in ('algorithm', 'frontier')
        if algorithm and breakthrough:
            continue
        result.add(tid)
        if algorithm:
            breakthrough = True
            algorithm_gain += Q(15, 100)
    # Caller applies gain NEXT cycle. The frozen capability is never recomputed here.
    return frozenset(result), algorithm_gain


def incident_probability(capability: Fraction, align: Fraction, frontier: bool) -> Fraction:
    return clip(Q(3 if frontier else 2, 1000) * max(Q(0), capability - align), Q(0), Q(1))

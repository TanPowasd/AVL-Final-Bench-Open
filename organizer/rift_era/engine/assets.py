"""Damage and repairs track remaining construction cost independently of health."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .errors import InvariantViolation, RuleViolation
from .numeric import Q, ceil
from .capital_book import proportional_loss


@dataclass
class Asset:
    id: str
    owner: str
    hub: str
    original_nmU: dict[str, int]
    health_mU: int = 1000
    book_nmU: dict[str, Fraction] = field(default_factory=dict)
    maintenance_debt: Fraction = Q(0)
    repair: Repair | None = None
    maintenance_offline: bool = False
    version: int = 0
    under_construction: bool = False
    work_locked: bool = False
    book_rounding_steps: int = 0

    def __post_init__(self):
        if not self.book_nmU:
            self.book_nmU = {g: Q(q) * self.health_mU / 1000 for g, q in self.original_nmU.items()}

    @property
    def availability(self) -> Fraction:
        if self.repair is not None or self.maintenance_offline or self.under_construction or self.work_locked:
            return Q(0)
        return max(Q(1, 5), 1 - Q(3, 10) * self.maintenance_debt) * Q(self.health_mU, 1000)

    def damage(self, amount_mU: int) -> dict[str, Fraction]:
        if type(amount_mU) is not int or amount_mU < 0:
            raise ValueError("invalid damage")
        damage = min(self.health_mU, amount_mU)
        if damage == 0:
            return {}
        removed = proportional_loss(self.book_nmU, damage, self.health_mU)
        for g, q in removed.items():
            self.book_nmU[g] -= q
        self.book_rounding_steps += len(removed)
        self.health_mU -= damage
        self.version += 1
        # Caller refunds unspent escrow before discarding an interrupted repair.
        if self.repair is not None:
            self.repair.interrupted = True
        return removed

    def plan_repair(self, target_mU: int, whole_goods: frozenset[str]) -> Repair:
        if self.repair is not None or not self.health_mU < target_mU <= 1000:
            raise RuleViolation("ERR_SCHEMA", "invalid repair target")
        fraction = Q(target_mU - self.health_mU, 1000)
        material = {}
        for good, original in self.original_nmU.items():
            quantum = 1000 if good in whole_goods else 1  # warehouse mU
            amount = ceil(fraction * original / (1000 * quantum)) * quantum
            if self.book_nmU.get(good, Q(0)) + amount * 1000 > original:
                raise RuleViolation("ERR_REPAIR_QUANTUM", good)
            material[good] = amount
        return Repair(self.health_mU, target_mU, max(1, ceil(20 * fraction)) * 10,
                      material, total_labor_mU=ceil(2000 * fraction),
                      total_electricity_mU=ceil(2000 * fraction))


@dataclass
class Repair:
    start_health_mU: int
    target_health_mU: int
    duration_ticks: int
    materials_mU: dict[str, int]
    total_labor_mU: int
    total_electricity_mU: int
    elapsed: int = 0
    interrupted: bool = False

    def due(self) -> tuple[dict[str, int], int, int]:
        """Cumulative integer differences preserve exact totals over the job."""
        def delta(total):
            return total * (self.elapsed + 1) // self.duration_ticks - total * self.elapsed // self.duration_ticks
        return ({g: delta(q) for g, q in self.materials_mU.items()},
                delta(self.total_labor_mU), delta(self.total_electricity_mU))

    def advance_paid(self, asset: Asset) -> None:
        """Only after the caller atomically consumes this tick's reserved inputs."""
        if self.interrupted or self.elapsed >= self.duration_ticks or asset.repair is not self:
            raise InvariantViolation("repair advanced outside its live reservation")
        materials, _, _ = self.due()
        for g, q in materials.items():
            asset.book_nmU[g] = asset.book_nmU.get(g, Q(0)) + q * 1000
        self.elapsed += 1
        asset.health_mU = self.start_health_mU + (self.target_health_mU - self.start_health_mU) * self.elapsed // self.duration_ticks
        asset.version += 1
        if self.elapsed == self.duration_ticks:
            asset.repair = None

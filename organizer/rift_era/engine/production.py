"""P6 two-pass allocation and per-line nanounit input/output credits.

Public warehouses use mU. Material handed to a line is accounted separately in
nmU; audit() proves handed = consumed + remaining credit, including tiny runs.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction

from .catalog import Catalog
from .errors import InvariantViolation
from .ledger import Account, Inventory
from .materials import Origin, split_origins
from .provenance import Use
from .numeric import Q, ceil, max_feasible, proportional, units


@dataclass(frozen=True)
class Recipe:
    id: str
    inputs: Mapping[str, int]  # mU per full cycle batch
    outputs: Mapping[str, int]
    whole_goods: frozenset[str]
    flow_goods: frozenset[str]

    @classmethod
    def from_catalog(cls, catalog: Catalog, recipe_id: str) -> Recipe:
        row = catalog.recipes[recipe_id]
        return cls(recipe_id, {g: units(q) for g, q in row["inputs"].items()},
                   {g: units(q) for g, q in row["outputs"].items()},
                   frozenset(g for g, d in catalog.goods.items() if d.get("indivisible")),
                   frozenset(g for g, d in catalog.goods.items() if d["kind"] != "inventory"))


@dataclass
class Line:
    id: str
    owner: str
    hub: str
    recipe: Recipe
    credit_nmU: dict[str, int] = field(default_factory=dict)
    output_nmU: dict[str, int] = field(default_factory=dict)
    fed_nmU: dict[str, int] = field(default_factory=dict)
    used_nmU: dict[str, int] = field(default_factory=dict)
    generated_nmU: dict[str, int] = field(default_factory=dict)
    released_nmU: dict[str, int] = field(default_factory=dict)

    def required(self, batch_mU: int) -> dict[str, int]:
        return {g: max(0, ceil(Q(a * batch_mU - self.credit_nmU.get(g, 0), 1000)))
                for g, a in self.recipe.inputs.items()}

    def releases(self, batch_mU: int) -> dict[str, int]:
        result = {}
        for g, rate in self.recipe.outputs.items():
            quantum = 1_000_000 if g in self.recipe.whole_goods else 1_000
            accumulated = self.output_nmU.get(g, 0) + rate * batch_mU
            result[g] = (accumulated // quantum) * (quantum // 1000)
        return result

    def audit(self) -> None:
        if any(set(getattr(self, name)) - self.recipe.inputs.keys() for name in ('credit_nmU', 'fed_nmU', 'used_nmU')):
            raise InvariantViolation('orphan input progress after recipe change')
        if any(set(getattr(self, name)) - self.recipe.outputs.keys() for name in ('output_nmU', 'generated_nmU', 'released_nmU')):
            raise InvariantViolation('orphan output progress after recipe change')
        for g in self.recipe.inputs:
            if self.fed_nmU.get(g, 0) != self.used_nmU.get(g, 0) + self.credit_nmU.get(g, 0):
                raise InvariantViolation(f"line input conservation: {self.id}/{g}")
            if not 0 <= self.credit_nmU.get(g, 0) < 1000:
                raise InvariantViolation("invalid line credit remainder")
        for g in self.recipe.outputs:
            if self.generated_nmU.get(g, 0) != self.released_nmU.get(g, 0) + self.output_nmU.get(g, 0):
                raise InvariantViolation(f"line output conservation: {self.id}/{g}")

    def discard_and_switch(self, recipe: Recipe) -> dict[str, int]:
        """Return exact discarded physical nmU for loss accounting, then reset."""
        waste = defaultdict(int, self.credit_nmU)
        for g, q in self.output_nmU.items():
            waste[g] += q
        self.recipe = recipe
        for name in ("credit_nmU", "output_nmU", "fed_nmU", "used_nmU", "generated_nmU", "released_nmU"):
            getattr(self, name).clear()
        return dict(waste)


@dataclass(frozen=True)
class ProductionResult:
    line_id: str
    batch_mU: int
    handed_mU: Mapping[str, int]
    consumed_nmU: Mapping[str, int]
    released_mU: Mapping[str, int]


class Production:
    def __init__(self, seed: int, inventory: Inventory, weights: Mapping[str, Fraction] | None = None, *, economic_owner=None, output_titles=None):
        self.seed, self.inventory = seed, inventory
        self.cargo_weights = weights or {}
        self.economic_owner = economic_owner or (lambda line:None)
        self.output_titles = output_titles or (lambda line,pieces:pieces)

    def _weight(self, good: str) -> Fraction:
        return self.cargo_weights.get(good, Q(1))

    @staticmethod
    def credit_account(line: Line, good: str) -> Account:
        return Account(line.owner, line.hub, f'input_credit:{line.id}', good)

    @staticmethod
    def wip_account(line: Line, good: str) -> Account:
        return Account(line.owner, line.hub, f'work_in_progress:{line.id}', good)

    @classmethod
    def virtual_stock(cls, lines) -> dict[Account, Fraction]:
        result = {}
        for line in lines:
            result.update({cls.credit_account(line, good): Q(q, 1000) for good, q in line.credit_nmU.items() if q})
            result.update({cls.wip_account(line, good): Q(q, 1000) for good, q in line.output_nmU.items()
                           if q and good not in line.recipe.flow_goods})
        return result

    def _materials(self, results, lines_by_id, source):
        if self.inventory.materials is None:
            return None
        draft = self.inventory.materials.draft()
        for result in results:
            line = lines_by_id[result.line_id]
            recycling = line.recipe.id in {'P066', 'P067', 'P068', 'P069'}
            use = (Use.EXTRACTION if line.recipe.id.startswith('extract:') else Use.RECYCLING if recycling
                   else Use.RESEARCH if 'research_points' in line.recipe.outputs or line.recipe.id.startswith('validate:') else Use.INDUSTRY)
            scrap = defaultdict(Fraction)
            for good, quantity in result.handed_mU.items():
                pieces = draft.take(source(line, good), Q(quantity))
                if any(origin.location != line.hub for origin in pieces):
                    raise InvariantViolation('production input has not physically reached the line hub')
                draft.add(self.credit_account(line, good), pieces, virtual=True)
            for good, quantity in result.consumed_nmU.items():
                pieces = draft.consume(self.credit_account(line, good), Q(quantity, 1000),
                                       use=use, reason=f'production:{line.recipe.id}', hub=line.hub)
                if good.startswith('scrap_'):
                    for origin, amount in pieces.items():
                        scrap[origin] += amount
            for good, rate in line.recipe.outputs.items():
                if good in line.recipe.flow_goods:
                    continue
                generated = Q(rate * result.batch_mU, 1000)
                if recycling and generated:
                    total = sum(scrap.values(), Q(0))
                    if not total:
                        raise InvariantViolation('recycling output without real scrap input')
                    from dataclasses import replace
                    pieces = defaultdict(Fraction)
                    allocation = split_origins(generated, dict(scrap), capped=False,
                        key=(self.seed, 'recycled_origin', self.inventory.materials.tick, line.id, good))
                    for origin, amount in allocation.items():
                        pieces[replace(origin, recognized=True,economic_owner=self.economic_owner(line))] += amount
                else:
                    pieces = {Origin(line.owner, line.hub, synthetic_food=(line.recipe.id == 'P090' and good == 'food'),
                                     economic_owner=self.economic_owner(line)): generated}
                wip = self.wip_account(line, good)
                pieces = self.output_titles(line,pieces)
                draft.create(wip, pieces, virtual=True)
                released = draft.take(wip, Q(result.released_mU[good]))
                draft.add(Account(line.owner, line.hub, 'next_available', good), released)
        return draft.finish()

    def discard_and_switch(self, line: Line, recipe: Recipe) -> dict[str, int]:
        """Dispose of real input/WIP once, keeping origin and destruction debt.

        The industry command layer must separately validate facility, technology,
        installed equipment, permissions and the cost of the requested switch.
        """
        line.audit()
        if self.inventory.materials:
            draft = self.inventory.materials.draft()
            for good, amount in line.credit_nmU.items():
                draft.consume(self.credit_account(line, good), Q(amount, 1000),
                              use=Use.DESTRUCTION, reason='line_switch_input_credit')
            for good, amount in line.output_nmU.items():
                if good not in line.recipe.flow_goods:
                    draft.consume(self.wip_account(line, good), Q(amount, 1000),
                                  use=Use.DESTRUCTION, reason='line_switch_work_in_progress')
            self.inventory.transform({}, {}, reason='line_switch_discard', material_plan=draft.finish())
        flow_goods = line.recipe.flow_goods
        return {g: q for g, q in line.discard_and_switch(recipe).items() if g not in flow_goods}

    def advance(self, tick: int, lines: list[Line], max_batches: Mapping[str, int], *,
                free_capacity_mU: Mapping[str, int],
                input_buckets: Mapping[str, str] | None = None,
                input_sources: Mapping[str, tuple[str, str, str]] | None = None) -> list[ProductionResult]:
        """Call once for all competing lines, AFTER P3 labor/power clipping.

        No second pass reuses quotas stranded by another input constraint.
        Ownership partitions materials; all owners compete for public warehouse.
        """
        if len({line.id for line in lines}) != len(lines):
            raise InvariantViolation("duplicate line")
        lines_by_id = {line.id: line for line in lines}
        buckets = input_buckets or {}
        sources = input_sources or {}
        def source(line, good):
            owner, location, bucket = sources.get(line.id, (line.owner, line.hub, buckets.get(line.id, 'available')))
            return Account(owner, location, bucket, good)
        requests: dict[Account, dict[str, int]] = defaultdict(dict)
        for line in lines:
            high = max_batches[line.id]
            if type(high) is not int or high < 0:
                raise ValueError("negative or fractional batch limit")
            for good, qty in line.required(high).items():
                requests[source(line, good)][line.id] = qty
        quotas: dict[str, dict[str, int]] = defaultdict(dict)
        for account, demand in requests.items():
            shares = proportional(self.inventory.balance(account), demand,
                                  key=(self.seed, "pool_tie", tick, "P6", account.owner, account.location, account.asset))
            for line_id, qty in shares.items():
                quotas[line_id][account.asset] = qty
        feasible = {}
        for line in lines:
            feasible[line.id] = max_feasible(max_batches[line.id], lambda x, l=line:
                all(q <= quotas[l.id][g] for g, q in l.required(x).items()))

        def released_weight(line: Line, batch: int) -> int:
            return ceil(sum((q * self._weight(g) for g, q in line.releases(batch).items()
                             if g not in line.recipe.flow_goods), Q(0)))

        space_requests: dict[str, dict[str, int]] = defaultdict(dict)
        for line in lines:
            space_requests[line.hub][line.id] = released_weight(line, feasible[line.id])
        for hub, demand in space_requests.items():
            share = proportional(free_capacity_mU[hub], demand,
                                 key=(self.seed, "pool_tie", tick, "P6", hub, "warehouse"))
            for line_id, allotted in share.items():
                line = lines_by_id[line_id]
                feasible[line_id] = max_feasible(feasible[line_id], lambda x, l=line, a=allotted:
                                                 released_weight(l, x) <= a)

        debits, credits = defaultdict(int), defaultdict(int)
        results = []
        for line in sorted(lines, key=lambda l: l.id):
            batch = feasible[line.id]
            handed, released = line.required(batch), line.releases(batch)
            consumed = {g: a * batch for g, a in line.recipe.inputs.items()}
            for g, q in handed.items():
                debits[source(line, g)] += q
            for g, q in released.items():
                if g not in line.recipe.flow_goods:
                    credits[Account(line.owner, line.hub, "next_available", g)] += q
            results.append(ProductionResult(line.id, batch, handed, consumed, released))
        # All inventory validation precedes BOTH warehouse and line-state writes.
        self.inventory.transform(debits, credits, reason="production_feeding_and_release",
                                 material_plan=self._materials(results, lines_by_id, source))
        for result in results:
            line = lines_by_id[result.line_id]
            for g, used in result.consumed_nmU.items():
                fed = result.handed_mU[g] * 1000
                line.credit_nmU[g] = line.credit_nmU.get(g, 0) + fed - used
                line.fed_nmU[g] = line.fed_nmU.get(g, 0) + fed
                line.used_nmU[g] = line.used_nmU.get(g, 0) + used
            for g, rate in line.recipe.outputs.items():
                generated = rate * result.batch_mU
                released = result.released_mU[g] * 1000
                line.output_nmU[g] = line.output_nmU.get(g, 0) + generated - released
                line.generated_nmU[g] = line.generated_nmU.get(g, 0) + generated
                line.released_nmU[g] = line.released_nmU.get(g, 0) + released
            line.audit()
        return results


def commit_buffers(inventory: Inventory) -> None:
    from .ledger import Transfer
    inventory.transfer_many(Transfer(a, Account(a.owner, a.location, "available", a.asset), q)
                            for a, q in inventory.balances().items() if a.bucket == "next_available")


def allocate_labor(seed: int, tick: int, hub: str, supply: Mapping[str, int],
                   demands: Mapping[str, Mapping[str, int]]) -> dict[str, dict[str, int]]:
    """Dedicated skills first, remaining high skills substitute downwards.

    Input/output units are mU-person-ticks, not stored person-time commodities.
    """
    order = ("engineer", "skilled_worker", "general_worker")
    remaining = {skill: supply.get(skill, 0) for skill in order}
    result = {entity: {skill: 0 for skill in order} for entity in demands}
    for index, skill in enumerate(order):
        request = {e: d.get(skill, 0) for e, d in demands.items()}
        available = sum(remaining[s] for s in order[:index + 1])
        allocated = proportional(available, request, key=(seed, "labor", tick, hub, skill))
        used = sum(allocated.values())
        # Consume the least-specialized eligible skill before its substitutes.
        for source in reversed(order[:index + 1]):
            take = min(remaining[source], used)
            remaining[source] -= take
            used -= take
        for entity, amount in allocated.items():
            result[entity][skill] = amount
    return result


def labor_sources(seed: int, tick: int, hub: str, supply: Mapping[str, int],
                  demands: Mapping[str, Mapping[str, int]]) -> dict[str, dict[str, int]]:
    """Actual worker skill used by each employer, for wages and non-duplication."""
    quotas = allocate_labor(seed, tick, hub, supply, demands)
    order = ('engineer', 'skilled_worker', 'general_worker')
    remaining = {skill: supply.get(skill, 0) for skill in order}
    result = {entity: {skill: 0 for skill in order} for entity in demands}
    for index, skill in enumerate(order):
        unfilled = {entity: quota[skill] for entity, quota in quotas.items()}
        for source in reversed(order[:index + 1]):
            part = proportional(remaining[source], unfilled,
                                key=(seed, 'labor_source', tick, hub, skill, source))
            remaining[source] -= sum(part.values())
            for entity, quantity in part.items():
                result[entity][source] += quantity
                unfilled[entity] -= quantity
    return result

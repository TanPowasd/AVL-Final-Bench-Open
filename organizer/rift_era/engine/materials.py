"""Conserved material provenance for warehouse, input credit and work in progress.

Identical origin/path attributes coalesce; there is no batch ID per production
tick. Mixed-origin draws use nmU proportional floors and hash-order tails.
Exact sub-nmU fuel residues remain physical credit, never newly created mass.
Repeated mixing cannot multiply denominators as exact proportional ratios do.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import TYPE_CHECKING
from collections.abc import Mapping
from types import MappingProxyType

from .determinism import canonical, digest, fingerprint
from .errors import InvariantViolation
from .numeric import Q, floor
from .provenance import Leg, Use, erase_loops
from .economic_ownership import EconomicOwner

if TYPE_CHECKING:
    from .ledger import Account, Transfer


@dataclass(frozen=True)
class Origin:
    nation: str
    hub: str
    path: tuple[Leg, ...] = ()
    recognized: bool = False
    synthetic_food: bool = False
    economic_owner: EconomicOwner | None = None

    @property
    def location(self) -> str:
        return self.path[-1].destination if self.path else self.hub

    def travel(self, legs: tuple[Leg, ...]) -> Origin:
        if not legs:
            return self
        if legs[0].origin != self.location:
            raise InvariantViolation('material path does not start at actual position')
        return replace(self, path=erase_loops(self.path + legs))


def split_origins(quantity: Fraction, weights: dict[Origin, Fraction], *,
                  key: tuple[str | int, ...], capped: bool) -> dict[Origin, Fraction]:
    """Integer nmU allocation, with exact physical dust rather than ratio dust.

    A capped draw never removes more of an origin than exists. Any sub-nmU
    fraction in the result comes from actual physical quantities/caps, not from
    storing a new quotient of arbitrary mixed-stock totals.
    """
    weights = {origin: q for origin, q in weights.items() if q}
    total = sum(weights.values(), Q(0))
    if quantity < 0 or any(q < 0 for q in weights.values()) or (capped and quantity > total) or (quantity and not total):
        raise InvariantViolation('invalid origin allocation')
    if not quantity:
        return {}
    if capped and quantity == total:
        return dict(weights)
    if len(weights) == 1:
        return {next(iter(weights)): quantity}
    assigned = {o: Q(floor(quantity * q * 1000 / total), 1000) for o, q in weights.items()}
    remaining = quantity - sum(assigned.values(), Q(0))
    if not remaining:
        return {o: q for o, q in assigned.items() if q}
    for origin in sorted(weights, key=lambda o: digest(*key, fingerprint(o))):
        if not remaining:
            break
        capacity = weights[origin] - assigned[origin] if capped else remaining
        take = min(remaining, Q(1, 1000), capacity)
        assigned[origin] += take
        remaining -= take
    if remaining:
        raise InvariantViolation('origin rounding tails exceed available fragments')
    return {o: q for o, q in assigned.items() if q}


@dataclass(frozen=True)
class MaterialUse:
    id: str
    tick: int
    owner: str
    hub: str
    good: str
    quantity_mU: Fraction
    origin: Origin
    use: Use
    reason: str
    origin_credit: Fraction = Q(0)  # frozen at physical debit COMMIT, not P7/query time
    commissioned: bool = False
    source_bucket: str = ''


@dataclass(frozen=True)
class MaterialDelivery:
    source: Account
    destination: Account
    origin: Origin  # title immediately before ownership transfer
    quantity_mU: Fraction


class MaterialBook:
    def __init__(self, balances: dict[Account, int], *, seed: int = 0,
                 credits: dict[str, Fraction] | None = None):
        self.seed = seed
        self.credits = credits if credits is not None else {}
        self._pools: dict[Account, dict[Origin, Fraction]] = {}
        self._virtual: set[Account] = set()
        self._sources: dict[str, Fraction] = defaultdict(Fraction)
        self._sinks: dict[str, Fraction] = defaultdict(Fraction)
        self.tick = -1
        self.events: list[MaterialUse] = []
        self._revision = 0
        for account, quantity in balances.items():
            if quantity:
                self._pools[account] = {Origin(account.owner, account.location): Q(quantity)}
                self._sources[account.asset] += quantity

    def begin_tick(self, tick: int) -> None:
        if type(tick) is not int or tick <= self.tick:
            raise InvariantViolation('backdated or repeated material tick')
        self.tick = tick
        self.events.clear()
        self._revision += 1

    def quantity(self, account: Account) -> Fraction:
        return sum(self._pools.get(account, {}).values(), Q(0))

    def composition(self, account: Account) -> dict[Origin, Fraction]:
        return dict(self._pools.get(account, {}))

    def physical_by_owner(self, owner: str) -> dict[str, Fraction]:
        totals = defaultdict(Fraction)
        for account, composition in self._pools.items():
            if account.owner == owner and not account.bucket.startswith('capital:'):
                totals[account.asset] += sum(composition.values(), Q(0))
        return dict(totals)

    def economic_positions(self,owner):
        rows=[]
        for account,pool in sorted(self._pools.items()):
            if account.owner!=owner or account.bucket.startswith(('capital:','asset_wip:')):continue
            titles=defaultdict(Fraction)
            for origin,q in pool.items():titles[origin.economic_owner or EconomicOwner(owner,'treasury')]+=q
            for title,q in sorted(titles.items()):
                rows.append([account.location,account.bucket,account.asset,title.nation,title.account,floor(q*1000)])
        return rows

    def food_shares(self, owner: str) -> list[list[str | int]]:
        """Own food composition for observations; public precision is 0.001."""
        rows = []
        for account, pool in sorted(self._pools.items()):
            if account.owner == owner and account.asset == 'food':
                quantity = sum(pool.values(), Q(0))
                synthetic = sum((q for o, q in pool.items() if o.synthetic_food), Q(0))
                rows.append([account.location, account.bucket, floor(1000 * synthetic / quantity)])
        return rows

    def draft(self) -> MaterialDraft:
        return MaterialDraft(self)

    def transfers(self, transfers: tuple[Transfer, ...]) -> MaterialPlan:
        draft = self.draft()
        # Every source is frozen before any destination receives material.
        # Disjoint named origins are reserved first; unrestricted transfers use
        # only the actual remainder, independently of caller iteration order.
        demanded = defaultdict(Fraction)
        for transfer in transfers:
            if transfer.source != transfer.destination:
                demanded[transfer.source, transfer.origin_nation] += transfer.quantity
        drawn = {}
        for (account, nation), quantity in sorted(demanded.items(),
                key=lambda item: (item[0][0], item[0][1] is None, item[0][1] or '')):
            if quantity:
                drawn[account, nation] = draft.take(account, quantity, nation=nation)

        def delivery_key(transfer):
            values = dict(vars(transfer))
            if values['origin_nation'] is None:
                del values['origin_nation']  # Preserve existing unrestricted random keys.
            if not values['retitle']:
                del values['retitle']
            return fingerprint(values)

        keys = {id(t): delivery_key(t) for t in transfers}
        for transfer in sorted(transfers, key=lambda t: digest(self.seed, 'origin_delivery', self.tick, keys[id(t)])):
            if transfer.source == transfer.destination or not transfer.quantity:
                continue
            source = transfer.source, transfer.origin_nation
            selected = split_origins(Q(transfer.quantity), drawn[source], capped=True,
                key=(self.seed, 'origin_delivery_parts', self.tick, keys[id(transfer)]))
            pieces = defaultdict(Fraction)
            for origin, q in selected.items():
                pieces[origin.travel(transfer.legs)] += q
                drawn[source][origin] -= q
            destination = transfer.destination_hub or transfer.destination.location
            if any(origin.location != destination for origin in pieces):
                raise InvariantViolation('cross-hub material transfer requires actual transport legs')
            draft.deliver(transfer.source,transfer.destination,pieces,
                          retitle=transfer.retitle or transfer.source.owner!=transfer.destination.owner)
        return draft.finish()

    def transform(self, consumed, produced, *, reason: str, use: Use,
                  origins: dict[Account, Origin] | None = None) -> MaterialPlan:
        draft = self.draft()
        for account, quantity in sorted(consumed.items()):
            draft.consume(account, Q(quantity), use=use, reason=reason)
        for account, quantity in sorted(produced.items()):
            origin = (origins or {}).get(account, Origin(account.owner, account.location))
            draft.create(account, {origin: Q(quantity)})
        return draft.finish()

    def travel(self, accounts: tuple[Account, ...], leg: Leg) -> MaterialPlan:
        draft = self.draft()
        for account in accounts:
            composition = draft.take(account, draft.quantity(account))
            shifted = defaultdict(Fraction)
            for origin, quantity in composition.items():
                shifted[origin.travel((leg,))] += quantity
            draft.add(account, shifted)
        return draft.finish()

    def audit(self, warehouse: dict[Account, int]) -> None:
        totals = defaultdict(Fraction)
        for account in self._pools.keys() | warehouse.keys():
            composition = self._pools.get(account, {})
            if any(q <= 0 for q in composition.values()):
                raise InvariantViolation('nonpositive material fragment')
            if any(o.economic_owner is not None and not isinstance(o.economic_owner,EconomicOwner) for o in composition):
                raise InvariantViolation('invalid inventory economic title')
            quantity = sum(composition.values(), Q(0))
            if account not in self._virtual and quantity != warehouse.get(account, 0):
                raise InvariantViolation('material projection differs from warehouse')
            if account in self._virtual and account in warehouse:
                raise InvariantViolation('virtual input/WIP also counted in warehouse')
            if len({o.location for o in composition}) > 1:
                raise InvariantViolation('one material container has multiple physical positions')
            totals[account.asset] += quantity
        for good in totals.keys() | self._sources.keys() | self._sinks.keys():
            if totals.get(good, 0) != self._sources.get(good, 0) - self._sinks.get(good, 0):
                raise InvariantViolation('material source/sink conservation failed')

    def audit_virtual(self, expected: Mapping[Account, Fraction]) -> None:
        actual = {a: self.quantity(a) for a in self._virtual}
        if actual != {a: q for a, q in expected.items() if q}:
            raise InvariantViolation('material credit/WIP differs from live engine entities')

    def snapshot(self) -> dict:
        return {'tick': self.tick, 'seed': self.seed, 'credits': dict(self.credits),
                'pools': [[a.owner, a.location, a.bucket, a.asset,
                    [[o, q] for o, q in sorted(p.items(), key=lambda row: canonical(row[0]))]]
                    for a, p in sorted(self._pools.items())],
                'virtual': [[a.owner, a.location, a.bucket, a.asset] for a in sorted(self._virtual)],
                'sources': dict(self._sources), 'sinks': dict(self._sinks), 'events': self.events}


class MaterialDraft:
    """Copy-on-write overlay. Discarding it has no effect on the live book."""
    def __init__(self, book: MaterialBook):
        self.book, self.revision = book, book._revision
        self.pools: dict[Account, dict[Origin, Fraction]] = {}
        self.virtual: set[Account] = set()
        self.sources = defaultdict(Fraction)
        self.sinks = defaultdict(Fraction)
        self.events: list[MaterialUse] = []
        self.deliveries: list[MaterialDelivery] = []

    @classmethod
    def from_plan(cls, plan: MaterialPlan) -> MaterialDraft:
        plan.check_current()
        result = cls(plan.book)
        result.pools = {a: dict(pool) for a, pool in plan.pools.items()}
        result.virtual = set(plan.virtual)
        result.sources.update(plan.sources)
        result.sinks.update(plan.sinks)
        result.events.extend(plan.events)
        result.deliveries.extend(plan.deliveries)
        return result

    def quantity(self, account: Account) -> Fraction:
        return sum(self.pools.get(account, self.book._pools.get(account, {})).values(), Q(0))

    def take(self, account: Account, quantity: Fraction, *, nation: str | None = None) -> dict[Origin, Fraction]:
        pool = self.pools.get(account, self.book._pools.get(account, {}))
        eligible = pool if nation is None else {o:q for o,q in pool.items() if o.nation == nation}
        available = sum(eligible.values(), Q(0))
        if quantity < 0 or quantity > available:
            raise InvariantViolation('material debit exceeds physical provenance')
        if not quantity:
            return {}
        drawn = split_origins(quantity, eligible, capped=True,
            key=(self.book.seed, 'origin_draw', self.book.tick, account.owner, account.location, account.bucket, account.asset))
        self.pools[account] = {o: q - drawn.get(o, Q(0)) for o, q in pool.items() if q > drawn.get(o, Q(0))}
        return drawn

    def add(self, account: Account, pieces: dict[Origin, Fraction], *, virtual: bool = False) -> None:
        if virtual:
            self.virtual.add(account)
        pool = self.pools.setdefault(account, dict(self.book._pools.get(account, {})))
        for origin, quantity in pieces.items():
            if quantity < 0:
                raise InvariantViolation('negative material credit')
            if quantity:
                pool[origin] = pool.get(origin, Q(0)) + quantity

    def create(self, account: Account, pieces: dict[Origin, Fraction], *, virtual: bool = False) -> None:
        self.add(account, pieces, virtual=virtual)
        self.sources[account.asset] += sum(pieces.values(), Q(0))

    def deliver(self,source: Account,destination: Account,pieces: dict[Origin,Fraction],*,retitle=False,virtual=False,
                economic_owner: EconomicOwner | None = None) -> None:
        """Credit already drawn physical pieces, preserving their pre-sale title."""
        credited=defaultdict(Fraction)
        for origin,quantity in pieces.items():
            if quantity:
                self.deliveries.append(MaterialDelivery(source,destination,origin,quantity))
                credited[replace(origin,economic_owner=economic_owner) if retitle else origin]+=quantity
        self.add(destination,dict(credited),virtual=virtual)

    def consume(self, account: Account, quantity: Fraction, *, use: Use, reason: str,
                hub: str | None = None) -> dict[Origin, Fraction]:
        if not isinstance(use, Use) or not reason:
            raise InvariantViolation('material use must have a defined purpose and reason')
        pieces = self.take(account, quantity)
        self.sinks[account.asset] += quantity
        for origin, amount in sorted(pieces.items(), key=lambda row: canonical(row[0])):
            destination = hub or origin.location
            if origin.location != destination:
                raise InvariantViolation('material consumed away from its physical position')
            self.events.append(MaterialUse(f'{self.book.tick}/{len(self.book.events) + len(self.events)}',
                self.book.tick, account.owner, destination, account.asset, amount, origin, use, reason,
                source_bucket=account.bucket))
        return pieces

    def capitalize(self, account: Account, pieces: Mapping[Origin, Fraction], *, use: Use, reason: str) -> None:
        """Record irreversible installation, without destroying embodied mass.

        Only construction/repair may use this path; neither grants a terminal
        trade receipt. Stored capital is audited separately from inventory eta.
        """
        if use not in (Use.CONSTRUCTION, Use.REPAIR):
            raise InvariantViolation('eligible consumption cannot be capitalized')
        for origin, amount in sorted(pieces.items(), key=lambda row: canonical(row[0])):
            if amount <= 0 or origin.location != account.location:
                raise InvariantViolation('invalid capitalized material')
            self.events.append(MaterialUse(f'{self.book.tick}/{len(self.book.events) + len(self.events)}',
                self.book.tick, account.owner, account.location, account.asset, amount, origin, use, reason,
                commissioned=use == Use.CONSTRUCTION,source_bucket=account.bucket))

    def finish(self) -> MaterialPlan:
        return MaterialPlan(self.book, self.revision,
            MappingProxyType({a: MappingProxyType(dict(p)) for a, p in self.pools.items()}), frozenset(self.virtual),
            MappingProxyType(dict(self.sources)), MappingProxyType(dict(self.sinks)), tuple(self.events),tuple(self.deliveries))


@dataclass(frozen=True)
class MaterialPlan:
    book: MaterialBook
    revision: int
    pools: Mapping[Account, Mapping[Origin, Fraction]]
    virtual: frozenset[Account]
    sources: Mapping[str, Fraction]
    sinks: Mapping[str, Fraction]
    events: tuple[MaterialUse, ...]
    deliveries: tuple[MaterialDelivery, ...] = ()

    def check_current(self) -> None:
        if self.book._revision != self.revision:
            raise InvariantViolation('stale or repeated material transaction')
        if any(not 0 <= self.book.credits.get(e.origin.nation, Q(0)) <= 1 for e in self.events):
            raise InvariantViolation('invalid credit at material consumption')

    def check_projection(self, inventory, changed_balances: dict[Account, int]) -> None:
        self.check_current()
        if any(len({o.location for o in pool}) > 1 for pool in self.pools.values()):
            raise InvariantViolation('material container mixes different physical positions')
        virtual = self.book._virtual | self.virtual
        for account in self.pools.keys() | changed_balances.keys():
            if account in virtual:
                if changed_balances.get(account, inventory.balance(account)):
                    raise InvariantViolation('virtual material duplicates warehouse stock')
                continue
            quantity = sum(self.pools.get(account, self.book._pools.get(account, {})).values(), Q(0))
            if quantity != changed_balances.get(account, inventory.balance(account)):
                raise InvariantViolation('physical transaction does not match material plan')

    def commit(self) -> None:
        self.check_current()
        events = tuple(replace(e, origin_credit=self.book.credits.get(e.origin.nation, Q(0))) for e in self.events)
        for account, pool in self.pools.items():
            if pool:
                self.book._pools[account] = dict(pool)
            else:
                self.book._pools.pop(account, None)
        # Same result as `_virtual = (_virtual | virtual) & _pools`, given the
        # invariant _virtual <= _pools left by every commit: only accounts this
        # plan touched can enter or leave either set.
        live = self.book._virtual
        pools = self.book._pools
        for account in self.virtual:
            if account in pools:
                live.add(account)
        for account in self.pools:
            if account not in pools:
                live.discard(account)
        for good, quantity in self.sources.items():
            self.book._sources[good] += quantity
        for good, quantity in self.sinks.items():
            self.book._sinks[good] += quantity
        self.book.events.extend(events)
        self.book._revision += 1

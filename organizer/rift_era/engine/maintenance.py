"""Periodic, materially and financially backed asset maintenance (02/05).

P1 updates timers and reserves shared stocks/budgets; P6 consumes those locks.
Insufficient periodic servicing accumulates actual arrears. Catch-up requires
120% of those arrears and two full cycles offline, never a free debt reset.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from math import gcd, lcm
from functools import reduce
from types import MappingProxyType

from .assets import Asset
from .determinism import uniform
from .errors import InvariantViolation
from .ledger import Account, CashAccount, CashLedger, CashTransfer, DeliveryPayment, Inventory, Transfer
from .numeric import Q, ceil, max_feasible, proportional
from .payments import Obligation, Payments
from .provenance import Use


@dataclass(frozen=True)
class MaintenanceTarget:
    asset_id: str
    materials_mU: Mapping[str, int]
    payer: CashAccount
    suppliers: Mapping[str, CashAccount]


@dataclass
class MaintenanceStatus:
    next_due_tick: int
    arrears_mU: dict[str, int] = field(default_factory=dict)
    fault_until_tick: int = 0
    service_until_tick: int = 0
    covered_debt: Fraction = Q(0)
    covered_arrears_mU: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class MaintenanceRequest:
    asset_id: str
    recovery: bool
    required_mU: Mapping[str, int]
    selected_mU: Mapping[str, int]
    prices: Mapping[str, Fraction]
    coverage: Fraction
    cost_mU: int
    payer: CashAccount
    suppliers: Mapping[str, CashAccount]


@dataclass(frozen=True)
class MaintenanceReceipt:
    asset_id: str
    tick: int
    recovery: bool
    consumed_mU: Mapping[str, int]
    paid_mU: int
    coverage: Fraction
    suppliers_paid: tuple[tuple[CashAccount, int], ...] = ()
    payer: CashAccount | None = None


class Maintenance:
    PERIOD_CYCLES = 20

    def __init__(self, inventory: Inventory, cash: CashLedger, payments: Payments,
                 assets: Mapping[str, Asset], *, seed: int, cycle_ticks: int = 10, title_payments=False):
        self.inventory, self.cash, self.payments, self.assets = inventory, cash, payments, assets
        self.title_payments=title_payments
        self.seed, self.cycle_ticks = seed, cycle_ticks
        self.targets: dict[str, MaintenanceTarget] = {}
        self.status: dict[str, MaintenanceStatus] = {}
        self.reservations: dict[str, MaintenanceRequest] = {}
        self.tick = -1
        self.reserved_tick = -1
        self.settled_tick = -1
        self.last_receipts: tuple[MaintenanceReceipt, ...] = ()
        self.cash_reserved: dict[CashAccount, int] = {}
        self.cash_sources: dict[CashAccount, CashAccount] = {}

    def register(self, asset_id: str, materials_mU: Mapping[str, int], payer: CashAccount,
                 suppliers: Mapping[str, CashAccount], *, commissioned_tick: int = 0) -> None:
        if (asset_id in self.targets or asset_id not in self.assets or not materials_mU
                or any(type(q) is not int or q <= 0 for q in materials_mU.values())
                or set(materials_mU) != set(suppliers)
                or any(account.currency != payer.currency for account in suppliers.values())):
            raise InvariantViolation('invalid maintenance registration')
        self.targets[asset_id] = MaintenanceTarget(asset_id, MappingProxyType(dict(materials_mU)), payer,
                                                    MappingProxyType(dict(suppliers)))
        self.status[asset_id] = MaintenanceStatus(commissioned_tick + self.PERIOD_CYCLES * self.cycle_ticks - 1)

    def begin_tick(self, tick: int) -> None:
        if tick <= self.tick or self.reservations or self.cash_reserved:
            raise InvariantViolation('maintenance tick repeated or previous custody unsettled')
        self.tick = tick
        self.last_receipts = ()
        for aid, status in self.status.items():
            asset = self.assets[aid]
            previous = (asset.maintenance_debt, asset.maintenance_offline)
            if status.service_until_tick and tick >= status.service_until_tick:
                # A new periodic bill may have fallen due during the overhaul.
                # Only the arrears actually paid at its start are cleared.
                asset.maintenance_debt -= status.covered_debt
                for good, amount in status.covered_arrears_mU.items():
                    status.arrears_mU[good] -= amount
                status.arrears_mU = {g: q for g, q in status.arrears_mU.items() if q}
                status.service_until_tick = 0
                status.covered_debt = Q(0)
                status.covered_arrears_mU.clear()
            if (tick % self.cycle_ticks == 0 and asset.health_mU > 0 and asset.maintenance_debt >= 2
                    and tick >= max(status.fault_until_tick, status.service_until_tick)):
                probability = min(Q(1), Q(8, 100) * asset.maintenance_debt)
                if uniform(self.seed, 'maintenance_failure', tick // self.cycle_ticks, aid) < probability:
                    status.fault_until_tick = tick + 4 * self.cycle_ticks
            asset.maintenance_offline = tick < max(status.fault_until_tick, status.service_until_tick)
            if previous != (asset.maintenance_debt, asset.maintenance_offline):
                asset.version += 1

    def stock_lock(self, aid: str, good: str) -> Account:
        asset = self.assets[aid]
        return Account(asset.owner, asset.hub, f'locked_maintenance:{aid}', good)

    def cash_lock(self, aid: str) -> CashAccount:
        payer = self.reservations[aid].payer if aid in self.reservations else self.targets[aid].payer
        return self.pool(payer)

    @staticmethod
    def pool(payer: CashAccount) -> CashAccount:
        return CashAccount(payer.owner, f'maintenance_pool:{payer.name}', payer.currency)

    def _cost(self, aid: str, quantities: Mapping[str, int], prices: Mapping[str, Fraction]) -> int:
        return ceil(sum((q * prices[g] for g, q in quantities.items()), Q(0)))

    @staticmethod
    def _partial(required: Mapping[str, int], quotas: Mapping[str, int]) -> tuple[dict[str, int], Fraction]:
        # A common integer quantum services all components in the same ratio;
        # scarce AI cores cannot consume unrelated spare parts for no service.
        quanta = reduce(gcd, required.values())
        unit = {g: q // quanta for g, q in required.items()}
        count = min(quanta, *(quotas.get(g, 0) // q for g, q in unit.items()))
        return {g: q * count for g, q in unit.items()}, Q(count, quanta)

    def requests(self, price: Callable[[str, str], Fraction]) -> tuple[MaintenanceRequest, ...]:
        if self.tick < 0 or self.reserved_tick == self.tick:
            raise InvariantViolation('maintenance request outside its P1 preflight')
        requested, prices, recoveries = {}, {}, {}
        demands = defaultdict(dict)
        for aid, target in sorted(self.targets.items()):
            asset, status = self.assets[aid], self.status[aid]
            if status.next_due_tick < self.tick:
                raise InvariantViolation('periodic maintenance settlement was skipped')
            due = status.next_due_tick == self.tick
            recovery = bool(status.arrears_mU) and not status.service_until_tick and not due
            if not (due or recovery):
                continue
            requested[aid] = ({g: ceil(Q(6, 5) * q) for g, q in status.arrears_mU.items()}
                              if recovery else dict(target.materials_mU))
            prices[aid] = {g: price(g, asset.hub) for g in requested[aid]}
            recoveries[aid] = recovery
            for good, quantity in requested[aid].items():
                demands[Account(asset.owner, asset.hub, 'available', good)][aid] = quantity if asset.health_mU else 0
        quotas = defaultdict(dict)
        for account, demand in demands.items():
            shares = proportional(self.inventory.balance(account), demand,
                key=(self.seed, 'maintenance_material', self.tick, account.owner, account.location, account.asset))
            for aid, quantity in shares.items():
                quotas[aid][account.asset] = quantity
        result = []
        for aid, required in requested.items():
            selected, coverage = self._partial(required, quotas[aid])
            if recoveries[aid] and coverage != 1:
                selected, coverage = {g: 0 for g in required}, Q(0)
            result.append(MaintenanceRequest(aid, recoveries[aid], MappingProxyType(required), MappingProxyType(selected),
                MappingProxyType(prices[aid]), coverage, self._cost(aid, selected, prices[aid]),
                self.targets[aid].payer, self.targets[aid].suppliers))
        return tuple(result)

    def funding_requests(self, requests: tuple[MaintenanceRequest, ...]) -> dict[CashAccount, int]:
        required = defaultdict(Fraction)
        for request in requests:
            required[request.payer] += sum((q * request.prices[g] for g, q in request.selected_mU.items()), Q(0))
        return {payer: ceil(amount + self.payments.unpaid_fraction(self.pool(payer))) for payer, amount in required.items()}

    def reserve(self, requests: tuple[MaintenanceRequest, ...], *, spending_limits: Mapping[CashAccount, int] | None = None) -> None:
        if self.reserved_tick == self.tick or len({r.asset_id for r in requests}) != len(requests):
            raise InvariantViolation('duplicate maintenance reservation')
        demands = defaultdict(dict)
        for request in requests:
            demands[request.payer][request.asset_id] = sum((q * request.prices[g] for g, q in request.selected_mU.items()), Q(0))
        cash_grants = {}
        for account, demand in demands.items():
            pending = self.payments.unpaid_fraction(self.pool(account))
            limit = (spending_limits or {}).get(account, self.cash.balance(account).native_mU)
            available = max(Q(0), min(self.cash.balance(account).native_mU, limit) - pending)
            scale = lcm(available.denominator, *(q.denominator for q in demand.values()))
            amounts = proportional(int(available * scale), {aid: int(q * scale) for aid, q in demand.items()},
                key=(self.seed, 'maintenance_cash', self.tick, account.owner, account.name, account.currency))
            cash_grants.update({aid: Q(q, scale) for aid, q in amounts.items()})
        goods, money, reservations = [], [], {}
        needed = defaultdict(Fraction)
        for request in requests:
            aid = request.asset_id
            def exact(quantities):
                return sum((q * request.prices[g] for g, q in quantities.items()), Q(0))
            if request.recovery:
                fraction = request.coverage if cash_grants[aid] >= exact(request.selected_mU) else Q(0)
                selected = dict(request.selected_mU) if fraction else {g: 0 for g in request.required_mU}
            else:
                quanta = reduce(gcd, request.required_mU.values())
                unit = {g: q // quanta for g, q in request.required_mU.items()}
                count = max_feasible(int(request.coverage * quanta), lambda n:
                    exact({g: n * q for g, q in unit.items()}) <= cash_grants[aid])
                selected, fraction = {g: q * count for g, q in unit.items()}, Q(count, quanta)
            asset = self.assets[aid]
            for good, quantity in selected.items():
                lock = self.stock_lock(aid, good)
                if self.inventory.balance(lock):
                    raise InvariantViolation('old maintenance material lock was not released')
                goods.append(Transfer(Account(asset.owner, asset.hub, 'available', good), lock, quantity))
            needed[request.payer] += exact(selected)
            reservations[aid] = MaintenanceRequest(aid, request.recovery, request.required_mU,
                MappingProxyType(selected), request.prices, fraction, ceil(exact(selected)), request.payer, request.suppliers)
        cash_reserved, cash_sources = {}, {}
        for payer, amount in needed.items():
            pool = self.pool(payer)
            if self.cash.balance(pool).native_mU:
                raise InvariantViolation('old maintenance pool was not released')
            reserve = min(self.cash.balance(payer).native_mU, (spending_limits or {}).get(payer, self.cash.balance(payer).native_mU),
                          ceil(amount + self.payments.unpaid_fraction(pool)))
            money.append(CashTransfer(payer, pool, reserve))
            cash_reserved[pool], cash_sources[pool] = reserve, payer
        DeliveryPayment.prepare(self.inventory, self.cash, goods, money).commit()
        self.cash_reserved, self.cash_sources = cash_reserved, cash_sources
        self.reservations = reservations
        self.reserved_tick = self.tick

    def settle(self) -> tuple[MaintenanceReceipt, ...]:
        if self.reserved_tick != self.tick or self.settled_tick == self.tick:
            raise InvariantViolation('maintenance settled without fresh P1 reservations')
        receipts = []
        for aid, request in sorted(self.reservations.items()):
            asset, status, target = self.assets[aid], self.status[aid], self.targets[aid]
            selected = dict(request.selected_mU) if asset.health_mU else {g: 0 for g in request.selected_mU}
            obligations = tuple(Obligation(self.cash_lock(aid), request.suppliers[g], q * request.prices[g], f'maintenance:{g}')
                                for g, q in selected.items() if q)
            consumption = self.inventory.prepare_transform({self.stock_lock(aid, g): q for g, q in selected.items()}, {},
                                                             reason='maintenance_catchup' if request.recovery else 'periodic_maintenance',
                                                             use=Use.MAINTENANCE)
            if self.title_payments:
                from .economic_ownership import prepare_purchase
                payment,recipients=prepare_purchase(self.payments,self.cash_lock(aid),consumption.materials.events,
                    request.prices,kind='maintenance',key=(self.seed,self.tick,'maintenance_payment',aid),buyer=request.payer)
            else:
                payment=self.payments.prepare(obligations)
                recipients=tuple(row.target for row in obligations)
            payment.commit(consumption)
            coverage = request.coverage if asset.health_mU else Q(0)
            if request.recovery and coverage == 1:
                status.covered_debt = asset.maintenance_debt
                status.covered_arrears_mU = dict(status.arrears_mU)
                status.service_until_tick = self.tick + 1 + 2 * self.cycle_ticks
                asset.maintenance_offline = True
                asset.version += 1
            elif not request.recovery:
                status.next_due_tick += self.PERIOD_CYCLES * self.cycle_ticks
                if asset.health_mU:
                    asset.maintenance_debt += 1 - coverage
                    if coverage < 1:
                        asset.version += 1
                    for good, required in request.required_mU.items():
                        status.arrears_mU[good] = status.arrears_mU.get(good, 0) + required - selected[good]
                    status.arrears_mU = {g: q for g, q in status.arrears_mU.items() if q}
            # Dead/interrupted assets refund unconsumed locks, preserving basis.
            refund_goods = [Transfer(self.stock_lock(aid, g), Account(asset.owner, asset.hub, 'available', g),
                                     self.inventory.balance(self.stock_lock(aid, g))) for g in selected]
            self.inventory.transfer_many(refund_goods)
            receipts.append(MaintenanceReceipt(aid, self.tick, request.recovery, MappingProxyType(selected),
                                               sum(payment.amounts), coverage,
                                               tuple(zip(recipients,payment.amounts)), request.payer))
        self.cash.transfer_many(CashTransfer(pool, source, self.cash.balance(pool).native_mU)
                                for pool, source in self.cash_sources.items())
        self.cash_reserved.clear()
        self.cash_sources.clear()
        self.last_receipts = tuple(receipts)
        self.reservations.clear()
        self.settled_tick = self.tick
        return self.last_receipts

    def audit(self) -> None:
        if self.targets.keys() != self.status.keys():
            raise InvariantViolation('orphan maintenance status')
        for aid, status in self.status.items():
            if self.assets[aid].maintenance_debt < 0 or any(q < 0 for q in status.arrears_mU.values()):
                raise InvariantViolation('negative maintenance arrears')
            if status.covered_debt > self.assets[aid].maintenance_debt:
                raise InvariantViolation('overhaul clears more debt than exists')
            request = self.reservations.get(aid)
            goods = self.targets[aid].materials_mU.keys() | status.arrears_mU.keys()
            if request:
                goods |= request.required_mU.keys()
            for good in goods:
                expected = request.selected_mU.get(good, 0) if request else 0
                if self.inventory.balance(self.stock_lock(aid, good)) != expected:
                    raise InvariantViolation('maintenance material custody mismatch')
        for pool, amount in self.cash_reserved.items():
            if self.cash.balance(pool).native_mU != amount:
                raise InvariantViolation('maintenance cash pool custody mismatch')

    def unregister(self, aid: str) -> None:
        if aid not in self.targets:
            return
        request = self.reservations.get(aid)
        asset, target = self.assets[aid], self.targets[aid]
        goods = [Transfer(self.stock_lock(aid, g), Account(asset.owner, asset.hub, 'available', g),
                          self.inventory.balance(self.stock_lock(aid, g))) for g in request.required_mU] if request else []
        self.inventory.transfer_many(goods)
        self.reservations.pop(aid, None)
        del self.targets[aid]
        del self.status[aid]

    def snapshot(self) -> dict:
        return {'targets': self.targets, 'status': self.status, 'reservations': self.reservations,
                'tick': self.tick, 'reserved_tick': self.reserved_tick, 'settled_tick': self.settled_tick,
                'last_receipts': self.last_receipts,
                'cash_reserved': [[a.owner, a.name, a.currency, q] for a, q in sorted(self.cash_reserved.items())],
                'cash_sources': [[a.owner, a.name, a.currency, b.owner, b.name, b.currency] for a, b in sorted(self.cash_sources.items())]}

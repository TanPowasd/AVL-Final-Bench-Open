"""Durable work orders: real custody, paid progress, cancellation and completion.

Ordinary construction uses the catalog's material schedule and quoted local
construction-service payment. Repairs additionally require their specified
labor and electricity. No worker or energy constant is invented for buildings
whose catalog specifies only materials and time.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType

from .asset_lifecycle import AssetLoss
from .errors import InvariantViolation, RuleViolation
from .ledger import Account, CashAccount, CashTransfer, DeliveryPayment, Transfer
from .materials import MaterialDraft
from .numeric import Q, ceil, proportional
from .payments import Obligation
from .protocol import PreparedCommand
from .provenance import Use


@dataclass
class WorkOrder:
    id: str
    command_id: str
    owner: str
    hub: str
    kind: str
    target: str
    duration_ticks: int
    materials_mU: dict[str, int]
    cash_mU: int = 0
    labor_mU: int = 0
    electricity_mU: int = 0
    temporary_asset: str | None = None
    payload: dict = field(default_factory=dict)
    elapsed: int = 0
    consumed_mU: dict[str, int] = field(default_factory=dict)
    cash_paid_mU: int = 0
    material_paid_mU: int = 0
    waiting: bool = False

    def due(self, whole_goods: frozenset[str]) -> tuple[dict[str, int], int, int, int]:
        def delta(total):
            return total * (self.elapsed + 1) // self.duration_ticks - total * self.elapsed // self.duration_ticks
        goods = {g: (q if self.elapsed + 1 == self.duration_ticks else 0) if g in whole_goods else delta(q)
                 for g, q in self.materials_mU.items()}
        return goods, delta(self.cash_mU), delta(self.labor_mU), delta(self.electricity_mU)


class Construction:
    def __init__(self, state):
        self.state = state
        self.jobs: dict[str, WorkOrder] = {}
        self.recent: dict[str, deque] = {p: deque(maxlen=256) for p in state.initial.known}
        self.whole = frozenset(g for g, row in state.catalog.goods.items() if row['indivisible'])
        self.on_complete = None
        self.on_cancel = None
        self.last_tick = -1

    def stock_lock(self, job: WorkOrder, good: str) -> Account:
        return Account(job.owner, job.hub, f'locked_build:{job.id}', good)

    def cash_lock(self, job: WorkOrder) -> CashAccount:
        return self.state.native(job.owner, f'build:{job.id}')

    def payer(self, job: WorkOrder) -> CashAccount:
        return self.state.native(job.owner, 'treasury')

    def _required(self, job: WorkOrder, *, initial=False):
        goods, cash, _, _ = job.due(self.whole)
        if initial:
            if job.kind in ('repair_asset', 'set_line', 'install_system', 'install_storage'):
                goods = dict(job.materials_mU)
            else:
                goods.update({g: q for g, q in job.materials_mU.items() if g in self.whole})
        return goods, cash

    def material_budget(self,job,goods):
        from .economic_ownership import income_account
        value=Q(0);inventory=self.state.initial.inventory;buyer=self.payer(job)
        for good,quantity in goods.items():
            lock=self.stock_lock(job,good)
            pools=[lock] if inventory.balance(lock)>=quantity else [lock,Account(job.owner,job.hub,'available',good)]
            origins=[origin for account in pools for origin in inventory.materials.composition(account)]
            if origins and any(income_account(o,job.owner,buyer.currency)!=buyer for o in origins):
                value+=quantity*self.state.price(good,job.hub)
        return ceil(value)

    def prepare_start(self, job: WorkOrder, register) -> PreparedCommand:
        if (job.id in self.jobs or job.duration_ticks <= 0 or job.elapsed or job.consumed_mU or job.cash_paid_mU
                or self.on_complete is None or self.on_cancel is None):
            raise InvariantViolation('invalid work order or unbound lifecycle handler')
        goods, money = self._required(job, initial=True)
        due_goods,_,_,_=job.due(self.whole)
        money+=self.material_budget(job,due_goods)
        transfers = [Transfer(Account(job.owner, job.hub, 'available', good), self.stock_lock(job, good), amount)
                     for good, amount in goods.items()]
        transaction = DeliveryPayment.prepare(self.state.initial.inventory, self.state.initial.cash, transfers,
                                              [CashTransfer(self.payer(job), self.cash_lock(job), money)])

        def commit():
            transaction.commit()
            register()
            self.jobs[job.id] = job
            self.recent[job.owner].append({'id': job.command_id, 'status': 'EXECUTING', 'target_id': job.target})

        return PreparedCommand(commit)

    def reserve(self, ctx) -> None:
        inventory, cash = self.state.initial.inventory, self.state.initial.cash
        goods, payments = defaultdict(dict), defaultdict(dict)
        for job in self.jobs.values():
            required, money = self._required(job)
            money+=self.material_budget(job,required)
            for good, amount in required.items():
                source = Account(job.owner, job.hub, 'available', good)
                goods[source][job.id] = max(0, amount - inventory.balance(self.stock_lock(job, good)))
            payments[self.payer(job)][job.id] = max(0, money - cash.balance(self.cash_lock(job)).native_mU)
        transfers, money_transfers = [], []
        for source, demand in goods.items():
            quantum = 1000 if source.asset in self.whole else 1
            grants = proportional(inventory.balance(source) // quantum,
                {jid: q // quantum for jid, q in demand.items()},
                key=(ctx.seed, 'construction_stock', ctx.tick, source.owner, source.location, source.asset))
            for jid, quantity in grants.items():
                transfers.append(Transfer(source, self.stock_lock(self.jobs[jid], source.asset), quantity * quantum))
        for source, demand in payments.items():
            grants = proportional(cash.balance(source).native_mU, demand,
                key=(ctx.seed, 'construction_cash', ctx.tick, source.owner, source.currency))
            money_transfers.extend(CashTransfer(source, self.cash_lock(self.jobs[jid]), q) for jid, q in grants.items())
        DeliveryPayment.prepare(inventory, cash, transfers, money_transfers).commit()

    def prepare_cancel(self, owner: str, command_id: str) -> PreparedCommand:
        job = next((j for j in self.jobs.values() if j.owner == owner and j.command_id == command_id), None)
        if job is None:
            raise RuleViolation('ERR_UNKNOWN_TARGET_ID')
        inventory, cash = self.state.initial.inventory, self.state.initial.cash
        refunds = tuple(Transfer(self.stock_lock(job, good), Account(job.owner, job.hub, 'available', good),
                                 inventory.balance(self.stock_lock(job, good))) for good in job.materials_mU)
        draft = MaterialDraft.from_plan(inventory.materials.transfers(refunds))
        loss = None
        if job.temporary_asset is not None:
            asset = self.state.initial.assets[job.temporary_asset]
            removed = dict(asset.book_nmU)
            for good, amount in removed.items():
                draft.consume(self.state.asset_lifecycle.account(asset, good), amount / 1000,
                              use=Use.DESTRUCTION, reason='cancelled_work_in_progress')
            value = sum((amount / 1000000 * self.state.catalog.weights[g] for g, amount in removed.items()), Q(0))
            loss = AssetLoss(f'cancel:{job.id}', inventory.materials.tick, asset.id, asset.owner, value, removed, {})
        stock = inventory.prepare_transfers(refunds, material_plan=draft.finish())
        money = cash.prepare_transfers([CashTransfer(self.cash_lock(job), self.payer(job),
                                                     cash.balance(self.cash_lock(job)).native_mU)])

        def commit():
            DeliveryPayment(stock, money).commit()
            if loss is not None:
                self.state.asset_lifecycle.events.append(loss)
            self.on_cancel(job)
            del self.jobs[job.id]
            self.recent[owner].append({'id': job.command_id, 'status': 'CANCELLED', 'target_id': job.target})

        return PreparedCommand(commit)

    def advance(self, ctx) -> None:
        if ctx.tick <= self.last_tick:
            raise InvariantViolation('construction advanced twice or backdated')
        self.last_tick = ctx.tick
        state, inventory, cash = self.state, self.state.initial.inventory, self.state.initial.cash
        completed = []
        for jid, job in sorted(tuple(self.jobs.items())):
            if job.kind == 'repair_asset' and state.initial.assets[job.target].repair.interrupted:
                self.prepare_cancel(job.owner, job.command_id).commit()
                continue
            if job.temporary_asset is not None and state.initial.assets[job.temporary_asset].health_mU < 1000:
                self.prepare_cancel(job.owner, job.command_id).commit()
                continue
            goods, money, labor, power = job.due(self.whole)
            staffed = sum(state.frame.labor.get(jid, {}).values()) >= labor
            wage_lock = state.frame.wage_locks.get(jid)
            wage_due = ceil(state.wage_cost(job.owner, state.frame.labor.get(jid, {})))
            staffed &= wage_due == 0 or wage_lock is not None and cash.balance(wage_lock).native_mU >= wage_due
            powered = state.frame.power_received.get(jid, 0) >= power
            funded = cash.balance(self.cash_lock(job)).native_mU >= money+self.material_budget(job,goods)
            stocked = all(inventory.balance(self.stock_lock(job, g)) >= q for g, q in goods.items())
            job.waiting = not (staffed and powered and funded and stocked)
            state.frame.worked[jid] = dict(state.frame.labor.get(jid, {})) if not job.waiting else {}
            if job.waiting:
                continue
            inputs = {self.stock_lock(job, g): q for g, q in goods.items() if q}
            capital_target = job.target if job.kind == 'repair_asset' else job.temporary_asset
            if capital_target:
                stock = state.asset_lifecycle.prepare_investment(capital_target, inputs, repair=job.kind == 'repair_asset')
            else:
                stock = inventory.prepare_transform(inputs, {}, reason=job.kind, use=Use.CONSTRUCTION)
            from .economic_ownership import prepare_purchase,record_income
            sources=stock.materials.deliveries if capital_target else stock.materials.events
            payment,recipients=prepare_purchase(state.payments,self.cash_lock(job),sources,
                {g:state.price(g,job.hub) for g in goods},kind='construction_material',
                key=(ctx.seed,ctx.tick,'construction_material',job.id),buyer=self.payer(job),payer_scope=self.payer(job))
            payment=payment.with_fixed_transfers((CashTransfer(self.cash_lock(job),
                state.native(job.owner,f'household:{job.hub}'),money),))
            payment.commit(stock)
            state.industry_activity.use_power(jid,power)
            record_income(state,recipients,payment.amounts)
            material_paid=sum(payment.amounts)
            job.material_paid_mU+=material_paid
            metric=state.cycles[job.owner]
            metric.costs[self.payer(job).name]=metric.costs.get(self.payer(job).name,Q(0))+material_paid
            for good, amount in goods.items():
                job.consumed_mU[good] = job.consumed_mU.get(good, 0) + amount
                if capital_target:
                    asset = state.initial.assets[capital_target]
                    asset.book_nmU[good] = asset.book_nmU.get(good, Q(0)) + amount * 1000
            job.cash_paid_mU += money
            job.elapsed += 1
            if job.kind == 'repair_asset':
                asset = state.initial.assets[job.target]
                repair = asset.repair
                repair.elapsed = job.elapsed
                if not self.whole.intersection(job.materials_mU) or job.elapsed == job.duration_ticks:
                    asset.health_mU = repair.start_health_mU + (repair.target_health_mU - repair.start_health_mU) * job.elapsed // job.duration_ticks
                asset.version += 1
            if job.elapsed == job.duration_ticks:
                completed.append(job)
        for job in completed:
            self.on_complete(job)
            state.payments.release(self.cash_lock(job), self.payer(job))
            del self.jobs[job.id]
            self.recent[job.owner].append({'id': job.command_id, 'status': 'COMPLETED', 'target_id': job.target})

    def audit(self) -> None:
        for job in self.jobs.values():
            if not 0 <= job.elapsed < job.duration_ticks:
                raise InvariantViolation('invalid work progress')
            if any(not 0 <= q <= job.materials_mU[g] for g, q in job.consumed_mU.items()):
                raise InvariantViolation('work consumed beyond its material vector')
            if not 0 <= job.cash_paid_mU <= job.cash_mU or job.material_paid_mU<0:
                raise InvariantViolation('work paid beyond its quoted service cost')
            if job.cash_paid_mU != job.cash_mU * job.elapsed // job.duration_ticks:
                raise InvariantViolation('work cash schedule differs from completed progress')
            for good, total in job.materials_mU.items():
                expected = 0 if good in self.whole else total * job.elapsed // job.duration_ticks
                if job.consumed_mU.get(good, 0) != expected:
                    raise InvariantViolation('work material schedule differs from completed progress')
                held = self.state.initial.inventory.balance(self.stock_lock(job, good))
                if held + job.consumed_mU.get(good, 0) > total:
                    raise InvariantViolation('work custody exceeds remaining requirement')
                if good in self.whole and held % 1000:
                    raise InvariantViolation('unfinished work returned a fractional whole machine')

    def visible(self, owner: str) -> dict:
        return {'jobs': [{'id': j.command_id, 'entity_id': j.id, 'kind': j.kind, 'target_id': j.target,
                          'hub_id': j.hub, 'elapsed_ticks': j.elapsed, 'duration_ticks': j.duration_ticks,
                          'waiting_resource': j.waiting} for j in self.jobs.values() if j.owner == owner],
                'receipts': list(self.recent[owner])}

    def snapshot(self) -> dict:
        return {'jobs': self.jobs, 'recent': {p: list(q) for p, q in self.recent.items()}, 'last_tick': self.last_tick}

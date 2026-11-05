"""Hub-by-hub cargo relay using the ordinary finite cargo fleets.

Every leg unloads to a physical, warehouse-counted custody account. The next
leg can split this stock among another real fleet; the incoming vehicle returns
empty on its own prepaid fuel. No vehicle or provenance is cloned at transfer.
"""
from dataclasses import dataclass
from .command_schema import fields
from .determinism import digest
from .errors import InvariantViolation, RuleViolation
from .ledger import Account, Transfer
from .numeric import Q, floor
from .protocol import PreparedCommand


@dataclass
class Relay:
    id: str
    owner: str
    command_id: str
    hubs: tuple[str, ...]
    path: tuple[str, ...]
    good: str
    quantity_mU: int
    carrier: str
    repeat_cycles: int
    next_departure_tick: int
    cancelled: bool = False
    completed: bool = False
    serial: int = 0
    propulsion: str = 'fuel'


class IntermodalOperations:
    def __init__(self, state):
        self.s = state
        self.plans = {}
        self.serial = 0

    def bucket(self, plan, stage):
        return f'locked_relay:{plan.id}:{stage}'

    def account(self, plan, stage):
        return Account(plan.owner, plan.hubs[stage], self.bucket(plan, stage), plan.good)

    def prepare(self, owner, order, ctx):
        fields(order, {'origin_hub': str, 'dest_hub': str, 'corridor_path': list, 'good': str,
                       'qty_mU': int, 'carrier_recipe': str}, {'repeat_cycles': int, 'propulsion': str})
        path, good, qty = tuple(order['corridor_path']), order['good'], order['qty_mU']
        goods = self.s.catalog.goods.get(good)
        if not path or len(path) > 128 or any(type(e) is not str for e in path) or qty <= 0 or goods is None or goods['kind'] != 'inventory':
            raise RuleViolation('ERR_SCHEMA')
        if goods['indivisible'] and qty % 1000 or not 0 <= order.get('repeat_cycles', 0) <= 100:
            raise RuleViolation('ERR_SCHEMA')
        if order.get('propulsion', 'fuel') not in ('fuel', 'electric'):
            raise RuleViolation('ERR_SCHEMA')
        node = order['origin_hub']
        if not self.s.authority.controls_hub(owner, node):
            raise RuleViolation('ERR_NO_PERMISSION')
        hubs = [node]
        for eid in path:
            edge = self.s.cargo.corridors.get(eid)
            if edge is None or not edge.health_mU or not self.s.cargo.permitted(owner, eid):
                raise RuleViolation('ERR_CORRIDOR_BLOCKED')
            node = edge.other(node); hubs.append(node)
        if node != order['dest_hub'] or len(set(hubs)) != len(hubs):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'relay path loops or destination differs')
        carrier = order['carrier_recipe']
        if carrier not in ('auto', 'intermodal', 'manual', 'P036', 'P037', 'P052', 'P053', 'river_freighter'):
            raise RuleViolation('ERR_SCHEMA')
        supported = {'manual': {'road'}, 'P036': {'road'}, 'P037': {'rail'}, 'P052': {'river', 'sea'},
                     'river_freighter': {'river'}, 'P053': {'air'}}
        if carrier in supported and any(self.s.cargo.corridors[e].mode not in supported[carrier] for e in path):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'explicit carrier cannot traverse another mode; use auto')
        if sum(p.owner == owner and not p.completed for p in self.plans.values()) >= self.s.catalog.runtime['limits']['shipments_per_nation']:
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        cid = str(int(order['id']))
        plan = Relay(f'{owner}:{cid}', owner, cid, tuple(hubs), path, good, qty, carrier,
                     order.get('repeat_cycles', 0), ctx.tick + order.get('repeat_cycles', 0) * ctx.clock.cycle_ticks)
        plan.propulsion = order.get('propulsion', 'fuel')
        stock = self.s.initial.inventory.prepare_transfers((Transfer(Account(owner, hubs[0], 'available', good), self.account(plan, 0), qty),))
        def commit():
            stock.commit()
            self.plans[plan.id] = plan
        return PreparedCommand(commit, persistent=True)

    def _carrier(self, plan, stage):
        mode = self.s.cargo.corridors[plan.path[stage]].mode
        default = {'road': ('P036', 'vehicle', 15000), 'rail': ('P037', 'locomotive', 80000),
                   'river': ('river_freighter', 'freighter', 120000), 'sea': ('P052', 'freighter', 200000),
                   'air': ('P053', 'transport_aircraft', 20000)}[mode]
        carrier, good, capacity = default
        if plan.carrier == 'manual' and mode == 'road':
            return 'manual', 800
        idle = sum(v.capacity_mU for v in self.s.cargo.vehicles.values() if v.owner == plan.owner and
                   v.good == good and v.hub == plan.hubs[stage] and self.s.cargo.available(v) and v.id not in self.s.cargo.vehicle_jobs)
        stock = self.s.initial.inventory.balance(Account(plan.owner, plan.hubs[stage], 'available', good)) // 1000
        if not stock and not idle and mode == 'road':
            return 'manual', 800
        return carrier, idle + stock * capacity

    def reserve(self, ctx):
        from .transport_operations import TransportCommands
        inv, cargo = self.s.initial.inventory, self.s.cargo
        self.plans = {pid: p for pid, p in self.plans.items() if not p.completed}
        for plan in sorted(self.plans.values(), key=lambda p: digest(ctx.seed, 'relay_reserve', ctx.tick, p.id)):
            if plan.completed or plan.cancelled:
                continue
            # A child owns its fleet until every trip has physically returned.
            for stage, eid in enumerate(plan.path):
                if any(j.parent_id == plan.id and j.origin == plan.hubs[stage] and j.status != 'COMPLETED' for j in cargo.hauls.values()):
                    continue
                source = self.account(plan, stage)
                amount = inv.balance(source)
                if not amount:
                    continue
                carrier, capacity = self._carrier(plan, stage)
                weight = self.s.cargo_weights[plan.good]
                quantum = 1000 if self.s.catalog.goods[plan.good]['indivisible'] else 1
                amount = min(amount, floor(Q(capacity) / weight))
                amount -= amount % quantum
                if not amount:
                    continue
                cid = str(10**60 + self.serial)
                order = {'id': cid, 'kind': 'logistics', 'op': 'route_shipment', 'origin_hub': plan.hubs[stage],
                         'dest_hub': plan.hubs[stage + 1], 'corridor_path': [eid], 'good': plan.good,
                         'qty_mU': amount, 'carrier_recipe': carrier}
                if plan.propulsion == 'electric' and cargo.corridors[eid].mode == 'rail':
                    order['propulsion'] = 'electric'
                # A real unload at this hub is the sole handoff proof; the
                # incoming carrier may still be making its empty return.
                try:
                    prepared = TransportCommands(self.s).prepare(plan.owner, order, ctx, source_bucket=source.bucket,
                        delivery_bucket='available' if stage == len(plan.path) - 1 else self.bucket(plan, stage + 1), parent_id=plan.id)
                    prepared.commit()
                    self.serial += 1
                    plan.serial += 1
                except RuleViolation:
                    continue

    def advance(self, ctx):
        inv, cargo = self.s.initial.inventory, self.s.cargo
        for plan in self.plans.values():
            if plan.completed:
                continue
            if plan.cancelled:
                for i in range(len(plan.path)):
                    a = self.account(plan, i)
                    inv.transfer(a, Account(plan.owner, a.location, 'available', a.asset), inv.balance(a))
            active = any(j.parent_id == plan.id and j.status != 'COMPLETED' for j in cargo.hauls.values())
            waiting = any(inv.balance(self.account(plan, i)) for i in range(len(plan.path)))
            if not active and not waiting:
                if plan.repeat_cycles and not plan.cancelled:
                    if ctx.tick < plan.next_departure_tick:
                        continue
                    source = Account(plan.owner, plan.hubs[0], 'available', plan.good)
                    if inv.balance(source) < plan.quantity_mU:
                        continue
                    inv.transfer(source, self.account(plan, 0), plan.quantity_mU)
                    plan.next_departure_tick = ctx.tick + plan.repeat_cycles * ctx.clock.cycle_ticks
                else:
                    plan.completed = True
                    self.s.command_streams[plan.owner].plans.pop(plan.command_id, None)

    def cancel(self, owner, command_id):
        plan = self.plans.get(f'{owner}:{command_id}')
        if plan is None or plan.completed:
            return None
        def commit():
            plan.cancelled = True; plan.repeat_cycles = 0
            for job in tuple(self.s.cargo.hauls.values()):
                if job.parent_id == plan.id and job.status != 'COMPLETED':
                    prepared = self.s.cargo.cancel(owner, job.command_id)
                    if prepared:
                        prepared.commit()
            for i in range(len(plan.path)):
                a = self.account(plan, i)
                self.s.initial.inventory.transfer(a, Account(owner, a.location, 'available', a.asset), self.s.initial.inventory.balance(a))
        return PreparedCommand(commit)

    def snapshot(self):
        return {'plans': self.plans, 'serial': self.serial}

    def visible(self, owner):
        return {'plans': [p for p in self.plans.values() if p.owner == owner]}

    def audit(self):
        for p in self.plans.values():
            if len(p.hubs) != len(p.path) + 1:
                raise InvariantViolation('relay route custody differs from its hubs')
            for stage in range(len(p.path)):
                quantity = self.s.initial.inventory.balance(self.account(p, stage))
                if quantity < 0 or quantity % (1000 if self.s.catalog.goods[p.good]['indivisible'] else 1):
                    raise InvariantViolation('relay custody violates cargo quantum')

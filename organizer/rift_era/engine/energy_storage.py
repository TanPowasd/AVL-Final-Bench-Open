"""Single-direction storage with paid charge receipts and physical SOC limits."""

from collections import defaultdict, deque
from dataclasses import dataclass
from fractions import Fraction
import math

from .command_schema import fields, version
from .construction import WorkOrder
from .errors import InvariantViolation, RuleViolation
from .ledger import Account, CASH_BOOK_SCALE
from .numeric import Q, ceil, floor
from .payments import Obligation
from .power import Supply, storage_auto
from .protocol import PreparedCommand
from .provenance import Use


@dataclass(frozen=True)
class StorageSpec:
    capacity_mU: int
    power_mU_cycle: int
    roundtrip: Fraction
    technologies: frozenset[str]
    materials_mU: tuple[tuple[str, int], ...]
    installation_cycles: int


SPECS = {
    'battery': StorageSpec(80000, 10000, Q(85, 100), frozenset(('E04',)), (('battery', 8000),), 2),
    'pumped': StorageSpec(400000, 40000, Q(75, 100), frozenset(('E02', 'L01')),
                         (('steel', 30000), ('cement', 60000), ('basic_parts', 6000)), 30),
}


@dataclass
class Store:
    id: str
    owner: str
    hub: str
    host: str
    kind: str
    mode: str = 'idle'
    requested_power_mU: int | None = None
    direction: str = 'idle'
    quote_ru: Fraction = Q(0)
    soc_mU: int = 0
    battery_health_mU: int = 1000
    wear_remainder: Fraction = Q(0)
    # Historical input-energy basis; selling stored electricity removes it pro rata.
    input_energy_mU: Fraction = Q(0)
    charge_cost_ru_mU: Fraction = Q(0)
    charged_mU: int = 0
    discharged_mU: int = 0
    conversion_loss_mU: int = 0
    spill_mU: int = 0
    dispatched_tick: int = -1
    basis_rounding_steps: int = 0


class EnergyStorage:
    def __init__(self, state):
        self.state = state
        self.stores: dict[str, Store] = {}
        self.prices = {h: deque(maxlen=10) for h in state.initial.map.hubs}
        self.cycle_quantity = defaultdict(int)
        self.cycle_cost = defaultdict(Fraction)
        self.last_cycle = -1

    @staticmethod
    def entity(aid):
        return f'storage:{aid}'

    def availability(self, store):
        assets = self.state.initial.assets
        return assets[store.id].availability * assets[store.host].availability

    def capacity(self, store):
        spec = SPECS[store.kind]
        assets = self.state.initial.assets
        physical = Q(assets[store.id].health_mU * assets[store.host].health_mU, 1000000)
        water = Q(1)
        if store.kind == 'pumped':
            water = min(Q(1), self.state.hydrology.flow_ratio(store.hub))
        return floor(spec.capacity_mU * physical * Q(store.battery_health_mU, 1000) * water)

    @staticmethod
    def efficiency(store):
        # Runtime-fingerprinted binary64 sqrt, as required by 01's transcendentals.
        return Q.from_float(math.sqrt(float(SPECS[store.kind].roundtrip)))

    def begin_tick(self, ctx):
        if ctx.tick % ctx.clock.cycle_ticks:
            return
        if ctx.cycle <= self.last_cycle:
            raise InvariantViolation('storage cycle repeated')
        self.last_cycle = ctx.cycle
        for store in self.stores.values():
            self.choose(store)

    def choose(self, store):
        if store.mode == 'auto':
            average = store.charge_cost_ru_mU / store.input_energy_mU if store.input_energy_mU else Q(0)
            store.direction, store.quote_ru = storage_auto(tuple(self.prices[store.hub]), average, SPECS[store.kind].roundtrip)
        else:
            store.direction = store.mode
            # Manual discharge has no markup; charge uses the declared maximum.
            store.quote_ru = Q(0) if store.mode != 'charge' else 50 * self.state.rates[store.owner]

    def power_limit(self, store):
        nominal = SPECS[store.kind].power_mU_cycle
        return floor(min(nominal, nominal if store.requested_power_mU is None else store.requested_power_mU) * self.availability(store) / 10)

    def charge_demands(self):
        result = {}
        for store in self.stores.values():
            if store.direction == 'charge':
                free = max(0, self.capacity(store) - store.soc_mU)
                amount = min(self.power_limit(store), floor(Q(free) / self.efficiency(store)))
                result[self.entity(store.id)] = (store.hub, 5, amount)
        return result

    def supplies(self):
        result = []
        for store in self.stores.values():
            if store.direction == 'discharge':
                amount = min(self.power_limit(store), floor(min(store.soc_mU, self.capacity(store)) * self.efficiency(store)))
                if amount:
                    result.append(Supply(f'{self.entity(store.id)}:dispatch', store.hub, store.owner, amount, store.quote_ru))
        return result

    def settle_dispatch(self, ctx, allocated, loads):
        cost = self.state.frame.power_paid_ru_mU
        hubs = {load.id: load.hub for load in loads}
        for receipt in self.state.frame.power_receipts:
            hub = hubs[receipt.load]
            self.cycle_quantity[hub] += receipt.received_mU
        for entity, amount in cost.items():
            self.cycle_cost[hubs[entity]] += amount
        for store in self.stores.values():
            if store.dispatched_tick == ctx.tick:
                raise InvariantViolation('storage settled twice')
            store.dispatched_tick = ctx.tick
            old = store.soc_mU
            capacity = self.capacity(store)
            if old > capacity:
                removed = old - capacity
                self.retain_basis(store, Q(capacity, old))
                store.soc_mU = capacity
                store.spill_mU += removed
            entity = self.entity(store.id)
            charge = self.state.frame.power_received.get(entity, 0)
            discharge = allocated.get(entity, 0)
            if charge and discharge or charge and store.direction != 'charge' or discharge and store.direction != 'discharge':
                raise InvariantViolation('storage changed direction during dispatch')
            efficiency = self.efficiency(store)
            throughput = 0
            if charge:
                stored = min(capacity - store.soc_mU, round(charge * efficiency))
                store.soc_mU += stored
                store.input_energy_mU += charge
                store.charge_cost_ru_mU += cost.get(entity, 0)
                store.conversion_loss_mU += charge - stored
                store.charged_mU += charge
                throughput += stored
            if discharge:
                used = ceil(Q(discharge) / efficiency)
                if used > store.soc_mU:
                    raise InvariantViolation('discharge exceeded actual stored energy')
                retained = Q(store.soc_mU - used, store.soc_mU)
                self.retain_basis(store, retained)
                store.soc_mU -= used
                store.conversion_loss_mU += used - discharge
                store.discharged_mU += discharge
                throughput += used
            if store.kind == 'battery':
                wear = store.wear_remainder + Q(8 * throughput, 2 * SPECS['battery'].capacity_mU)
                store.battery_health_mU = max(0, store.battery_health_mU - floor(wear))
                store.wear_remainder = wear - floor(wear)

    @staticmethod
    def retain_basis(store, retained):
        """Bound both weighted-average histories on the cash book's fine grid."""
        for name in ('input_energy_mU', 'charge_cost_ru_mU'):
            exact = getattr(store, name) * retained
            value = Q(round(exact * CASH_BOOK_SCALE), CASH_BOOK_SCALE)
            setattr(store, name, value)
            store.basis_rounding_steps += int(exact != value)

    def end_cycle(self, ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):
            return
        for hub in self.prices:
            price = self.cycle_cost[hub] / self.cycle_quantity[hub] if self.cycle_quantity[hub] else self.state.grid_prices_ru[hub]
            self.prices[hub].append(price)
        self.cycle_quantity.clear()
        self.cycle_cost.clear()

    def remove(self, sid):
        self.stores.pop(sid, None)

    def audit(self):
        for store in self.stores.values():
            if (store.soc_mU < 0 or store.charged_mU != store.soc_mU + store.discharged_mU + store.conversion_loss_mU + store.spill_mU
                    or not 0 <= store.battery_health_mU <= 1000 or not 0 <= store.wear_remainder < 1
                    or self.state.slot_use.get(store.id) != f'storage:{store.id}'):
                raise InvariantViolation('storage custody, direction or energy conservation failure')

    def snapshot(self):
        return {'stores': self.stores, 'prices': {h: tuple(p) for h, p in self.prices.items()},
                'cycle_quantity': dict(self.cycle_quantity), 'cycle_cost': dict(self.cycle_cost), 'last_cycle': self.last_cycle}


class StorageCommands:
    OPERATIONS = frozenset({('industry', 'install_storage'), ('industry', 'refill_storage'), ('logistics', 'storage_mode')})

    def __init__(self, state, industry):
        self.state, self.industry = state, industry

    def prepare(self, owner, order, ctx):
        state, op = self.state, order['op']
        if op == 'install_storage':
            fields(order, {'facility_id': str, 'storage_kind': str}, {'expected_version': int})
            asset = self.industry._owned_asset(owner, order['facility_id'], order)
            facility = state.initial.facilities.get(asset.id)
            kind = order['storage_kind']
            if kind not in SPECS:
                raise RuleViolation('ERR_SCHEMA')
            spec = SPECS[kind]
            self.industry._tech(owner, spec.technologies)
            if facility is None or not facility.commissioned or facility.profile not in ('S2', 'S3', 'S4', 'S5'):
                raise RuleViolation('ERR_SITE_INCOMPATIBLE')
            if kind == 'pumped' and not state.hydrology.pumped_site(facility.hub):
                raise RuleViolation('ERR_SITE_INCOMPATIBLE')
            slots = [lid for lid in facility.line_ids if state.slot_use.get(lid) == 'unassigned' and not state.initial.assets[lid].work_locked]
            if not slots:
                raise RuleViolation('ERR_CAPACITY_EXCEEDED')
            slot = min(slots)
            jid, temporary = self.industry._ids(owner, order)
            costs = dict(spec.materials_mU)
            new_asset = self.industry._asset(temporary, owner, facility.hub, costs)
            job = WorkOrder(jid, str(int(order['id'])), owner, facility.hub, 'install_storage', slot,
                            spec.installation_cycles * ctx.clock.cycle_ticks, costs, temporary_asset=temporary,
                            payload={'host': asset.id, 'storage_kind': kind})
            def register():
                state.initial.assets[temporary] = new_asset
                state.initial.assets[slot].work_locked = True
                state.initial.assets[slot].version += 1
                state.slot_use[slot] = f'building:{jid}'
            return state.work.prepare_start(job, register)
        required = {'facility_id': str}
        if op == 'storage_mode':
            required['mode'] = str
        fields(order, required, {'expected_version': int, **({'power_mU': int} if op == 'storage_mode' else {})})
        asset = self.industry._owned_asset(owner, order['facility_id'], order)
        candidates = [s for s in state.storage.stores.values() if s.id == asset.id or s.host == asset.id]
        if len(candidates) != 1:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        store = candidates[0]
        if op == 'storage_mode':
            mode, power = order['mode'], order.get('power_mU')
            if mode not in ('auto', 'charge', 'discharge', 'idle') or power is not None and power < 0:
                raise RuleViolation('ERR_SCHEMA')
            def commit():
                store.mode, store.requested_power_mU = mode, power
                # Manual changes apply now; auto reads only completed history.
                state.storage.choose(store)
                asset.version += 1
            return PreparedCommand(commit)
        if store.kind != 'battery' or store.battery_health_mU >= 800:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        source = Account(owner, store.hub, 'available', 'battery')
        stock = state.initial.inventory.prepare_transform({source: 2000}, {}, reason='storage_battery_service', use=Use.MAINTENANCE)
        from .economic_ownership import prepare_purchase,record_income
        payment,recipients=prepare_purchase(state.payments,state.native(owner,'treasury'),stock.materials.events,
            {'battery':state.price('battery',store.hub)},kind='storage_battery_service',
            key=(ctx.seed,ctx.tick,'storage_battery_service',store.id),buyer=state.native(owner,'treasury'))
        def commit():
            payment.commit(stock)
            amount = sum(payment.amounts)
            metric = state.cycles[owner]
            metric.costs['treasury'] = metric.costs.get('treasury', Q(0)) + amount
            record_income(state,recipients,payment.amounts)
            state.macro.input(owner, store.hub, 'battery', 2000)
            store.battery_health_mU, store.wear_remainder = 1000, Q(0)
            asset.version += 1
        return PreparedCommand(commit)

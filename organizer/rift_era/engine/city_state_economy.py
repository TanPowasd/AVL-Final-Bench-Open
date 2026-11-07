"""Finite-cash neutral cities, physical local trade and receipt-backed relations.

NPC actors are deliberately not added to initial.known (the eight player
seats), or initial.populations (the national governance pipeline).  The world
candidate choices are versioned in data/city_states_v1.json.  Frozen r6
coefficients are read directly from the catalog.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
import json

from .assets import Asset
from .command_schema import fields
from .determinism import digest, fingerprint
from .diplomacy import Relation
from .economic_ownership import record_income, prepare_purchase, purchase_quote, EconomicOwner
from .errors import InvariantViolation, RuleViolation
from .ledger import Account, CashAccount, CashTransfer, DeliveryPayment, Transfer
from .materials import Origin
from .numeric import Q, ceil, clip, floor, max_feasible, proportional, units
from .payments import Obligation
from .population import Demography, Population, grow, support_delta, update_support
from .power import Load
from .protocol import PreparedCommand
from .provenance import ELIGIBLE, Use
from .spot import RestingOrder, Venue
from .world import draw
from .maintenance import Maintenance
from .city_power import CityPower
from .city_relations import CityRelations


@dataclass
class CitySite:
    id: str
    city: str
    good: str
    initial_mU: int
    source: Account
    elapsed_output_mU: int = 0
    output_remainder: Q = Q(0)
    last_output_mU: int = 0
    wage_lock: CashAccount | None = None
    wage_reserved_mU: int = 0
    labor_mU: int = 0
    wages_paid_mU: int = 0


@dataclass
class Affinity:
    trade: Q = Q(0)
    vital: Q = Q(0)
    security: Q = Q(1, 2)
    coercion: Q = Q(0)
    j: Q = Q(0)


@dataclass
class City:
    id: str
    hub: str
    population: Population
    initial_population_mU: int
    sites: tuple[str, ...]
    housing_asset: str
    affinity: dict[str, Affinity]
    history: list = field(default_factory=list)
    cycle_demand: dict[str, int] = field(default_factory=dict)
    cycle_used: dict[str, int] = field(default_factory=dict)
    cycle_external: dict[str, dict[str, Q]] = field(default_factory=dict)
    cycle_power_needed_mU: int = 0
    cycle_power_received_mU: int = 0
    cycle_labor_mU: int = 0
    partner: str | None = None
    treaty_kind: str | None = None
    expiry_cycle: int | None = None
    previous_partner: str | None = None
    direct_shares: dict[str, dict[str, int]] = field(default_factory=dict)
    last_report: dict = field(default_factory=dict)

    @property
    def treasury(self): return CashAccount(self.id, 'treasury', 'RU')

    @property
    def household(self): return CashAccount(self.id, f'household:{self.hub}', 'RU')


@dataclass
class CityProposal:
    city: str
    nation: str
    kind: str
    aid_ru_mU: int
    cash_lock: CashAccount
    material_lock: str
    site: str | None
    materials: dict[str, int]
    submitted_cycle: int


class CityStateEconomy:
    VERSION = 'rift_city_state_economy_v2'
    GOODS = ('food', 'consumer_goods', 'medicine', 'water')
    NEEDS = {'food': Q(6, 100), 'consumer_goods': Q(3, 100),
             'medicine': Q(1, 100), 'water': Q(4, 100)}
    THRESHOLD = {'resource_jv': Q(35, 100), 'port_lease': Q(1, 2), 'protectorate': Q(65, 100)}

    def __init__(self, state):
        self.s = state
        self.config = json.loads((Path(__file__).resolve().parents[1] / 'data' / 'city_states_v1.json').read_text())
        self.config_hash = fingerprint(self.config)
        self.cities: dict[str, City] = {}
        self.sites: dict[str, CitySite] = {}
        self.proposals: dict[tuple[str, str], CityProposal] = {}
        self.last_tick = -1
        self.last_produced_tick = -1
        self.last_settled_tick = -1
        self.last_activity_tick = -1
        self.last_market_tick = -1
        self.frame_power = {}
        self.power_locks = {}
        self.purchases = {}
        self.annexations = deque()
        self.annexed_cities = set()
        self.pending_handoffs = {}
        self.coercion_events = {}
        self.last_view = {}
        self._build()
        self.power = CityPower(self)
        self.relations = CityRelations(self)
        self.maintenance = Maintenance(state.initial.inventory, state.initial.cash, state.payments,
            state.initial.assets, seed=state.initial.map.seed, title_payments=True)
        for city in self.cities.values():
            costs = {city.housing_asset: {'cement': 200}, self.power.generators[city.id].id: {'basic_parts':100}}
            costs.update({f'{sid}/asset': {'basic_parts':100} for sid in city.sites})
            for aid, materials in costs.items():
                self.maintenance.register(aid, materials, city.treasury, {g:city.treasury for g in materials})

    def _build(self):
        state, initial = self.s, self.s.initial
        capital = {}
        for cid in sorted(initial.map.city_states):
            hub = f'{cid}-h0'
            amount = units(draw(initial.map.seed, initial.map.attempt, 'city_population', cid, 10, 20))
            terrain = initial.map.regions[initial.map.hubs[hub].region].terrain
            chosen = self.config['terrain_minerals'][terrain]
            site_ids = []
            for index, good in enumerate(('water', *chosen)):
                sid = f'{cid}/site/{index}'
                source = Account('nature', sid, 'deposit', good)
                reserve = 0 if good == 'water' else units(state.endowments['finite_reserve_units_per_starting_site'][good])
                self.sites[sid] = CitySite(sid, cid, good, reserve, source)
                if reserve:
                    initial.inventory.transform({}, {source: reserve}, reason='declared_city_state_deposit',
                        origins={source: Origin('nature', hub)})
                aid = f'{sid}/asset'
                material = {'steel': 4000000, 'cement': 4000000, 'basic_parts': 1000000}
                initial.assets[aid] = Asset(aid, cid, hub, material)
                capital[aid] = initial.assets[aid]
                site_ids.append(sid)
            house = f'{cid}/housing/0'
            initial.assets[house] = Asset(house, cid, hub, {'cement': 20000000, 'steel': 5000000})
            capital[house] = initial.assets[house]
            self.cities[cid] = City(cid, hub, Population(hub, {'general_worker': amount, 'skilled_worker': 0, 'engineer': 0}),
                amount, tuple(site_ids), house, {p: Affinity() for p in initial.known})
        # Explicit, audited initial embodied capital, never warehouse inventory.
        draft = initial.inventory.materials.draft()
        for asset in capital.values():
            for good, quantity in asset.book_nmU.items():
                draft.create(state.asset_lifecycle.account(asset, good), {Origin(asset.owner, asset.hub): quantity / 1000}, virtual=True)
        initial.inventory.transform({}, {}, reason='declared_city_state_capital', material_plan=draft.finish())
        state.authority.sync_assets(initial.assets)
        self.publish()

    def stock(self, city, good, bucket='available'):
        return Account(city.id, city.hub, bucket, good)

    def controller(self, city):
        region=self.s.initial.map.hubs[city.hub].region
        return self.s.authority.regions[region].controller

    def operation_factor(self, city):
        controller=self.controller(city)
        return Q(3,5) if controller not in (None,city.id) and self.s.relations.will_be_at_war(controller,city.id) else Q(1)

    def _order_id(self, city, good, side): return f'{city.id}/npc_city:{side}:{good}'

    def _cancel_orders(self, city):
        for order in tuple(self.s.spot.orders.values()):
            if order.owner == city.id:
                self.s.spot.prepare_cancel(city.id, order.id).commit()

    def _venue(self, city, good):
        sid = f'spot:{city.hub}:{good}'
        return Venue(sid, city.hub, good, 'RU', units(10 * self.s.catalog.weights[good]),
                     1000 if self.s.catalog.goods[good].get('indivisible') else 1)

    def _post(self, city, good, side, quantity, payer, ctx, *, role=''):
        if not quantity: return
        venue = self._venue(city, good)
        # National commands create this identical venue if they arrive first.
        existing = self.s.spot.venues.get(venue.id)
        if existing is not None and existing != venue:
            raise InvariantViolation('city market venue differs from public local venue')
        oid = self._order_id(city, good, side) + (':'+role if role else '')
        order = RestingOrder(oid, 'npc', city.id, venue.id, side, quantity,
            venue.initial_price_mU, (ctx.cycle + 1) * ctx.clock.cycle_ticks, payer,
            self.stock(city, good, 'available' if side == 'sell' else 'next_available'))
        self.s.spot.prepare_order(order, tick=ctx.tick,
            new_venue=None if venue.id in self.s.spot.venues else venue).commit()

    def begin_tick(self, ctx):
        if ctx.tick <= self.last_tick:
            raise InvariantViolation('city-state tick repeated or backdated')
        self.last_tick = ctx.tick
        for cid,nation in tuple(self.pending_handoffs.items()):
            self._handoff_annexed(self.cities[cid],nation)
            del self.pending_handoffs[cid]
        self.frame_power = {}
        self.power_locks = {}
        self.purchases = {}
        self.maintenance.begin_tick(ctx.tick)
        self.maintenance.reserve(self.maintenance.requests(lambda g,h:10*self.s.catalog.weights[g]))
        for city in self.cities.values():
            if city.id in self.annexed_cities:continue
            for sid in city.sites:
                site = self.sites[sid]
                site.wage_lock = None
                site.labor_mU = 0
                site.wage_reserved_mU = 0
                site.wages_paid_mU = 0
                site.last_output_mU = 0
            if ctx.tick % ctx.clock.cycle_ticks == 0:
                self._cancel_orders(city)
                city.cycle_demand = {good: floor(city.population.total_mU * rate) for good, rate in self.NEEDS.items()}
                city.cycle_used = {}
                city.cycle_external = {}
                city.cycle_power_needed_mU = city.cycle_power_received_mU = city.cycle_labor_mU = 0
                # Residents spend their actual funds in frozen rule priority.
                for good in self.GOODS:
                    remaining = max(0, city.cycle_demand[good] - self.s.initial.inventory.balance(self.stock(city, good, 'household')))
                    price = 10 * self.s.catalog.weights[good]
                    budget = self.s.initial.cash.balance(city.household).native_mU
                    buy = min(remaining, floor(Q(budget) / price)) if price else remaining
                    self._post(city, good, 'buy', buy, city.household, ctx)
                for sid in city.sites:
                    site = self.sites[sid]
                    if site.good == 'water': continue
                    usage20 = sum((row['used'].get(site.good, 0) for row in city.history), 0)
                    reserve = usage20 if len(city.history) == 20 else usage20 * 20 // max(1, len(city.history))
                    available = max(0, self.s.initial.inventory.balance(self.stock(city, site.good)) - reserve)
                    self._post(city, site.good, 'sell', min(available, units(self.s.catalog.runtime['extraction'][site.good]['output'])), city.treasury, ctx)
                for good, target in {'coal':30000,'water':30000,'basic_parts':2000,'cement':1000}.items():
                    missing=max(0,target-self.s.initial.inventory.balance(self.stock(city,good)))
                    price=10*self.s.catalog.weights[good]
                    quantity=min(missing,floor(self.s.initial.cash.balance(city.treasury).native_mU/price))
                    self._post(city,good,'buy',quantity,city.treasury,ctx,role='government')
            plant_workers=self.power.reserve(city,ctx)
            self._reserve_labor(city, ctx, plant_workers)
            self._reserve_power(city, ctx)
        self.relations.begin_cycle(ctx)

    def _reserve_labor(self, city, ctx, already_working=0):
        demand = {}
        for sid in city.sites:
            site = self.sites[sid]
            asset = self.s.initial.assets[f'{sid}/asset']
            demand[sid] = floor(Q(units(self.s.catalog.runtime['extraction'][site.good]['labor']), ctx.clock.cycle_ticks) * asset.availability * self.operation_factor(city))
        available = max(0,floor(city.population.total_mU * Q(65, 100) / ctx.clock.cycle_ticks))-already_working
        labor = proportional(available, demand, key=(ctx.seed, 'city_labor', ctx.tick, city.id))
        cash = self.s.initial.cash.balance(city.treasury).native_mU
        wage = {sid: quantity * 5 for sid, quantity in labor.items()}
        allocation = proportional(cash, wage, key=(ctx.seed, 'city_wages', ctx.tick, city.id))
        for sid, quantity in labor.items():
            site = self.sites[sid]
            # One mU of actual labor costs five mU RU.  Fractional unpaid
            # workers do not produce, and no unpaid salary becomes income.
            site.labor_mU = min(quantity, allocation[sid] // 5)
            reserved = site.labor_mU * 5
            lock = CashAccount(city.id, f'city_wage:{sid}', 'RU')
            self.s.initial.cash.transfer(city.treasury, lock, reserved)
            site.wage_lock, site.wage_reserved_mU = lock, reserved

    def _reserve_power(self, city, ctx):
        sources = []
        for sid in city.sites:
            site = self.sites[sid]
            row = self.s.catalog.runtime['extraction'][site.good]
            need = ceil(Q(units(row['power']), ctx.clock.cycle_ticks) *
                min(Q(1), Q(site.labor_mU * ctx.clock.cycle_ticks, max(1, units(row['labor'])))))
            sources.append((sid, need, city.treasury, 1 if site.good == 'water' else 3))
        need = floor(Q(city.population.total_mU, 50 * ctx.clock.cycle_ticks))
        city.cycle_power_needed_mU += need
        sources.append((f'city_residents:{city.id}', need, city.household, 1))
        grouped = defaultdict(dict)
        for entity, need, payer, rank in sources: grouped[payer][entity] = need * 50
        allocations = {}
        for payer, requested in grouped.items():
            allocations.update(proportional(self.s.initial.cash.balance(payer).native_mU, requested,
                key=(ctx.seed, 'city_power_cash', ctx.tick, city.id, payer.name)))
        for entity, need, payer, rank in sources:
            lock = CashAccount(city.id, f'city_power:{entity}', 'RU')
            amount = allocations[entity]
            self.s.initial.cash.transfer(payer, lock, amount)
            self.power_locks[entity] = (lock, payer)
            self.frame_power[entity] = Load(entity, city.hub, city.id, need, rank, Q(50), Q(amount), 'B1' if rank == 1 else 'B4')

    def power_loads(self):
        return tuple(value[0] if isinstance(value, tuple) else value
                     for _, value in sorted(self.frame_power.items()))

    def after_markets(self, ctx):
        """Freeze actual household purchases before national P7 buffers commit."""
        if ctx.tick <= self.last_market_tick:
            raise InvariantViolation('city market deliveries collected twice')
        self.relations.clear(ctx)
        for clearing in self.s.market_events:
            if clearing.tick != ctx.tick: continue
            for trade in clearing.trades:
                city = self.cities.get(trade.buyer)
                if city is not None and trade.buy_order == self._order_id(city, trade.good, 'buy'):
                    key = (city.id, trade.good)
                    self.purchases[key] = self.purchases.get(key, 0) + trade.quantity_mU
        self.last_market_tick = ctx.tick

    def record_power(self, ctx, result, payees):
        """Commit dispatch cash for city loads; host supplies actual source owners.

        ``payees`` maps source IDs to actual RU CashAccounts (same dispatcher
        sources used for every other load).  Receipts and budget are checked
        before any payment.  The dispatch itself remains the host's shared-grid
        operation; this method cannot invent delivered electricity.
        """
        bills = defaultdict(lambda: defaultdict(Q))
        quantities = defaultdict(int)
        for receipt in result.receipts:
            if receipt.load not in self.frame_power: continue
            if receipt.source not in payees: raise InvariantViolation('city power has no actual supplier account')
            quantities[receipt.load] += receipt.received_mU
            bills[receipt.load][payees[receipt.source]] += receipt.source_payment_ru_mU
            for operator, amount in receipt.tolls_ru_mU:
                bills[receipt.load][CashAccount(operator, 'corp:energy', 'RU')] += amount
        for entity, load in self.frame_power.items():
            if isinstance(load, tuple): raise InvariantViolation('city power settled twice')
            if quantities[entity] != result.received.get(entity, 0) or quantities[entity] > load.demand_mU:
                raise InvariantViolation('city power receipt differs from dispatch')
            lock, _ = self.power_locks[entity]
            obligations = tuple(Obligation(lock, target, amount, 'electricity') for target, amount in sorted(bills[entity].items()))
            plan = self.s.payments.prepare_invoice(obligations, kind='city_power', key=(ctx.seed, ctx.tick, entity),
                                                   payer_scope=self.power_locks[entity][1])
            plan.commit()
            record_income(self.s, tuple(o.target for o in obligations), plan.amounts)
            for obligation, paid in zip(obligations, plan.amounts):
                self.s.macro.electricity(load.owner, obligation.target.owner, paid, intermediate=entity in self.sites)
            if entity.startswith('city_residents:') and quantities[entity] and sum(plan.amounts):
                city = self.cities[load.owner]
                # Electricity is a delivered flow and cannot be stored or
                # relisted.  This records only actual paid municipal service.
                for receipt in result.receipts:
                    if receipt.load != entity: continue
                    supplier = payees[receipt.source].owner
                    if supplier in city.affinity:
                        row = city.cycle_external.setdefault(supplier, {})
                        value = Q(receipt.received_mU, 1000) * self.s.catalog.weights['electricity']
                        row['trade'] = row.get('trade', Q(0)) + value
                        row['vital'] = row.get('vital', Q(0)) + value
            # Actual received quantity lives only until the current P6/P7.
            self.frame_power[entity] = (load, quantities[entity])

    def _received(self, entity):
        value = self.frame_power.get(entity)
        return value[1] if isinstance(value, tuple) else 0

    def produce(self, ctx):
        if ctx.tick != self.last_tick or ctx.tick <= self.last_produced_tick:
            raise InvariantViolation('city production repeated or not initialized')
        for city in self.cities.values():
            if city.id in self.annexed_cities:continue
            # Finite local natural water inflow is the same declared 16-unit
            # BASE water source.  Unpumped water drains away every tick.
            water = self.sites[city.sites[0]]
            amount = units(self.s.catalog.runtime['extraction']['water']['output']) // ctx.clock.cycle_ticks
            self.s.initial.inventory.transform({}, {water.source: amount}, reason='city_natural_water_inflow',
                origins={water.source: Origin('nature', city.hub)})
            requests = {}
            for sid in city.sites:
                site = self.sites[sid]
                row = self.s.catalog.runtime['extraction'][site.good]
                work = min(Q(1), Q(site.labor_mU * ctx.clock.cycle_ticks, max(1, units(row['labor']))))
                load = self.frame_power[sid]
                load = load[0] if isinstance(load, tuple) else load
                power = min(Q(1), Q(self._received(sid), load.demand_mU)) if load.demand_mU else Q(1)
                remaining = self.s.initial.inventory.balance(site.source)
                efficiency = Q(1) if not site.initial_mU or remaining * 10 >= site.initial_mU * 3 else Q(1, 4) + Q(3, 4) * Q(remaining * 10, site.initial_mU * 3)
                rate = Q(units(row['output']), ctx.clock.cycle_ticks) * work * power * efficiency
                requests[sid] = min(remaining, floor(rate + site.output_remainder))
                site.output_remainder = rate + site.output_remainder - floor(rate + site.output_remainder)
            free = max(0, self.s.initial.warehouse_mU[city.hub] - self.s.warehouse_load_at(city.hub))
            # Different mineral cargo weights compete for the same warehouse.
            total = sum((q * self.s.cargo_weights[self.sites[sid].good] for sid, q in requests.items()), Q(0))
            scale = min(Q(1), Q(free) / total) if total else Q(1)
            for sid in city.sites:
                site = self.sites[sid]
                quantity = floor(requests[sid] * scale)
                row = self.s.catalog.runtime['extraction'][site.good]
                nominal = Q(units(row['output']), ctx.clock.cycle_ticks)
                actual_work = min(site.labor_mU, ceil(Q(quantity, max(1, requests[sid])) * site.labor_mU)) if quantity else 0
                wage = actual_work * 5
                paid = self.s.payments.prepare((Obligation(site.wage_lock, city.household, Q(wage), 'city_mine_wage'),))
                account = self.stock(city, site.good, 'next_available')
                rights = self.s.authority.assets[f'{sid}/asset']
                allocations = proportional(quantity, {p: quantity * share for p, share in rights.owner_shares.items()},
                    key=(ctx.seed, 'city_mine_title', ctx.tick, sid))
                draft = self.s.initial.inventory.materials.draft()
                draft.consume(site.source, Q(quantity), use=Use.EXTRACTION, reason='city_state_extraction', hub=city.hub)
                draft.create(account, {Origin(city.id, city.hub, economic_owner=EconomicOwner(p, 'treasury' if p == city.id else 'corp:mining')): Q(q)
                    for p, q in allocations.items() if q})
                stock = self.s.initial.inventory.prepare_transform({site.source: quantity}, {account: quantity},
                    reason='city_state_extraction', material_plan=draft.finish())
                paid.commit(stock)
                site.wages_paid_mU = wage
                city.cycle_labor_mU += actual_work
                site.elapsed_output_mU += quantity
                site.last_output_mU = quantity
            leftover = self.s.initial.inventory.balance(water.source)
            if leftover:
                self.s.initial.inventory.transform({water.source: leftover}, {}, reason='city_unpumped_water_outflow', use=Use.EXTRACTION)
        self.last_produced_tick = ctx.tick
        receipts=self.maintenance.settle()
        for receipt in receipts:
            record_income(self.s,tuple(a for a,q in receipt.suppliers_paid),tuple(q for a,q in receipt.suppliers_paid))

    def _consume(self, city, ctx):
        for good in self.GOODS:
            need = max(0, city.cycle_demand.get(good, 0) - city.cycle_used.get(good, 0))
            household = self.stock(city, good, 'household')
            # A buy order used residents' own cash. Its actual fill belongs to
            # residents and cannot be sold to those same residents again.
            warehouse = self.stock(city, good)
            bought = self.purchases.get((city.id, good), 0)
            if bought:
                self.s.initial.inventory.transfer(warehouse, household, bought)
            need_purchase = max(0, need - self.s.initial.inventory.balance(household))
            available = self.s.initial.inventory.balance(warehouse)
            price = 10 * self.s.catalog.weights[good]
            cash = self.s.initial.cash.balance(city.household).native_mU
            buy = max_feasible(min(need_purchase, available), lambda q:
                purchase_quote(self.s.payments, city.household, q * price, 'city_resident_purchase') <= cash)
            if buy:
                stock = self.s.initial.inventory.prepare_transfers((Transfer(warehouse, household, buy, retitle=True),))
                invoice, recipients = prepare_purchase(self.s.payments, city.household, stock.materials.deliveries,
                    {good: price}, kind='city_resident_purchase', key=(ctx.seed, ctx.tick, city.id, good))
                invoice.commit(stock)
                record_income(self.s, recipients, invoice.amounts)
            amount = min(need, self.s.initial.inventory.balance(household))
            aided = 0
            if amount < need:
                aided = self.relations.consume_aid(city, good, need - amount, ctx)
            if aided:
                city.cycle_used[good] = city.cycle_used.get(good, 0) + aided
            if amount:
                before = len(self.s.initial.inventory.materials.events)
                self.s.initial.inventory.transform({household: amount}, {}, reason='city_resident_consumption', use=Use.CIVILIAN)
                city.cycle_used[good] = city.cycle_used.get(good, 0) + amount
                for event in self.s.initial.inventory.materials.events[before:]:
                    origin = event.origin
                    if origin.nation not in city.affinity or origin.recognized: continue
                    row = city.cycle_external.setdefault(origin.nation, {})
                    value = Q(event.quantity_mU, 1000) * self.s.catalog.weights[good]
                    row['trade'] = row.get('trade', Q(0)) + value
                    if good in ('food', 'water', 'medicine'):
                        row['vital'] = row.get('vital', Q(0)) + value
        entity = f'city_residents:{city.id}'
        city.cycle_power_received_mU += self._received(entity)

    def settle(self, ctx):
        if ctx.tick != self.last_tick or ctx.tick <= self.last_settled_tick:
            raise InvariantViolation('city settlement repeated or not initialized')
        for city in self.cities.values():
            if city.id in self.annexed_cities:continue
            self._consume(city, ctx)
            for sid in city.sites:
                site = self.sites[sid]
                if site.wage_lock is not None:
                    self.s.payments.release(site.wage_lock, city.treasury)
            for entity, (lock, payer) in self.power_locks.items():
                if payer.owner == city.id: self.s.payments.release(lock, payer)
            if ctx.clock.is_cycle_end(ctx.tick):
                self._cancel_orders(city)
                # Subsidy does not finance the current P1's already locked
                # purchases and cannot reuse the same cycle's cash twice.
                self.s.initial.cash.transfer(city.treasury, city.household,
                    min(50000, self.s.initial.cash.balance(city.treasury).native_mU))
                self._end_cycle(city, ctx)
                controller=self.controller(city)
                if controller in self.s.initial.known:self.annex_if_eligible(city.id,controller,ctx.cycle+1)
        self.power.release_unrun()
        self.last_settled_tick = ctx.tick
        self.apply_industry_activity(ctx)

    advance = settle

    def _security(self, city, nation, cycle):
        region = self.s.initial.map.hubs[city.hub].region
        nearby = {region, *self.s.initial.map.regions[region].adjacent}
        protect = threat = hostile = Q(0)
        dip = self.s.initial.diplomacy
        for unit in self.s.initial.units.values():
            spec = self.s.catalog.units[unit.unit_type]
            if unit.region not in nearby or unit.hp_mU <= 0 or spec['domain'] not in ('Land', 'Sea-Surface', 'Sea-Sub'): continue
            ratio = Q(unit.hp_mU, max(1, units(spec['hp'])))
            ratio *= min(Q(1), Q(unit.crew_mU, max(1, units(spec['crew']))))
            fp = Q(spec['firepower']['ground'] + spec['firepower']['sea']) * ratio
            if unit.owner == nation and dip.relation(nation, city.id) != Relation.WAR: protect += fp
            if unit.owner != nation and (dip.relation(unit.owner, nation) == Relation.WAR or dip.relation(unit.owner, city.id) == Relation.WAR): threat += fp
            if unit.owner == nation and dip.relation(nation, city.id) == Relation.WAR: hostile += fp
        annex = sum(1 for c, p in self.annexations if p == nation and c > cycle - 200)
        security = clip(Q(1, 2) + Q(1, 2) * (protect - threat) / (protect + threat + 100) - Q(2, 5) * annex, Q(0), Q(1))
        policy = self.s.export_controls.policies.get(nation)
        embargo = bool(policy and city.id in policy.targets and policy.goods)
        coercion = clip(hostile / 500 + Q(int(embargo), 2) + self.coercion_events.get((city.id, nation), (0, Q(0)))[1]
            * int(self.coercion_events.get((city.id, nation), (0, Q(0)))[0] > cycle), Q(0), Q(1))
        return security, coercion

    def _end_cycle(self, city, ctx):
        demand_value = sum((Q(city.cycle_demand[g], 1000) * self.s.catalog.weights[g] for g in ('food', 'water', 'medicine')), Q(0))
        demand_value += Q(city.cycle_power_needed_mU, 1000) * self.s.catalog.weights['electricity']
        city.history.append({'cycle': ctx.cycle, 'used': dict(city.cycle_used), 'external': city.cycle_external, 'vital_demand': demand_value})
        city.history[:] = city.history[-20:]
        alpha = Q(2, 21)
        for nation, affinity in city.affinity.items():
            trade = sum((r['external'].get(nation, {}).get('trade', Q(0)) for r in city.history), Q(0))
            vital = sum((r['external'].get(nation, {}).get('vital', Q(0)) for r in city.history), Q(0))
            vital += self.relations.aid_value(city.id,nation)
            demand = sum((r['vital_demand'] for r in city.history), Q(0))
            security, coercion = self._security(city, nation, ctx.cycle)
            for key, value in {'trade': min(Q(1), trade / 20 / 500), 'vital': min(Q(1), vital / demand) if demand else Q(0),
                               'security': security, 'coercion': coercion}.items():
                setattr(affinity, key, getattr(affinity, key) + alpha * (value - getattr(affinity, key)))
            affinity.j = Q(3, 10) * affinity.trade + Q(1, 4) * affinity.vital + Q(1, 5) * affinity.security + Q(1, 4) * self.s.credit[nation] - Q(35, 100) * affinity.coercion
        food = Q(city.cycle_used.get('food', 0), max(1, city.cycle_demand.get('food', 0)))
        medicine = Q(city.cycle_used.get('medicine', 0), max(1, city.cycle_demand.get('medicine', 0)))
        consumer = Q(city.cycle_used.get('consumer_goods', 0), max(1, city.cycle_demand.get('consumer_goods', 0)))
        house = floor(100000 * self.s.initial.assets[city.housing_asset].availability)
        employment = min(Q(1), Q(city.cycle_labor_mU, max(1, floor(city.population.total_mU * Q(65, 100)))))
        births, deaths = grow(ctx.seed, ctx.cycle, city.population, Demography(food, house, min(Q(1), consumer / 2), Q(0), Q(0), Q(0), Q(0)))
        region = self.s.initial.map.hubs[city.hub].region
        authority=self.s.authority.regions[region]
        occupied=authority.controller not in (None,city.id)
        if occupied: city.population.support_mU=authority.support_mU
        update_support(city.population, support_delta(food=food, consumer_goods=consumer, medicine=medicine,
            employment=employment, tax=Q(0), fatigue=Q(0), pollution=Q(0), autonomy=Q(0), social_security=None, occupied=occupied))
        if occupied: authority.support_mU=city.population.support_mU
        if city.expiry_cycle is not None and ctx.cycle + 1 >= city.expiry_cycle:
            city.previous_partner = city.partner
            city.partner = city.treaty_kind = city.expiry_cycle = None
        if (ctx.cycle + 1) % self.s.catalog.runtime['city_states']['evaluation_period'] == 0 and city.partner is None:
            self._choose(city, ctx)
        city.last_report = {'consumed_mU': dict(city.cycle_used), 'needed_mU': dict(city.cycle_demand),
            'births_mU': births, 'deaths_mU': deaths, 'power_received_mU': city.cycle_power_received_mU,
            'power_needed_mU': city.cycle_power_needed_mU, 'worked_mU': city.cycle_labor_mU}
        while self.annexations and self.annexations[0][0] <= ctx.cycle - 200: self.annexations.popleft()
        self.coercion_events = {k: v for k, v in self.coercion_events.items() if v[0] > ctx.cycle}

    def _choose(self, city, ctx):
        candidates = [p for (c, n), p in self.proposals.items() if c == city.id and city.affinity[n].j >= self.THRESHOLD[p.kind]
                      and not self.s.relations.will_be_at_war(n, city.id)]
        if not candidates: return
        winner = min(candidates, key=lambda p: (-city.affinity[p.nation].j, digest(ctx.seed, 'city_partner', ctx.cycle, city.id, p.nation)))
        incumbent = next((p for p in candidates if p.nation == city.previous_partner), None)
        if incumbent is not None and city.affinity[winner.nation].j <= city.affinity[incumbent.nation].j + Q(3, 100):
            winner = incumbent
        equity = None
        if winner.kind == 'resource_jv':
            asset = self.s.initial.assets[f'{winner.site}/asset']
            rights = self.s.authority.assets[asset.id]
            # Existing direct investments are not confiscated at expiry.  A
            # second 20% sale requires an actual unpledged city share.
            if rights.free_shares(city.id) < 200:
                self._refund(winner)
                del self.proposals[city.id, winner.nation]
                return
            equity = asset.id
        # Construction material contribution is explicitly consumed once as an
        # investment expense.  It cannot be relisted or earn trade/aid receipt.
        consumed = {Account(winner.nation, city.hub, winner.material_lock, g): q for g, q in winner.materials.items()}
        cash = self.s.initial.cash.prepare_transfers((CashTransfer(winner.cash_lock, city.treasury,
            self.s.initial.cash.balance(winner.cash_lock).native_mU),))
        stock = self.s.initial.inventory.prepare_transform(consumed, {}, reason='city_jv_contribution', use=Use.CONSTRUCTION)
        stock.check_current(); cash.check_current(); stock.commit(); cash.commit()
        if equity is not None:
            self.s.authority.transfer_shares(equity, city.id, winner.nation, 200)
            city.direct_shares.setdefault(winner.site, {})[winner.nation] = city.direct_shares.get(winner.site, {}).get(winner.nation, 0) + 200
        city.partner, city.treaty_kind = winner.nation, winner.kind
        city.expiry_cycle = ctx.cycle + 1 + self.s.catalog.runtime['city_states']['contract_duration']
        if winner.kind == 'protectorate':
            # Reuse the real type-7 defense obligation machine; this conveys
            # no sovereignty, base or free transport permission.
            from .contracts import DefenseTreaty
            self.s.contracts.defense.append(DefenseTreaty(tuple(sorted((city.id,winner.nation))),
                ctx.cycle + 1, city.expiry_cycle))
        for (c, n), proposal in tuple(self.proposals.items()):
            if c == city.id:
                if proposal is not winner: self._refund(proposal)
                del self.proposals[c, n]

    def _refund(self, proposal):
        city = self.cities[proposal.city]
        goods = tuple(Transfer(Account(proposal.nation, city.hub, proposal.material_lock, g),
            Account(proposal.nation, city.hub, 'available', g), q) for g, q in proposal.materials.items())
        money = (CashTransfer(proposal.cash_lock, CashAccount(proposal.nation, 'reserves', 'RU'),
            self.s.initial.cash.balance(proposal.cash_lock).native_mU),)
        DeliveryPayment.prepare(self.s.initial.inventory, self.s.initial.cash, goods, money).commit()

    def prepare_treaty(self, nation, order, ctx):
        fields(order, {'city_state_id': str, 'treaty_kind': str, 'aid_ru_mU': int})
        city = self.cities.get(order['city_state_id']); kind = order['treaty_kind']; aid = order['aid_ru_mU']
        if city is None: raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if kind not in self.THRESHOLD or aid < 0: raise RuleViolation('ERR_SCHEMA')
        if self.s.relations.will_be_at_war(nation, city.id): raise RuleViolation('ERR_NO_PERMISSION')
        if city.affinity[nation].j < self.THRESHOLD[kind]: raise RuleViolation('ERR_NO_PERMISSION', 'city affinity below treaty threshold')
        if (city.id, nation) in self.proposals: raise RuleViolation('ERR_NO_PERMISSION', 'city proposal already pending')
        if kind == 'port_lease':
            if not any(r.mode == 'sea' and city.hub in (r.a, r.b) for r in self.s.initial.map.routes.values()):
                raise RuleViolation('ERR_NO_PERMISSION', 'city has no sea port')
        site = next((sid for sid in city.sites if self.sites[sid].good != 'water' and
                     self.s.authority.assets[f'{sid}/asset'].free_shares(city.id) >= 200), None) if kind == 'resource_jv' else None
        if kind == 'resource_jv' and site is None: raise RuleViolation('ERR_INSUFFICIENT_STOCK')
        material = {'steel': 800, 'cement': 800, 'basic_parts': 200} if site else {}
        lockname = f'city_treaty:{city.id}:{nation}'
        cashlock = CashAccount(nation, lockname, 'RU')
        stock = tuple(Transfer(Account(nation, city.hub, 'available', g), Account(nation, city.hub, lockname, g), q) for g, q in material.items())
        # r6 does not authorize arbitrary remote asset giveaways: all materials
        # must already be at the city hub, and money must already be RU.
        plan = DeliveryPayment.prepare(self.s.initial.inventory, self.s.initial.cash, stock,
            (CashTransfer(CashAccount(nation, 'reserves', 'RU'), cashlock, aid + (200000 if site else 0)),))
        proposal = CityProposal(city.id, nation, kind, aid, cashlock, lockname, site, material, ctx.cycle)
        def commit():
            plan.commit(); self.proposals[city.id, nation] = proposal
        return PreparedCommand(commit)

    def publish(self):
        self.last_view = {p: {'cities': [{'id': c.id, 'hub_id': c.hub,
            'population_mU': c.population.total_mU, 'support_mU': c.population.support_mU,
            'partner': c.partner, 'treaty_kind': c.treaty_kind, 'expiry_cycle': c.expiry_cycle,
            'affinity': vars(c.affinity[p]).copy(), 'report': dict(c.last_report),
            'proposal_pending': (c.id, p) in self.proposals,
            'city_aid_mean20': self.relations.aid_mean20(p) if hasattr(self,'relations') else Q(0),
            'direct_shares_mU': {sid: row.get(p, 0) for sid, row in c.direct_shares.items()}}
            for c in sorted(self.cities.values(), key=lambda c: c.id)]} for p in self.s.initial.known}

    def visible(self, nation):
        import copy
        result=copy.deepcopy(self.last_view[nation])
        for row in result['cities']:
            row['affinity_mU']={k:round(v*1000) for k,v in row.pop('affinity').items()}
            row['city_aid_mean20_mU']=floor(row.pop('city_aid_mean20')*1000)
        return result

    def transit_lease(self, city_id, nation, cycle):
        """An actual live port lease authorizes passage, never free capacity."""
        city = self.cities.get(city_id)
        if (city is None or city.partner != nation or city.treaty_kind != 'port_lease'
                or city.expiry_cycle is None or cycle >= city.expiry_cycle):
            return None
        return {'hub_id': city.hub, 'expires_cycle': city.expiry_cycle,
                'fee_ru_per_load': self.s.catalog.runtime['city_states']['port_fee_ru_per_load']}

    def activity_rows(self):
        """Independent rows for IndustryActivity/corporate valuation integration."""
        rows = self.power.activity_rows()
        for city in self.cities.values():
            if city.id in self.annexed_cities:continue
            for sid in city.sites:
                site = self.sites[sid]
                rows.append({'asset': f'{sid}/asset', 'owner': city.id, 'hub': city.hub,
                    'sector': 'mining', 'outputs_mU': {site.good: site.last_output_mU},
                    'inputs_mU': {}, 'wages_native_mU': site.wages_paid_mU,
                    'maintenance_native_mU': sum(r.paid_mU for r in self.maintenance.last_receipts if r.asset_id==f'{sid}/asset'),
                    'direct_shares': dict(self.s.authority.assets[f'{sid}/asset'].owner_shares)})
        return rows

    def apply_industry_activity(self,ctx):
        """Commit foreign-owned city activities into the existing VA collector.

        Each company receives only its legal direct equity fraction. Extraction
        has no positive industrial VA; actual electricity/intermediate inputs
        and wages remain costs, while stock sale cash follows lot title.
        """
        activity=self.s.industry_activity
        if getattr(self,'last_activity_tick',-1)>=ctx.tick:raise InvariantViolation('city activity collected twice')
        from .industry_activity import Activity
        for data in self.activity_rows():
            if data['sector']=='mining':
                # Natural extraction itself never creates industrial value.
                outputs={}
                inputs={'electricity':self._received(data['asset'].rsplit('/asset',1)[0])}
            else:
                inputs=data['inputs_mU'];outputs=data['outputs_mU']
            for nation,share in data['direct_shares'].items():
                cid=nation+'-'+data['sector']
                if cid not in self.s.initial.companies or not share:continue
                key=data['asset'],cid
                row=activity.rows.setdefault(key,Activity(data['asset'],cid,nation,data['hub'],Q(share,1000)))
                for good,quantity in outputs.items():row.outputs_mU[good]=row.outputs_mU.get(good,Q(0))+quantity
                for good,quantity in inputs.items():row.inputs_mU[good]=row.inputs_mU.get(good,Q(0))+quantity
                # City cash is RU; equity holder's valuation uses its actual
                # native/RU rate without changing or minting a cash balance.
                row.wages_native_mU+=Q(data['wages_native_mU'])*share/1000/self.s.rates[nation]
                row.maintenance_native_mU+=Q(data['maintenance_native_mU'])*share/1000/self.s.rates[nation]
        self.last_activity_tick=ctx.tick

    def scoring_inputs(self,nation):
        """Concrete city control and irrevocably used aid inputs, without V."""
        influence=Q(0)
        for city in self.cities.values():
            if city.id in self.annexed_cities:continue
            initial_extract=sum((self.s.catalog.weights[self.sites[sid].good] *
                Q(self.s.catalog.runtime['extraction'][self.sites[sid].good]['output'])
                for sid in city.sites if self.sites[sid].good!='water'),Q(0))
            weight=clip(Q(1,2)+Q(city.initial_population_mU,50000)+Q(1,50)*initial_extract,Q(1,2),Q(2))
            if city.partner==nation and city.affinity[nation].j>=Q(35,100):influence+=weight
        return {'city_state_alignment':influence,'realized_aid_mean20':self.relations.aid_mean20(nation)}

    def record_coercion(self,nation,cycle):
        """Actual attributed S08 event; caller supplies already paid action."""
        if nation not in self.s.initial.known: raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        for city in self.cities:
            self.coercion_events[city,nation] = (cycle + 20,Q(15,100))

    def record_annexation(self,city_id,nation,cycle):
        city=self.cities.get(city_id)
        if city is None or nation not in self.s.initial.known: raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        region=self.s.authority.regions[self.s.initial.map.hubs[city.hub].region]
        if region.sovereign!=nation: raise RuleViolation('ERR_NO_PERMISSION','annexation requires actual sovereignty')
        if city_id in self.annexed_cities: return
        self.annexed_cities.add(city_id)
        self.annexations.append((cycle,nation))

    def annex_if_eligible(self, city_id, nation, cycle):
        """Perform the sole P7 annex transition after 240 real cycles.

        All city cash/stock remains in the same physical accounts until the
        transition; the controller receives only a real ledger transfer and
        third-party economic titles remain unchanged.
        """
        city=self.cities.get(city_id)
        if city is None: return False
        region=self.s.initial.map.hubs[city.hub].region
        auth=self.s.authority.regions[region]
        if auth.sovereign!=city.id or auth.controller!=nation or auth.occupied_since_tick is None:return False
        rules=self.s.catalog.runtime['diplomacy']
        if cycle - auth.occupied_since_tick // 10 < rules['city_state_annex_cycles'] or auth.support_mU<rules['city_state_annex_support_mU']:return False
        auth.sovereign=nation;auth.controller=nation;auth.version+=1
        self.record_annexation(city_id,nation,cycle)
        city.partner=None;city.treaty_kind=None;city.expiry_cycle=None
        for key,proposal in tuple(self.proposals.items()):
            if proposal.city==city_id:self._refund(proposal);del self.proposals[key]
        self._cancel_orders(city)
        # Cash stays RU: military control cannot rename residents' savings.
        initial=self.s.initial
        money=tuple(CashTransfer(a,CashAccount(nation,a.name if a.name.startswith('household:') else 'reserves',a.currency),b.native_mU)
            for a,b in initial.cash.balances().items() if a.owner==city_id)
        goods=tuple(Transfer(a,Account(nation,a.location,a.bucket,a.asset),q)
            for a,q in initial.inventory.balances().items() if a.owner==city_id)
        draft=initial.inventory.materials.draft()
        for transfer in goods:
            pieces=draft.take(transfer.source,Q(transfer.quantity))
            draft.deliver(transfer.source,transfer.destination,pieces,retitle=False)
        DeliveryPayment.prepare(initial.inventory,initial.cash,goods,money,material_plan=draft.finish()).commit()
        # The city NPC no longer takes autonomous economic decisions.  Its
        # residents and physical capital enter the conqueror's normal pipeline
        # at the next P1 boundary, retaining skills, health and third-party
        # direct shares.  Hand-off is performed once, never copied.
        self.pending_handoffs[city.id]=nation
        city.last_report['annexed_by']=nation
        from .diplomacy import Relation
        for pair,row in initial.diplomacy.pairs.items():
            if city_id in pair:row.state=Relation.PEACE;row.aggressors=frozenset();row.expiry_tick=None
        return True

    def _handoff_annexed(self,city,nation):
        from dataclasses import replace
        initial=self.s.initial
        if city.hub in initial.populations:raise InvariantViolation('annexed city population already registered')
        initial.populations[city.hub]=city.population
        initial.map.hubs[city.hub]=replace(initial.map.hubs[city.hub],owner=nation)
        initial.housing_mU[city.hub]=floor(100000*initial.assets[city.housing_asset].availability)
        from .economy_state import CycleEconomy
        self.s.local_cycles[city.hub]=CycleEconomy();self.s.local_history[city.hub]=deque(maxlen=40)
        self.s.pollution[city.hub]=0;self.s.rain[city.hub]=Q(1);self.s.natural_water_mU[city.hub]=0;self.s.water_used_mU[city.hub]=0
        from .power import GridRecovery
        self.s.grids[city.hub]=GridRecovery()
        self.s.governance.autonomy.setdefault(initial.map.hubs[city.hub].region,Q(0))
        from .initialization import Facility,MineSite,InitialLinePolicy
        from .production import Line,Recipe
        from .economy_state import Deposit
        # Remaining sovereign city equity transfers once.  Any pre-existing
        # foreign direct shareholder stays on the same capital book.
        asset_ids=[city.housing_asset,self.power.generators[city.id].id,*[f'{sid}/asset' for sid in city.sites]]
        draft=initial.inventory.materials.draft()
        for aid in asset_ids:
            asset=initial.assets[aid];rights=self.s.authority.assets[aid]
            shares=rights.owner_shares.pop(city.id,0)
            if shares:rights.owner_shares[nation]=rights.owner_shares.get(nation,0)+shares
            rights.operator=nation;rights.version+=1
            for good,amount in asset.book_nmU.items():
                source=self.s.asset_lifecycle.account(asset,good)
                pieces=draft.take(source,amount/1000)
                draft.add(Account(nation,asset.hub,source.bucket,good),pieces,virtual=True)
            asset.owner=nation
            self.maintenance.targets.pop(aid,None);self.maintenance.status.pop(aid,None)
        initial.inventory.transform({}, {},reason='city_annex_capital_transfer',material_plan=draft.finish())
        initial.facilities[city.housing_asset]=Facility(city.housing_asset,nation,city.hub,'S1','housing',())
        self.s.maintenance.register(city.housing_asset,{'cement':200},self.s.native(nation,'treasury'),{'cement':self.s.native(nation,'treasury')},commissioned_tick=self.last_tick+1)
        gen=self.power.generators[city.id]
        lid=f'{gen.id}/line'
        lineasset=Asset(lid,nation,city.hub,{})
        initial.assets[lid]=lineasset
        initial.lines[lid]=Line(lid,nation,city.hub,Recipe.from_catalog(self.s.catalog,'P070'))
        initial.plants[lid]=gen.plant
        gen.plant.id=lid
        initial.facilities[gen.id]=Facility(gen.id,nation,city.hub,'S1','电站',(lid,))
        initial.line_policies[lid]=InitialLinePolicy(1000,None)
        self.s.slot_use[lid]='production'
        self.s.maintenance.register(gen.id,{'basic_parts':100},self.s.native(nation,'corp:energy'),{'basic_parts':self.s.native(nation,'treasury')},commissioned_tick=self.last_tick+1)
        for sid in city.sites:
            site=self.sites[sid];aid=f'{sid}/asset'
            mine=MineSite(aid,nation,city.hub,site.good,Q(1))
            initial.mines[aid]=mine;self.s.resource_sites[aid]=mine
            source=('nature',f'basin:{city.hub}','deposit') if site.good=='water' else (site.source.owner,site.source.location,site.source.bucket)
            remaining=initial.inventory.balance(site.source)
            self.s.deposits[aid]=Deposit(site.good,site.initial_mU,remaining,site.good=='water')
            self.s.mine_deposit[aid]=aid;self.s.resource_sources[aid]=source;self.s.extraction_sources[aid]=source
            output=units(self.s.catalog.runtime['extraction'][site.good]['output'])
            self.s.extraction_lines[aid]=Line(aid,nation,city.hub,Recipe('extract:'+site.good,{site.good:output},{site.good:output},frozenset(),frozenset()))
            self.s.maintenance.register(aid,{'basic_parts':100},self.s.native(nation,'corp:mining'),{'basic_parts':self.s.native(nation,'treasury')},commissioned_tick=self.last_tick+1)
        self.s.authority.sync_assets(initial.assets)
        city.last_report['annexed_by']=nation

    def population_scoring_rows(self,nation,cycle):
        """The actual controller alone receives local occupied population."""
        from .scoring import population_pillar
        rows=[]
        for city in self.cities.values():
            if city.id in self.annexed_cities:continue
            region=self.s.initial.map.hubs[city.hub].region;authority=self.s.authority.regions[region]
            if authority.controller!=nation:continue
            needed=city.last_report.get('needed_mU',{});used=city.last_report.get('consumed_mU',{})
            ratio=lambda good:Q(used.get(good,0),max(1,needed.get(good,0)))
            electricity=Q(city.last_report.get('power_received_mU',0),max(1,city.last_report.get('power_needed_mU',0)))
            hold=max(0,cycle-(authority.occupied_since_tick or cycle*10)//10)
            value=population_pillar(city.population.total_mU,authority.support_mU,
                homeland=False,hold_cycles=hold,food=ratio('food'),synthetic_share=Q(0),consumer_goods=ratio('consumer_goods'),
                medicine=ratio('medicine'),advanced_medicine=Q(0),water=ratio('water'),electricity=electricity,
                combat=Q(0),blockade=Q(0),pollution=Q(0),remediation=Q(0))
            rows.append({'city_id':city.id,'region_id':region,'population_mU':city.population.total_mU,
                'controller':nation,'sovereign':authority.sovereign,'support_mU':authority.support_mU,'value':value})
        return rows

    def snapshot(self):
        return {'version': self.VERSION, 'config_hash': self.config_hash, 'cities': self.cities, 'sites': self.sites,
                'power': self.power.snapshot(), 'maintenance': self.maintenance.snapshot(),
                'relations': self.relations.snapshot(),
                'proposals': [[c, p, value] for (c, p), value in sorted(self.proposals.items())],
                'last_tick': self.last_tick, 'last_produced_tick': self.last_produced_tick,
                'last_settled_tick': self.last_settled_tick, 'last_market_tick': self.last_market_tick, 'frame_power': self.frame_power,
                'power_locks': self.power_locks, 'purchases': [[c,g,q] for (c,g),q in sorted(self.purchases.items())], 'annexations': list(self.annexations),
                'annexed_cities': self.annexed_cities,
                'pending_handoffs': self.pending_handoffs,'last_activity_tick':self.last_activity_tick,
                'coercion_events': [[c, p, value] for (c, p), value in sorted(self.coercion_events.items())],
                'last_view': self.last_view}

    def audit(self):
        self.power.audit()
        self.maintenance.audit()
        self.relations.audit()
        if set(self.cities) != set(self.s.initial.map.city_states): raise InvariantViolation('city roster changed')
        for city in self.cities.values():
            if city.population.total_mU < 0 or len(city.history) > 20: raise InvariantViolation('invalid city population/history')
            if any(not 0 <= getattr(row, key) <= 1 for row in city.affinity.values() for key in ('trade', 'vital', 'security', 'coercion')):
                raise InvariantViolation('city affinity outside [0,1]')
            for sid in city.sites:
                site = self.sites[sid]
                if city.id not in self.annexed_cities and site.initial_mU and self.s.initial.inventory.balance(site.source) + site.elapsed_output_mU != site.initial_mU:
                    raise InvariantViolation('city finite deposit conservation')
                if not 0 <= site.output_remainder < 1: raise InvariantViolation('city output remainder outside [0,1)')
                expected=city.id if city.id in self.pending_handoffs else city.last_report.get('annexed_by',city.id)
                if self.s.initial.assets[f'{sid}/asset'].owner != expected: raise InvariantViolation('city ownership silently replaced')
        for (cid, nation), proposal in self.proposals.items():
            if (cid, nation) != (proposal.city, proposal.nation): raise InvariantViolation('invalid city proposal key')
            if self.s.initial.cash.balance(proposal.cash_lock).native_mU != proposal.aid_ru_mU + (200000 if proposal.site else 0):
                raise InvariantViolation('city proposal lost cash escrow')
            for good, q in proposal.materials.items():
                if self.s.initial.inventory.balance(Account(nation, self.cities[cid].hub, proposal.material_lock, good)) != q:
                    raise InvariantViolation('city proposal lost material escrow')


class CityStateCommands:
    OPERATIONS = frozenset({('diplomacy', 'city_state_treaty'),('diplomacy','city_state_aid'),('trade','city_reexport')})
    def __init__(self, state): self.s = state
    def prepare(self, owner, order, ctx):
        if order['op']=='city_state_treaty':return self.s.city_economy.prepare_treaty(owner,order,ctx)
        if order['op']=='city_state_aid':
            fields(order,{'city_state_id':str,'good':str,'qty_mU':int})
            return self.s.city_economy.relations.prepare_aid(owner,order['city_state_id'],order['good'],order['qty_mU'],tick=ctx.tick)
        fields(order,{'city_state_id':str,'good':str,'qty_mU':int,'price_ru_mU':int})
        # The re-export price is the fixed public city quote, not a buyer-
        # controlled underpayment.  Acceptance only supplies that exact quote.
        if order['good'] not in self.s.catalog.weights or order['price_ru_mU']!=units(10*self.s.catalog.weights[order['good']]):raise RuleViolation('ERR_SCHEMA')
        return self.s.city_economy.relations.prepare_reexport_order(owner,order,ctx)

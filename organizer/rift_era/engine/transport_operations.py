"""World cargo custody, finite fleets and tick-local corridor arbitration.

The transport state machine owns motion. This adapter owns admission, inventory
locks and warehouse competition; command validation never advances a vehicle.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass

from .cargo import warehouse_load
from .command_schema import fields
from .determinism import digest
from .errors import InvariantViolation, RuleViolation
from .ledger import Account, CashAccount, Transfer
from .logistics import Corridor, Transport, TransitMode, Vehicle
from .export_controls import CONTROLLED_GOODS
from .numeric import Q, ceil, floor, proportional
from .protocol import PreparedCommand
from .provenance import Use


@dataclass
class Haul:
    id: str
    owner: str
    command_id: str
    origin: str
    destination: str
    path: tuple[str, ...]
    good: str
    quantity_mU: int
    remaining_mU: int
    vehicle_ids: tuple[str, ...]
    repeat_cycles: int
    next_departure_tick: int
    status: str = 'ACTIVE'
    serial: int = 0
    cancelled: bool = False
    delivery_bucket: str = 'available'
    parent_id: str | None = None


class CargoOperations:
    def __init__(self, state):
        self.s = state
        self.corridors = {r.id: Corridor(r.id, r.a, r.b, r.length, 30000, mode=r.mode)
                          for r in state.initial.map.routes.values()
                          if r.initial_operational and r.mode in ('road', 'rail', 'river', 'sea', 'air')}
        for edge in self.corridors.values():
            edge.capacity_mU_cycle = {'road': 30000, 'rail': 320000, 'river': 40000,
                                       'sea': 240000, 'air': 60000}[edge.mode]
        self.levels = dict.fromkeys(self.corridors, 1)
        self.paths = {e: state.initial.map.routes[e].cells for e in self.corridors}
        self.road_assets = {}
        self.vehicles: dict[str, Vehicle] = {}
        self.transport = Transport(state.initial.inventory, self.corridors, self.vehicles)
        self.hauls: dict[str, Haul] = {}
        self.vehicle_jobs: dict[str, str] = {}
        self.manual: set[str] = set()
        self.next_vehicle = 0
        self.shipment_jobs: dict[str, str] = {}
        self.decay_remainders = {}
        self.admitted_mU = {}
        self.admission_capacity_mU = {}
        self.terminals = {}
        self.contract_admitted = {}
        self.manual_admitted_mU = {}
        self.electric_grants = set()
        self.electric_power = {}
        self.terminal_admitted_mU = {}
        self.action_cycles = {}
        self.policy_tick = -1

    def terminal_remaining(self, eid):
        edge = self.corridors[eid]
        if edge.mode not in ('sea', 'air'):
            return self.capacity(eid)
        return min(max(0, self.capacity(eid) - self.terminal_admitted_mU.get((h, edge.mode), 0)) for h in (edge.a, edge.b))

    def record_terminal_entry(self, eid, weight):
        edge = self.corridors[eid]
        if edge.mode in ('sea', 'air'):
            for h in (edge.a, edge.b):
                key = h, edge.mode
                self.terminal_admitted_mU[key] = self.terminal_admitted_mU.get(key, 0) + weight

    def power_demands(self):
        return self.electric_power

    def reserve_electric(self, ctx):
        self.electric_power = {}
        for job in self.hauls.values():
            for vid in job.vehicle_ids:
                vehicle = self.vehicles.get(vid)
                if not vehicle or vehicle.propulsion != 'electric':
                    continue
                trip = self.transport.shipments.get(vehicle.assigned)
                if trip and trip.mode not in (TransitMode.COMPLETED, TransitMode.WAIT_UNLOAD, TransitMode.STRANDED):
                    eid = trip.path[max(0, min(trip.index, len(trip.path) - 1))]
                    distance = min(self.speed(vehicle, eid), self.corridors[eid].length_cells - trip.progress_cells)
                    hub = trip.current_hub
                elif trip is None and job.remaining_mU:
                    eid, hub = job.path[0], job.origin
                    distance = min(self.speed(vehicle, eid), self.corridors[eid].length_cells)
                else:
                    continue
                entity = f'electric_haul:{vid}'
                self.electric_power[entity] = (hub, 3, ceil(distance * 80))
                self.s.frame.work_context[entity] = (job.owner, hub, 'treasury')

    def advance_electric(self, ctx):
        for sid, trip in sorted(self.transport.shipments.items()):
            vehicle = self.vehicles[trip.vehicle_id]
            if vehicle.propulsion != 'electric' or trip.mode == TransitMode.COMPLETED:
                continue
            entity = f'electric_haul:{vehicle.id}'
            demand = self.electric_power.get(entity, ('', 3, 0))[2]
            delivered = self.s.frame.power_received.get(entity, 0)
            ratio = min(Q(1), Q(delivered, demand)) if demand else Q(0)
            eid = trip.path[max(0, min(trip.index, len(trip.path) - 1))]
            self.transport.advance(ctx.tick, sid, entry_granted=sid in self.electric_grants,
                free_capacity_mU=0, cargo_weight=self.s.cargo_weights[trip.good], defer_unload=True,
                speed_cells_tick=self.speed(vehicle, eid), fuel_mU_cell=Q(0), motion_fraction=ratio)
        self.unload(ctx)
        for sid, trip in tuple(self.transport.shipments.items()):
            if trip.mode == TransitMode.COMPLETED:
                del self.transport.shipments[sid]
                self.shipment_jobs.pop(sid, None)

    def transit_contract(self, owner, eid):
        contracts = getattr(self.s, 'contracts', None)
        return contracts.transit_right(owner, eid) if contracts and hasattr(contracts, 'transit_right') else None

    def city_transit_right(self, owner, eid, cycle=None):
        """Return a live city port lease, if one covers this physical edge.

        City leases are an additional ACL and toll payer.  They never add route
        capacity; the normal corridor admission still arbitrates the same
        physical edge and the same shipment weight.
        """
        economy = getattr(self.s, 'city_economy', None)
        relations = getattr(economy, 'relations', None)
        if relations is None:
            return None
        if cycle is None:
            cycle = getattr(self.s, 'completed_cycles', 0)
        return relations.transit_right(owner, eid, cycle)

    def terminal_online(self, hub, mode, owner=None):
        aid = self.terminals.get((hub, mode))
        asset = self.s.initial.assets.get(aid)
        return bool(asset and not asset.under_construction and asset.availability > 0 and
                    (owner is None or asset.owner == owner))

    def operator(self, eid):
        aid = self.road_assets.get(eid)
        rights = self.s.authority.assets.get(aid) if hasattr(self.s, 'authority') else None
        asset = self.s.initial.assets.get(aid)
        if rights:
            return rights.operator
        hub = self.s.initial.map.hubs[self.corridors[eid].a]
        return asset.owner if asset else self.s.authority.regions[hub.region].controller if hasattr(self.s, 'authority') else hub.owner

    def toll_base_ru_mU(self, cargo_mU):
        """06 corridor_policy fee base for one batch entering an edge.

        Sum_g (q_mU[g]/1000) * w_cat[g] * 10 RU, in RU milli-units:
        q_mU[g] * w_cat[g] * 10.  People use the food catalog value per head.
        """
        w = self.s.catalog.weights
        return sum((Q(q) * w['food' if g == 'people' else g] * 10 for g, q in cargo_mU.items()), Q(0))

    def entry_fee(self, owner, eid, weight, cargo_mU=None):
        right = self.city_transit_right(owner, eid)
        if right is not None:
            fee = self.s.city_economy.relations.entry_fee(owner, eid, weight, getattr(self.s, 'completed_cycles', 0))
            return fee[1] if fee is not None else 0
        edge = self.corridors[eid]
        recipient = self.operator(eid)
        if recipient is None or recipient == owner or not cargo_mU:
            return 0
        fee = ceil(self.toll_base_ru_mU(cargo_mU) * edge.toll_bp.get(owner, 0) / 10000)
        contract = self.transit_contract(owner, eid)
        if contract is not None:
            # 03 clause 3: the grantee never pays more than the agreed cap.
            fee = min(fee, contract.toll_cap_ru_mU)
        return ceil(Q(fee) / self.s.rates.get(owner, Q(1)))

    def pay_entry(self, owner, eid, weight, cargo_mU=None):
        right = self.city_transit_right(owner, eid)
        if right is not None:
            result = self.s.city_economy.relations.pay_entry(owner, eid, weight, getattr(self.s, 'completed_cycles', 0))
            return bool(result)
        amount = self.entry_fee(owner, eid, weight, cargo_mU)
        payer = self.s.native(owner, 'treasury')
        if self.s.initial.cash.balance(payer).native_mU < amount:
            return False
        if amount:
            self.s.initial.cash.transfer(payer, CashAccount(self.operator(eid), 'treasury', payer.currency), amount)
        return True

    def military_cargo(self, good):
        """Goods that need access tier 2 (military supplies) on a foreign edge."""
        controls = getattr(self.s, 'export_controls', None)
        return bool(controls and good in controls.war_goods and good not in CONTROLLED_GOODS)

    def permitted(self, owner, eid, good=None):
        edge = self.corridors[eid]
        if not edge.allowed(owner):
            return False
        city_right = self.city_transit_right(owner, eid)
        # An explicit right is required on every foreign-controlled endpoint:
        # tier 1 for commerce, tier 2 for military supplies (06 corridor_policy).
        if any((self.s.authority.regions[self.s.initial.map.hubs[h].region].controller if hasattr(self.s, 'authority') else self.s.initial.map.hubs[h].owner) not in (None, owner) for h in (edge.a, edge.b)):
            contract = self.transit_contract(owner, eid)
            need = 2 if good is not None and self.military_cargo(good) else 1
            if edge.tier(owner) < need and not contract and not city_right:
                return False
        return edge.mode not in ('sea', 'air') or all(self.terminal_online(h, edge.mode) for h in (edge.a, edge.b))

    def cargo_account(self, job):
        return Account(job.owner, job.origin, f'locked_haul:{job.id}', job.good)

    def fuel_account(self, job):
        return Account(job.owner, job.origin, f'locked_haul_fuel:{job.id}', 'fuel')

    def trip_fuel(self, vehicle, job):
        fuel = sum(self.corridors[e].length_cells * self.fuel_rate(vehicle, e) for e in job.path)
        return max(0, ceil(fuel * Q(3, 2) - vehicle.fuel_credit_mU))

    def fuel_rate(self, vehicle, eid):
        if vehicle.id in self.manual or vehicle.good == 'manual' or vehicle.propulsion == 'electric':
            return Q(0)
        mode = self.corridors[eid].mode
        return {'road': Q(60 if self.levels.get(eid, 1) == 1 else 40),
                'rail': Q(5), 'river': Q(3), 'sea': Q(7),
                'air': Q(35, 2)}.get(mode, Q(60))

    def speed(self, vehicle, eid):
        if vehicle.id in self.manual:
            return Q(4)
        mode = self.corridors[eid].mode
        known = self.s.research[vehicle.owner].known
        return {'road': Q(8 if self.levels.get(eid, 1) == 1 else 14),
                'rail': Q(36 if 'L11' in known else 20), 'river': Q(12), 'sea': Q(10),
                'air': Q(45)}.get(mode, Q(8))

    def available(self, vehicle):
        return vehicle.assigned is None and vehicle.health_mU > 0

    def labor_demands(self):
        for vid in sorted(self.manual):
            jid = self.vehicle_jobs.get(vid)
            if jid is not None:
                job = self.hauls[jid]
                if job.status != 'COMPLETED':
                    yield f'haul_labor:{vid}', job.owner, job.origin, 100

    def motion(self, vehicle):
        if vehicle.health_mU <= 0:
            return Q(0)
        if vehicle.id not in self.manual:
            return Q(1)
        entity = f'haul_labor:{vehicle.id}'
        workers = self.s.frame.labor.get(entity, {})
        # Labor was reserved alongside ordinary industry; it cannot be counted
        # again at another employer while the human carrier is away.
        return min(Q(1), Q(sum(workers.values()), 100))

    def capacity(self, eid):
        edge = self.corridors[eid]
        rated = edge.capacity_mU_cycle
        operator = self.operator(eid)
        known = self.s.research[operator].known if operator in self.s.research else set()
        if edge.mode == 'rail' and 'L09' in known:
            rated = 640000
        elif edge.mode == 'sea' and 'L10' in known:
            rated = 600000
        capacity = floor(Q(rated * edge.health_mU, 10000))
        for hub in (edge.a, edge.b):
            population = self.s.initial.populations.get(hub)
            if population and population.warning == 'GOV_CRISIS_PARALYSIS':
                capacity = floor(capacity * Q(2, 5))
                break
        return capacity

    def owner_capacity(self, owner, eid):
        cap = floor(self.capacity(eid) * (1 - self.corridors[eid].blocked.get(owner, Q(0))))
        contract = self.transit_contract(owner, eid)
        if contract:
            cap = min(cap, max(0, contract.maximum_flow_mU - self.contract_admitted.get((owner, eid), 0)))
        return cap

    def reserve(self, ctx):
        """P1: replenish only physically returned vehicles, before P2 loading."""
        inv = self.s.initial.inventory
        for job in sorted(self.hauls.values(), key=lambda j: digest(ctx.seed, 'haul_reserve', ctx.tick, j.id)):
            if job.status == 'COMPLETED' or job.cancelled:
                continue
            active = any(self.vehicles[v].assigned for v in job.vehicle_ids)
            if job.remaining_mU == 0 and not active:
                if not job.repeat_cycles:
                    self.finish(job)
                    continue
                if ctx.tick < job.next_departure_tick:
                    job.status = 'WAIT_REPEAT'
                    continue
                source = Account(job.owner, job.origin, 'available', job.good)
                if inv.balance(source) < job.quantity_mU:
                    job.status = 'WAIT_STOCK'
                    continue
                inv.transfer(source, self.cargo_account(job), job.quantity_mU)
                job.remaining_mU = job.quantity_mU
                job.next_departure_tick = ctx.tick + job.repeat_cycles * ctx.clock.cycle_ticks
            if not job.remaining_mU:
                continue
            wanted = sum(self.trip_fuel(self.vehicles[v], job) for v in job.vehicle_ids
                         if self.available(self.vehicles[v]))
            lock = self.fuel_account(job)
            need = max(0, wanted - inv.balance(lock))
            source = Account(job.owner, job.origin, 'available', 'fuel')
            taken = min(need, inv.balance(source))
            inv.transfer(source, lock, taken)
            job.status = 'ACTIVE' if inv.balance(lock) >= wanted else 'WAIT_FUEL'
        self.reserve_electric(ctx)

    def advance(self, ctx):
        inv, transport = self.s.initial.inventory, self.transport
        if ctx.tick % ctx.clock.cycle_ticks == 0:
            self.contract_admitted.clear()
        self.terminal_admitted_mU.clear()
        for eid, aid in self.road_assets.items():
            self.corridors[eid].health_mU = floor(self.s.initial.assets[aid].availability * 1000)
        remaining = {e: self.capacity(e) for e in self.corridors}
        manual_remaining = {e: min(800, cap) for e, cap in remaining.items()}
        self.admitted_mU = dict.fromkeys(remaining, 0)
        self.admission_capacity_mU = dict(remaining)
        self.manual_admitted_mU = dict.fromkeys(remaining, 0)
        # Existing loaded vehicles cannot shed cargo into thin air when a later
        # edge deteriorates. They wait intact; entry consumes this tick only.
        waiting = defaultdict(list)
        for shipment in transport.shipments.values():
            if shipment.mode == TransitMode.WAIT_ENTRY and ctx.tick > shipment.departure_tick:
                waiting[shipment.path[shipment.index]].append(shipment)
        grants = set()
        for eid, rows in sorted(waiting.items()):
            edge = self.corridors[eid]
            for shipment in sorted(rows, key=lambda s: (digest(ctx.seed, 'loaded_admission', ctx.tick, s.id), s.id)):
                vehicle = self.vehicles[shipment.vehicle_id]
                weight = ceil(shipment.quantity_mU * self.s.cargo_weights[shipment.good])
                cap = min(remaining[eid], manual_remaining[eid]) if vehicle.id in self.manual else remaining[eid]
                cap = min(cap, self.owner_capacity(shipment.owner, eid), self.terminal_remaining(eid))
                if weight > cap or not self.permitted(shipment.owner, eid, shipment.good) or not self.motion(vehicle):
                    continue
                if not self.pay_entry(shipment.owner, eid, weight, {shipment.good: shipment.quantity_mU}):
                    continue
                grants.add(shipment.id)
                remaining[eid] -= weight
                self.record_terminal_entry(eid, weight)
                if self.transit_contract(shipment.owner, eid):
                    key = shipment.owner, eid
                    self.contract_admitted[key] = self.contract_admitted.get(key, 0) + weight
                self.admitted_mU[eid] += weight
                if vehicle.id in self.manual:
                    manual_remaining[eid] -= weight
                    self.manual_admitted_mU[eid] += weight
        self.electric_grants = set(grants)
        # Start-of-edge cargo is still in a hub lock, so tails can be loaded
        # exactly to their proportional entitlement instead of blocking forever
        # behind a 15-unit truck on a 3-unit/tick road.
        candidates = defaultdict(dict)
        for job in self.hauls.values():
            if job.cancelled or not job.remaining_mU or job.status == 'COMPLETED':
                continue
            vehicles = [self.vehicles[v] for v in job.vehicle_ids if self.available(self.vehicles[v])]
            if not vehicles or not all(self.corridors[e].health_mU and
                    self.permitted(job.owner, e, job.good) for e in job.path):
                continue
            candidates[job.path[0]][job.id] = ceil(min(job.remaining_mU * self.s.cargo_weights[job.good],
                                                     sum(v.capacity_mU for v in vehicles)))
        for eid, demand in sorted(candidates.items()):
            manual_demand = {jid: q for jid, q in demand.items()
                             if self.hauls[jid].vehicle_ids[0] in self.manual}
            demand.update(proportional(manual_remaining[eid], manual_demand,
                          key=(ctx.seed, 'manual_entry', ctx.tick, eid)))
            allocations = proportional(remaining[eid], demand, key=(ctx.seed, 'haul_entry', ctx.tick, eid))
            for jid in sorted(demand, key=lambda j: digest(ctx.seed, 'haul_load', ctx.tick, eid, j)):
                job = self.hauls[jid]
                weight = self.s.cargo_weights[job.good]
                quantum = 1000 if self.s.catalog.goods[job.good]['indivisible'] else 1
                budget = allocations[jid]
                for vid in sorted(job.vehicle_ids, key=lambda v: digest(ctx.seed, 'haul_vehicle', ctx.tick, v)):
                    vehicle = self.vehicles[vid]
                    if not self.available(vehicle) or not self.motion(vehicle):
                        continue
                    cap = min(budget, vehicle.capacity_mU, self.terminal_remaining(eid), *(self.owner_capacity(job.owner, e) for e in job.path))
                    if vid in self.manual:
                        cap = min(cap, manual_remaining[eid])
                    quantity = min(job.remaining_mU, floor(Q(cap) / weight))
                    quantity -= quantity % quantum
                    if not quantity:
                        continue
                    if inv.balance(self.fuel_account(job)) < self.trip_fuel(vehicle, job):
                        job.status = 'WAIT_FUEL'
                        continue
                    if self.active_count(job.owner) >= self.s.catalog.runtime['limits']['shipments_per_nation']:
                        job.status = 'WAIT_CAPACITY'
                        break
                    if not self.pay_entry(job.owner, eid, ceil(quantity * weight), {job.good: quantity}):
                        job.status = 'WAIT_FEE'
                        continue
                    sid = f'{job.id}/trip{job.serial}'
                    # The loading tick is an admission tick. Motion starts next
                    # tick, so a new command never teleports an arrival in P1.
                    shipment = transport.launch(sid, job.owner, vid, job.good, quantity,
                        job.origin, job.destination, job.path, ctx.tick, cargo_weight=weight,
                        indivisible=quantum == 1000, cargo_source=self.cargo_account(job),
                        fuel_source=self.fuel_account(job), fuel_reserved_mU=self.trip_fuel(vehicle, job))
                    shipment.mode, shipment.entered_tick = TransitMode.IN_TRANSIT, ctx.tick
                    vehicle.hub = None
                    self.shipment_jobs[sid] = job.id
                    job.serial += 1
                    job.remaining_mU -= quantity
                    used = ceil(quantity * weight)
                    budget -= used
                    remaining[eid] -= used
                    self.record_terminal_entry(eid, used)
                    if self.transit_contract(job.owner, eid):
                        key = job.owner, eid
                        self.contract_admitted[key] = self.contract_admitted.get(key, 0) + used
                    self.admitted_mU[eid] += used
                    if vid in self.manual:
                        manual_remaining[eid] -= used
                        self.manual_admitted_mU[eid] += used
        for sid, shipment in sorted(transport.shipments.items()):
            if shipment.mode == TransitMode.COMPLETED:
                continue
            vehicle = self.vehicles[shipment.vehicle_id]
            if vehicle.propulsion == 'electric':
                continue
            transport.advance(ctx.tick, sid, entry_granted=sid in grants, free_capacity_mU=0,
                              cargo_weight=self.s.cargo_weights[shipment.good],
                              motion_fraction=self.motion(vehicle), defer_unload=True,
                              speed_cells_tick=self.speed(vehicle, shipment.path[min(shipment.index, len(shipment.path) - 1)]),
                              fuel_mU_cell=self.fuel_rate(vehicle, shipment.path[min(shipment.index, len(shipment.path) - 1)]))
        self.promote_outdoor(ctx)
        self.unload(ctx)
        if ctx.clock.is_cycle_end(ctx.tick):
            for sid in sorted(transport.shipments):
                transport.decay(sid)
            self.decay_outdoor()
        # Completed trips have no physical cargo. Remove them after clearing
        # assignment; sequence counters keep identities unique without a
        # whole-match archive in every observation/state snapshot.
        for sid, shipment in tuple(transport.shipments.items()):
            if shipment.mode == TransitMode.COMPLETED:
                del transport.shipments[sid]
                del self.shipment_jobs[sid]
        for job in self.hauls.values():
            if job.cancelled and job.status != 'COMPLETED' and all(self.vehicles[v].assigned is None for v in job.vehicle_ids):
                self.finish(job)

    def promote_outdoor(self, ctx):
        inv = self.s.initial.inventory
        occupied = self.s.warehouse_load()
        by_hub = defaultdict(dict)
        accounts = {}
        for account, amount in inv.balances().items():
            if account.bucket == 'outdoor':
                key = f'{account.owner}/{account.asset}'
                by_hub[account.location][key] = ceil(amount * self.s.cargo_weights[account.asset])
                accounts[account.location, key] = account
        for hub, demand in sorted(by_hub.items()):
            free = max(0, floor(self.s.initial.warehouse_mU.get(hub, 0) - occupied.get(hub, 0)))
            grants = proportional(free, demand, key=(ctx.seed, 'outdoor_promote', ctx.tick, hub))
            for key, grant in sorted(grants.items()):
                source = accounts[hub, key]
                quantum = 1000 if self.s.catalog.goods[source.asset]['indivisible'] else 1
                amount = min(inv.balance(source), floor(Q(grant) / self.s.cargo_weights[source.asset]))
                amount -= amount % quantum
                inv.transfer(source, Account(source.owner, hub, 'available', source.asset), amount)

    def decay_outdoor(self):
        inv = self.s.initial.inventory
        present = set()
        for account, quantity in sorted(inv.balances().items()):
            if account.bucket != 'outdoor':
                continue
            key = f'{account.owner}/{account.location}/{account.asset}'
            present.add(key)
            quantum = 1000 if self.s.catalog.goods[account.asset]['indivisible'] else 1
            exact = self.decay_remainders.get(key, Q(0)) + Q(quantity, 20)
            lost = min(quantity, floor(exact / quantum) * quantum)
            self.decay_remainders[key] = exact - lost if lost < quantity else Q(0)
            inv.transform({account: lost}, {}, reason='outdoor_storage_decay', use=Use.DESTRUCTION)
        self.decay_remainders = {k: v for k, v in self.decay_remainders.items() if k in present and v}

    def unload(self, ctx):
        inv = self.s.initial.inventory
        occupied = self.s.warehouse_load()
        outdoor = defaultdict(lambda: Q(0))
        for account, quantity in inv.balances().items():
            if account.bucket == 'outdoor':
                outdoor[account.location] += quantity * self.s.cargo_weights[account.asset]
        arrivals = defaultdict(list)
        for shipment in self.transport.shipments.values():
            if shipment.mode == TransitMode.WAIT_UNLOAD:
                arrivals[shipment.destination].append(shipment)
        for hub, rows in sorted(arrivals.items()):
            capacity = self.s.initial.warehouse_mU.get(hub, 0)
            for bucket, space in (('available', Q(capacity) - occupied.get(hub, 0)),
                                  ('outdoor', Q(capacity, 5) - outdoor[hub])):
                free = max(0, floor(space))
                # Essential goods first; within a band all arrivals share one
                # frozen warehouse pool, including foreign-owned inventory.
                for essential in (True, False):
                    band = [s for s in rows if s.mode == TransitMode.WAIT_UNLOAD and
                            (s.good in ('food', 'grain', 'water', 'medicine', 'medical_supplies')) == essential and
                            (bucket == 'available' or self.hauls[self.shipment_jobs[s.id]].delivery_bucket == 'available')]
                    requests = {s.id: ceil(s.quantity_mU * self.s.cargo_weights[s.good]) for s in band}
                    grants = proportional(free, requests, key=(ctx.seed, 'haul_unload', ctx.tick, hub, bucket, int(essential)))
                    for shipment in sorted(band, key=lambda s: s.id):
                        before = shipment.quantity_mU
                        self.transport.unload(shipment.id, grants[shipment.id],
                                              cargo_weight=self.s.cargo_weights[shipment.good], bucket=bucket,
                                              destination_account=(Account(shipment.owner, shipment.destination,
                                                  self.hauls[self.shipment_jobs[shipment.id]].delivery_bucket, shipment.good)
                                                  if bucket == 'available' and self.hauls[self.shipment_jobs[shipment.id]].delivery_bucket != 'available' else None))
                        free -= ceil((before - shipment.quantity_mU) * self.s.cargo_weights[shipment.good])

    def active_count(self, owner):
        return sum(s.owner == owner and s.mode != TransitMode.COMPLETED for s in self.transport.shipments.values())

    def finish(self, job):
        inv = self.s.initial.inventory
        for lock in (self.cargo_account(job), self.fuel_account(job)):
            inv.transfer(lock, Account(job.owner, job.origin, 'available', lock.asset), inv.balance(lock))
        job.remaining_mU = 0
        job.status = 'COMPLETED'
        for vid in job.vehicle_ids:
            self.vehicle_jobs.pop(vid, None)
            if vid in self.manual:
                self.manual.remove(vid)
                del self.vehicles[vid]
        self.s.command_streams[job.owner].plans.pop(job.command_id, None)

    def cancel(self, owner, command_id):
        job = next((j for j in self.hauls.values() if j.owner == owner and j.command_id == command_id
                    and j.status != 'COMPLETED'), None)
        if job is None:
            return None
        def commit():
            job.cancelled = True
            job.repeat_cycles = 0
            inv = self.s.initial.inventory
            lock = self.cargo_account(job)
            inv.transfer(lock, Account(owner, job.origin, 'available', job.good), inv.balance(lock))
            job.remaining_mU = 0
            fuel = self.fuel_account(job)
            inv.transfer(fuel, Account(owner, job.origin, 'available', 'fuel'), inv.balance(fuel))
            if all(self.vehicles[v].assigned is None for v in job.vehicle_ids):
                self.finish(job)
        return PreparedCommand(commit)

    def record_policy_events(self, ctx):
        """07: one minimum action-loss event per (operator, restricted target) per tick."""
        if ctx.tick <= self.policy_tick:
            raise InvariantViolation('corridor policy tick repeated')
        self.policy_tick = ctx.tick
        targets = defaultdict(set)
        for eid, edge in sorted(self.corridors.items()):
            if not edge.restricted:
                continue
            operator = self.operator(eid)
            for target, tier in edge.restricted.items():
                if operator not in (None, target) and edge.tier(target) < tier:
                    targets[operator].add(target)
        # power_acl_v1: a refused foreign source on a controlled power line is a
        # standing restriction too (lines are open to every source by default).
        utilities = getattr(self.s, 'utilities', None)
        for link in sorted(utilities.links.values(), key=lambda l: l.id) if utilities is not None else ():
            if link.mode == 'power' and link.denied:
                operator = utilities.operator(link)
                targets[operator].update(t for t in link.denied if t != operator)
        row = self.action_cycles.setdefault(ctx.cycle, {})
        for owner, others in targets.items():
            row[owner] = row.get(owner, Q(0)) + Q(len(others), 1000)
        self.action_cycles = {c: v for c, v in self.action_cycles.items() if c >= ctx.cycle - 19}

    def snapshot(self):
        return {'policy_tick': self.policy_tick,
                'action_cycles': {str(c): v for c, v in sorted(self.action_cycles.items())},
                'corridors': self.corridors, 'levels': self.levels, 'paths': self.paths,
                'road_assets': self.road_assets, 'vehicles': self.vehicles, 'hauls': self.hauls,
                'shipments': self.transport.shipments, 'shipment_jobs': self.shipment_jobs,
                'vehicle_jobs': self.vehicle_jobs, 'manual': self.manual, 'next_vehicle': self.next_vehicle,
                'decay_remainders': self.decay_remainders, 'admitted_mU': self.admitted_mU,
                'admission_capacity_mU': self.admission_capacity_mU,
                'terminals': [[hub, mode, aid] for (hub, mode), aid in sorted(self.terminals.items())],
                'contract_admitted': [[owner, eid, q] for (owner, eid), q in sorted(self.contract_admitted.items())],
                'manual_admitted_mU': self.manual_admitted_mU,
                'electric_grants': self.electric_grants, 'electric_power': self.electric_power,
                'terminal_admitted_mU': [[h, mode, q] for (h, mode), q in sorted(self.terminal_admitted_mU.items())]}

    def visible(self, owner):
        # Ratios are explicitly represented as pairs for the wire schema.
        return {'roads': [{'id': e.id, 'origin_hub': e.a, 'dest_hub': e.b, 'level': self.levels[e.id], 'mode': e.mode,
                           'health_mU': e.health_mU, 'capacity_mU_cycle': e.capacity_mU_cycle,
                           **({'my_access_tier': e.access_tier[owner], 'my_toll_rate_bp': e.toll_bp.get(owner, 0)}
                              if owner in e.access_tier else {}),
                           **({'policies': [{'target_nation': t, 'access_tier': a, 'toll_rate_bp': e.toll_bp.get(t, 0)}
                                            for t, a in sorted(e.access_tier.items())]} if self.operator(e.id) == owner else {})}
                          for e in self.corridors.values()
                          if self.s.initial.map.hubs[e.a].owner == owner or self.s.initial.map.hubs[e.b].owner == owner],
                'hauls': [asdict(j) for j in self.hauls.values() if j.owner == owner],
                'shipments': [{'id': s.id, 'haul_id': self.shipment_jobs[s.id], 'good': s.good,
                    'qty_mU': s.quantity_mU, 'hub_id': s.current_hub, 'mode': s.mode.value,
                    'vehicle_id': s.vehicle_id, 'returning': s.returning,
                    'progress_cells': [s.progress_cells.numerator, s.progress_cells.denominator]}
                    for s in self.transport.shipments.values() if s.owner == owner],
                'vehicles': [{'id': v.id, 'good': v.good, 'hub_id': v.hub,
                    'health_mU': v.health_mU, 'shipment_id': v.assigned}
                    for v in self.vehicles.values() if v.owner == owner]}

    def audit(self):
        self.transport.audit()
        inv = self.s.initial.inventory
        for job in self.hauls.values():
            if inv.balance(self.cargo_account(job)) != job.remaining_mU:
                raise InvariantViolation('haul queue differs from locked cargo')
        physical = set()
        for vid, vehicle in self.vehicles.items():
            if vehicle.physical_account is not None:
                if vehicle.physical_account in physical:
                    raise InvariantViolation('same physical vehicle deployed twice')
                physical.add(vehicle.physical_account)
            if vid not in self.manual and (vehicle.physical_account is None or inv.balance(vehicle.physical_account) != 1000):
                raise InvariantViolation('deployed vehicle lacks exclusive physical inventory')
        for eid, amount in self.admitted_mU.items():
            if not 0 <= amount <= self.admission_capacity_mU[eid]:
                raise InvariantViolation('corridor admission exceeded tick capacity')


class TransportCommands:
    OPERATIONS = frozenset({('logistics', 'route_shipment')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx, *, source_bucket='available', delivery_bucket='available', parent_id=None):
        if parent_id is None and hasattr(self.state, 'intermodal') and (order.get('carrier_recipe') in ('auto', 'intermodal') or
                len(order.get('corridor_path', ())) > 1):
            return self.state.intermodal.prepare(owner, order, ctx)
        fields(order, {'origin_hub': str, 'dest_hub': str, 'corridor_path': list, 'good': str,
                       'qty_mU': int, 'carrier_recipe': str}, {'repeat_cycles': int, 'propulsion': str})
        state, ops = self.state, self.state.cargo
        origin, destination, good = order['origin_hub'], order['dest_hub'], order['good']
        path = tuple(order['corridor_path'])
        row = state.catalog.goods.get(good)
        if (origin == destination or origin not in state.initial.map.hubs or destination not in state.initial.map.hubs
                or not path or len(path) > 128 or any(type(e) is not str for e in path)
                or row is None or row['kind'] != 'inventory' or order['qty_mU'] <= 0
                or not 0 <= order.get('repeat_cycles', 0) <= 100):
            raise RuleViolation('ERR_SCHEMA')
        if row['indivisible'] and order['qty_mU'] % 1000:
            raise RuleViolation('ERR_SCHEMA')
        carrier = order['carrier_recipe']
        carriers = {'manual': ('manual', 'road', 800), 'P036': ('vehicle', 'road', 15000),
                    'P037': ('locomotive', 'rail', 80000), 'P052': ('freighter', 'sea', 200000),
                    'river_freighter': ('freighter', 'river', 120000),
                    'P053': ('transport_aircraft', 'air', 20000)}
        if carrier not in carriers:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'carrier network not installed')
        carrier_good, mode, cap = carriers[carrier]
        propulsion = order.get('propulsion', 'fuel')
        if propulsion not in ('fuel', 'electric') or propulsion == 'electric' and mode != 'rail':
            raise RuleViolation('ERR_SCHEMA')
        if carrier == 'P052' and all(e in ops.corridors and ops.corridors[e].mode == 'river' for e in path):
            mode, cap = 'river', 120000
        if parent_id is None and not state.authority.controls_hub(owner, origin):
            raise RuleViolation('ERR_NO_PERMISSION', 'fleet must be based at a controlled hub')
        node = origin
        for eid in path:
            edge = ops.corridors.get(eid)
            if edge is None or not edge.health_mU or not ops.permitted(owner, eid, good):
                raise RuleViolation('ERR_CORRIDOR_BLOCKED')
            if edge.mode != mode:
                raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'vehicle incompatible with corridor mode')
            node = edge.other(node)
        if node != destination:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        inv = state.initial.inventory
        if carrier == 'P036':
            cap = min(15000 if ops.levels[e] == 1 else 18000 for e in path)
        quantum = 1000 if row['indivisible'] else 1
        if quantum * state.cargo_weights[good] > min(cap, *(ops.capacity(e) for e in path)):
            raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'one indivisible cargo exceeds route tick admission')
        desired = ceil(order['qty_mU'] * state.cargo_weights[good] / cap)
        if desired > state.catalog.runtime['limits']['shipments_per_nation']:
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        idle = sorted((v for v in ops.vehicles.values() if v.owner == owner and v.hub == origin
                       and v.id not in ops.vehicle_jobs and ops.available(v) and v.good == carrier_good and v.propulsion == propulsion),
                      key=lambda v: (digest(ctx.seed, 'fleet_choice', ctx.tick, v.id), v.id)) if carrier != 'manual' else []
        vehicles = idle[:desired]
        created = []
        for i in range(desired - len(vehicles)):
            vid = f'fleet:{owner}:{ops.next_vehicle + i}'
            account = None if carrier == 'manual' else Account(owner, vid, 'deployed_vehicle', carrier_good)
            vehicle = Vehicle(vid, owner, origin, carrier_good, cap,
                              Q(4 if carrier == 'manual' else 8), Q(0 if carrier == 'manual' else 60),
                              physical_account=account, propulsion=propulsion)
            created.append(vehicle)
        vehicles += created
        cid = str(int(order['id']))
        job = Haul(f'haul:{owner}:{cid}', owner, cid, origin, destination, path, good, order['qty_mU'],
                   order['qty_mU'], tuple(v.id for v in vehicles), order.get('repeat_cycles', 0),
                   ctx.tick + order.get('repeat_cycles', 0) * ctx.clock.cycle_ticks)
        job.delivery_bucket, job.parent_id = delivery_bucket, parent_id
        transfers = [Transfer(Account(owner, origin, source_bucket, good), ops.cargo_account(job), job.quantity_mU),
                     Transfer(Account(owner, origin, 'available', 'fuel'), ops.fuel_account(job),
                              sum(ops.trip_fuel(v, job) for v in vehicles))]
        transfers.extend(Transfer(Account(owner, origin, 'available', carrier_good), v.physical_account, 1000,
                                  destination_hub=origin) for v in created if v.physical_account)
        if sum(j.owner == owner and j.status != 'COMPLETED' for j in ops.hauls.values()) >= state.catalog.runtime['limits']['shipments_per_nation']:
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        stock = inv.prepare_transfers(transfers)
        def commit():
            stock.commit()
            ops.hauls = {jid: j for jid, j in ops.hauls.items() if j.status != 'COMPLETED'}
            ops.hauls[job.id] = job
            ops.next_vehicle += len(created)
            for vehicle in vehicles:
                vehicle.capacity_mU = cap
                ops.vehicles[vehicle.id] = vehicle
                ops.vehicle_jobs[vehicle.id] = job.id
                if carrier == 'manual':
                    ops.manual.add(vehicle.id)
        return PreparedCommand(commit, persistent=bool(job.repeat_cycles))


class CorridorPolicyCommands:
    OPERATIONS = frozenset({('logistics', 'corridor_policy')})

    def __init__(self, state):
        self.s = state

    def _power_line(self, owner, link, target, tier):
        """power_acl_v1 (03 section 7 item 3): the controller of a power line may refuse
        a foreign nation's generation on it (tier 0) or admit it again (tier >= 1).
        Tolls stay the dispatch's catalogue toll; toll_rate_bp is ignored here."""
        if self.s.utilities.operator(link) != owner:
            raise RuleViolation('ERR_NO_PERMISSION')
        if target not in self.s.initial.diplomacy.actors or target == owner:
            raise RuleViolation('ERR_SCHEMA')
        def commit():
            denied = set(link.denied)
            (denied.add if tier == 0 else denied.discard)(target)
            link.denied = tuple(sorted(denied))
        return PreparedCommand(commit)

    def prepare(self, owner, order, ctx):
        fields(order, {'corridor_id': str, 'target_nation': str, 'access_tier': int, 'toll_rate_bp': int})
        eid, target = order['corridor_id'], order['target_nation']
        tier, bp = order['access_tier'], order['toll_rate_bp']
        if not 0 <= tier <= 3 or not 0 <= bp <= 2000:
            raise RuleViolation('ERR_SCHEMA')
        link = self.s.utilities.links.get(eid) if hasattr(self.s, 'utilities') else None
        if link is not None and link.mode == 'power' and eid not in self.s.cargo.corridors:
            return self._power_line(owner, link, target, tier)
        if eid not in self.s.cargo.corridors:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if self.s.cargo.operator(eid) != owner:
            raise RuleViolation('ERR_NO_PERMISSION')
        if target not in self.s.initial.diplomacy.actors:
            raise RuleViolation('ERR_SCHEMA')
        edge = self.s.cargo.corridors[eid]
        def commit():
            before = edge.tier(target)
            # 03 section 7 item 1: tightening a foreign nation's existing access is
            # a hostile policy; remember the highest tier it held so the
            # restriction keeps accruing 07 events until it is restored.
            if target != owner and tier < before:
                edge.restricted[target] = max(before, edge.restricted.get(target, 0))
            if tier >= edge.restricted.get(target, 0):
                edge.restricted.pop(target, None)
            edge.access_tier[target] = tier
            edge.toll_bp[target] = bp
        return PreparedCommand(commit)

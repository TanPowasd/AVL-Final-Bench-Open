"""Passenger custody on finite physical vehicles, fuel and real returns.

Passenger stock never enters the goods ledger.  Provisions, savings and the
integer truck do; population belongs to exactly one residence or shipment.
"""
from dataclasses import dataclass
from collections import defaultdict
from heapq import heappop, heappush

from .command_schema import fields
from .determinism import digest
from .errors import InvariantViolation, RuleViolation
from .ledger import Account, CashAccount, CashTransfer, DeliveryPayment, Transfer
from .logistics import Shipment, Transport, TransitMode, Vehicle
from .numeric import Q, ceil, floor, proportional
from .population import SKILLS
from .protocol import PreparedCommand
from .provenance import Use


@dataclass
class PassengerShipment:
    id: str
    owner: str
    origin: str
    destination: str
    skills_mU: dict[str, int]
    housing_reserved_mU: int
    initial_people_mU: int
    delivered: bool = False
    provision_ticks: int = 0
    shortage_ticks: int = 0


class PassengerOperations:
    def __init__(self, state):
        self.s = state
        self.shipments = {}
        self.vehicles = {}
        self.transport = Transport(state.initial.inventory, state.cargo.corridors, self.vehicles)
        self.serial = 0
        self.housing_reserved = {}
        self.pending = {}
        self.manual = set()
        self.dispatching = False

    def controls(self, owner, hub):
        return self.s.authority.controls_hub(owner, hub)

    def carrier(self, owner, origin, mode):
        good, cap, speed, fuel = {'road': ('vehicle', 15000, 8, Q(60)),
            'rail': ('locomotive', 80000, 20, Q(5)), 'river': ('freighter', 120000, 12, Q(3)),
            'sea': ('freighter', 200000, 10, Q(7)), 'air': ('transport_aircraft', 20000, 45, Q(35, 2))}[mode]
        idle = next((v for v in sorted(self.vehicles.values(), key=lambda v: v.id) if v.owner == owner and v.hub == origin and
                     v.assigned is None and v.good == good), None)
        physical = idle or self.s.initial.inventory.balance(Account(owner, origin, 'available', good)) >= 1000
        if not physical:
            if mode != 'road':
                raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'physical passenger carrier unavailable')
            return None, 'manual', 800, 4, Q(0)
        return idle, good, cap, speed, fuel

    def shortest(self, owner, origin, destination, mode='road'):
        graph = {}
        for eid, e in self.s.cargo.corridors.items():
            if e.mode == mode and e.health_mU and self.s.cargo.permitted(owner, eid):
                graph.setdefault(e.a, []).append((e.b, eid, e.length_cells))
                graph.setdefault(e.b, []).append((e.a, eid, e.length_cells))
        queue = [(0, b'', origin, ())]; visited = set()
        while queue:
            distance, _, hub, path = heappop(queue)
            if hub in visited:
                continue
            visited.add(hub)
            if hub == destination:
                return path
            for node, eid, length in graph.get(hub, ()):
                if node not in visited:
                    heappush(queue, (distance + length, digest(self.s.initial.map.seed, 'passenger_path', origin, destination, *(path + (eid,))), node, path + (eid,)))
        raise RuleViolation('ERR_CORRIDOR_BLOCKED')

    def labor_demands(self):
        for vid in sorted(self.manual):
            vehicle = self.vehicles[vid]
            if vehicle.assigned:
                record = self.shipments[vehicle.assigned]
                yield f'passenger_labor:{vid}', record.owner, record.origin, 100

    def prepare(self, owner, order, ctx, *, passive=False):
        fields(order, {'origin_hub': str, 'dest_hub': str, 'skills_mU': dict, 'path': list}, {'allow_unhoused': bool})
        origin, dest, skills = order['origin_hub'], order['dest_hub'], order['skills_mU']
        pops = self.s.initial.populations
        if origin == dest or origin not in pops or dest not in pops:
            raise RuleViolation('ERR_SCHEMA')
        if not self.controls(owner, origin) or not self.controls(owner, dest):
            raise RuleViolation('ERR_NO_PERMISSION')
        if set(skills) != set(SKILLS) or any(type(q) is not int or q < 0 for q in skills.values()) or not sum(skills.values()):
            raise RuleViolation('ERR_SCHEMA')
        if any(skills[k] > pops[origin].skills_mU.get(k, 0) for k in SKILLS):
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        if self.dispatching:
            participation = self.s.labor_participation(owner, origin)
            for skill in SKILLS:
                committed = sum(workers.get(skill, 0) for entity, workers in self.s.frame.labor.items()
                                if self.s.frame.work_context.get(entity, (None, None, None))[1] == origin)
                committed += ceil(self.s.ai_deployment.held_at(origin, skill))
                if floor(Q(pops[origin].skills_mU.get(skill, 0) - skills[skill], ctx.clock.cycle_ticks) * participation) < committed:
                    raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'residents already working this tick')
        people = sum(skills.values())
        path = tuple(order['path'])
        if not path or len(path) > 128 or any(type(e) is not str for e in path):
            raise RuleViolation('ERR_SCHEMA')
        mode = self.s.cargo.corridors[path[0]].mode if path[0] in self.s.cargo.corridors else 'road'
        shortest = self.shortest(owner, origin, dest, mode)
        if sum(self.s.cargo.corridors[e].length_cells for e in path if e in self.s.cargo.corridors) != sum(self.s.cargo.corridors[e].length_cells for e in shortest):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'passengers require shortest legal path')
        node = origin
        for eid in path:
            e = self.s.cargo.corridors.get(eid)
            if e is None or e.mode != mode or not self.s.cargo.permitted(owner, eid) or not e.health_mU:
                raise RuleViolation('ERR_CORRIDOR_BLOCKED')
            node = e.other(node)
        if node != dest:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        allow = order.get('allow_unhoused', False)
        free = max(0, self.s.initial.housing_mU.get(dest, 0) - pops[dest].total_mU - self.housing_reserved.get(dest, 0))
        if not free and not allow:
            raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'destination housing unavailable')
        if allow and self.s.initial.map.hubs[dest].core_index is None:
            raise RuleViolation('ERR_NO_PERMISSION')
        if not self.dispatching:
            key = f'{owner}:{order["id"]}'
            if key in self.pending:
                raise RuleViolation('ERR_NO_PERMISSION')
            frozen = {**order, 'skills_mU': dict(skills), 'path': list(path)}
            if sum(p == owner for p, _, _ in self.pending.values()) >= self.s.catalog.runtime['limits']['shipments_per_nation']:
                raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
            return PreparedCommand(lambda: self.pending.__setitem__(key, (owner, frozen, passive)), persistent=True)
        sid = f'passenger:{owner}:{self.serial}'
        vehicle, carrier_good, cap, speed, fuel_rate = self.carrier(owner, origin, mode)
        use_manual = carrier_good == 'manual'
        created = vehicle is None
        if created:
            vid = sid + ':vehicle'
            vehicle = Vehicle(vid, owner, origin, carrier_good, cap, Q(speed), fuel_rate,
                              physical_account=None if use_manual else Account(owner, vid, 'deployed_vehicle', carrier_good))
        length = sum(self.s.cargo.corridors[e].length_cells for e in path)
        fuel = max(0, ceil(length * vehicle.fuel_mU_cell * Q(3, 2) - vehicle.fuel_credit_mU))
        if ceil(people * Q(3, 2)) > vehicle.capacity_mU:
            raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'passengers and provisions exceed vehicle payload')
        trip = Shipment(sid, owner, vehicle.id, 'passenger', 0, origin, dest, path,
                        ctx.tick, fuel, origin, initial_fuel_credit_mU=vehicle.fuel_credit_mU,
                        auxiliary_accounts=tuple(Account(owner, sid, 'passenger_provisions', g) for g in ('food', 'water')))
        record = PassengerShipment(sid, owner, origin, dest, dict(skills), 0 if allow else people, people)
        inv, cash = self.s.initial.inventory, self.s.initial.cash
        supplies = [Transfer(Account(owner, origin, 'available', g), Account(owner, sid, 'passenger_provisions', g), ceil(people * q), destination_hub=origin)
                    for g, q in (('food', Q(3, 10)), ('water', Q(1, 5)))]
        supplies.append(Transfer(Account(owner, origin, 'available', 'fuel'), Account(owner, sid, 'fuel_tank', 'fuel'), fuel, destination_hub=origin))
        if created and not use_manual:
            supplies.append(Transfer(Account(owner, origin, 'available', carrier_good), vehicle.physical_account, 1000, destination_hub=origin))
        household = self.s.native(owner, f'household:{origin}')
        money = [CashTransfer(self.s.native(owner, 'treasury'), self.s.native(owner, f'public:{origin}'), 0 if passive else people * 10)]
        # Move each currency's proportional savings without converting it.
        for account, balance in cash.balances().items():
            if account.owner == owner and account.name == household.name:
                money.append(CashTransfer(account, CashAccount(owner, sid, account.currency), floor(Q(balance.native_mU * people, pops[origin].total_mU))))
        transaction = DeliveryPayment.prepare(inv, cash, supplies, money)
        def commit():
            transaction.commit()
            pops[origin].reserve_people(skills)
            self.housing_reserved[dest] = self.housing_reserved.get(dest, 0) + record.housing_reserved_mU
            self.shipments[sid] = record
            self.vehicles[vehicle.id] = vehicle
            if use_manual:
                self.manual.add(vehicle.id)
            self.transport.shipments[sid] = trip
            vehicle.assigned = sid
            self.serial += 1
        return PreparedCommand(commit)

    def reserve(self, ctx):
        """P1: freeze every request before sharing people, housing and vehicles."""
        if not self.pending:
            return
        requests = dict(self.pending)
        amount = {k: sum(o['skills_mU'].values()) for k, (_, o, _) in requests.items()}
        # A resident can be requested by several plans, but can leave only once.
        source_grants = {}
        by_source = defaultdict(dict)
        for k, (_, o, _) in requests.items():
            for skill, q in o['skills_mU'].items():
                by_source[o['origin_hub'], skill][k] = q
        for (hub, skill), demands in by_source.items():
            source_grants[hub, skill] = proportional(self.s.initial.populations[hub].skills_mU.get(skill, 0), demands,
                key=(ctx.seed, 'passenger_source', ctx.tick, hub, skill))
        for k, (_, o, _) in requests.items():
            amount[k] = sum(source_grants[o['origin_hub'], s][k] for s in SKILLS)
        by_dest = defaultdict(dict)
        for k, (_, o, _) in requests.items():
            if not o.get('allow_unhoused', False):
                by_dest[o['dest_hub']][k] = amount[k]
        for hub, demand in by_dest.items():
            free = max(0, self.s.initial.housing_mU.get(hub, 0) - self.s.initial.populations[hub].total_mU - self.housing_reserved.get(hub, 0))
            amount.update(proportional(free, demand, key=(ctx.seed, 'passenger_housing', ctx.tick, hub)))
        by_edge = defaultdict(dict)
        for k, (owner, o, _) in requests.items():
            inv = self.s.initial.inventory
            mode = self.s.cargo.corridors[o['path'][0]].mode
            try:
                _, _, capacity, _, _ = self.carrier(owner, o['origin_hub'], mode)
            except RuleViolation:
                capacity = 0
            limit = floor(capacity * Q(2, 3))
            amount[k] = min(amount[k], limit)
            for edge in o['path']:
                by_edge[edge][k] = ceil(amount[k] * Q(3, 2))
        for edge, demand in by_edge.items():
            allocations = proportional(self.s.cargo.capacity(edge), demand, key=(ctx.seed, 'passenger_corridor', ctx.tick, edge))
            for k in demand:
                amount[k] = min(amount[k], floor(allocations[k] * Q(2, 3)))
        # Each request emits at most one real vehicle batch per tick, then the
        # unsent remainder stays in the origin skill pools and in the plan.
        for k in sorted(requests, key=lambda k: digest(ctx.seed, 'passenger_departure', ctx.tick, k)):
            owner, order, passive = requests[k]
            q = amount[k]
            # Another simultaneous plan may have taken the last integer truck;
            # only the real remaining fleet decides this batch's payload.
            origin = order['origin_hub']
            try:
                _, _, capacity, _, _ = self.carrier(owner, origin, self.s.cargo.corridors[order['path'][0]].mode)
            except RuleViolation:
                continue
            q = min(q, floor(capacity * Q(2, 3)))
            if not q:
                continue
            if sum(r.owner == owner for r in self.shipments.values()) >= self.s.catalog.runtime['limits']['shipments_per_nation']:
                continue
            mix = proportional(q, {s: source_grants[order['origin_hub'], s][k] for s in SKILLS},
                               key=(ctx.seed, 'passenger_batch', ctx.tick, k))
            self.dispatching = True
            try:
                self.prepare(owner, {**order, 'skills_mU': mix}, ctx, passive=passive).commit()
            except RuleViolation:
                continue
            finally:
                self.dispatching = False
            remaining = {s: order['skills_mU'][s] - mix[s] for s in SKILLS}
            if sum(remaining.values()):
                self.pending[k] = (owner, {**order, 'skills_mU': remaining}, passive)
            else:
                del self.pending[k]
                self.s.command_streams[owner].plans.pop(str(order['id']), None)

    def cancel(self, owner, command_id):
        key = f'{owner}:{command_id}'
        if key not in self.pending:
            return None
        # Only future batches are cancelled. People already in transit retain
        # their vehicle, food, savings and reserved destination housing.
        return PreparedCommand(lambda: self.pending.pop(key, None))

    def advance(self, ctx):
        inv = self.s.initial.inventory
        for sid in sorted(tuple(self.shipments), key=lambda s: digest(ctx.seed, 'passenger_entry', ctx.tick, s)):
            record, trip = self.shipments[sid], self.transport.shipments[sid]
            vehicle = self.vehicles[trip.vehicle_id]
            motion = (min(Q(1), Q(sum(self.s.frame.labor.get(f'passenger_labor:{vehicle.id}', {}).values()), 100))
                      if vehicle.id in self.manual else Q(1))
            edge = self.s.cargo.corridors[trip.path[max(0, min(trip.index, len(trip.path) - 1))]]
            people = sum(record.skills_mU.values()) if not record.delivered else 0
            payload = people + sum(inv.balance(Account(record.owner, sid, 'passenger_provisions', good)) for good in ('food', 'water'))
            grant = False
            if trip.mode == TransitMode.WAIT_ENTRY and ctx.tick > trip.departure_tick and self.s.cargo.permitted(record.owner, edge.id):
                used = self.s.cargo.admitted_mU.get(edge.id, 0)
                capacity = self.s.cargo.capacity(edge.id)
                if vehicle.id in self.manual:
                    capacity = min(capacity, used + max(0, 800 - self.s.cargo.manual_admitted_mU.get(edge.id, 0)))
                if payload <= min(capacity - used, self.s.cargo.owner_capacity(record.owner, edge.id), self.s.cargo.terminal_remaining(edge.id)) and self.s.cargo.pay_entry(record.owner, edge.id, payload,
                        {'people': people, **{good: inv.balance(Account(record.owner, sid, 'passenger_provisions', good)) for good in ('food', 'water')}}):
                    self.s.cargo.admission_capacity_mU.setdefault(edge.id, capacity)
                    self.s.cargo.admitted_mU[edge.id] = used + payload
                    self.s.cargo.record_terminal_entry(edge.id, payload)
                    if vehicle.id in self.manual:
                        self.s.cargo.manual_admitted_mU[edge.id] = self.s.cargo.manual_admitted_mU.get(edge.id, 0) + payload
                    if self.s.cargo.transit_contract(record.owner, edge.id):
                        key = record.owner, edge.id
                        self.s.cargo.contract_admitted[key] = self.s.cargo.contract_admitted.get(key, 0) + payload
                    grant = True
            self.transport.advance(ctx.tick, sid, entry_granted=grant, free_capacity_mU=0, defer_unload=True, motion_fraction=motion)
            if not record.delivered and ctx.tick > trip.departure_tick:
                before = record.provision_ticks
                record.provision_ticks += 1
                supplied = True
                for good, ratio in (('food', Q(3, 10)), ('water', Q(1, 5))):
                    ticks = 5 * ctx.clock.cycle_ticks
                    due = floor(Q(people) * ratio * record.provision_ticks / ticks) - floor(Q(people) * ratio * before / ticks)
                    source = Account(record.owner, sid, 'passenger_provisions', good)
                    supplied = supplied and inv.balance(source) >= due
                    inv.transform({source: min(due, inv.balance(source))}, {}, reason='passenger_subsistence', use=Use.CIVILIAN)
                record.shortage_ticks = 0 if supplied else record.shortage_ticks + 1
                if record.shortage_ticks and ctx.clock.is_cycle_end(ctx.tick):
                    # Starvation in custody never deletes savings. Estate cash
                    # returns in its original currency to the origin household.
                    lost = min(people, ceil(Q(people * record.shortage_ticks, ctx.clock.cycle_ticks * 1000)))
                    removed = proportional(lost, record.skills_mU, key=(ctx.seed, 'passenger_mortality', ctx.tick, sid))
                    for skill, q in removed.items():
                        record.skills_mU[skill] -= q
                    reserved_loss = min(record.housing_reserved_mU, lost)
                    record.housing_reserved_mU -= reserved_loss
                    self.housing_reserved[record.destination] -= reserved_loss
                    for account, balance in tuple(self.s.initial.cash.balances().items()):
                        if account.owner == record.owner and account.name == sid:
                            self.s.initial.cash.transfer(account, CashAccount(record.owner, f'household:{record.origin}', account.currency),
                                                         floor(Q(balance.native_mU * lost, people)) if people else 0)
            if trip.mode == TransitMode.WAIT_UNLOAD:
                self.s.initial.populations[record.destination].receive(record.skills_mU)
                record.delivered = True
                self.housing_reserved[record.destination] -= record.housing_reserved_mU
                for account, balance in tuple(self.s.initial.cash.balances().items()):
                    if account.owner == record.owner and account.name == sid:
                        self.s.initial.cash.transfer(account, CashAccount(record.owner, f'household:{record.destination}', account.currency), balance.native_mU)
                for good in ('food', 'water'):
                    source = Account(record.owner, sid, 'passenger_provisions', good)
                    inv.transfer(source, Account(record.owner, record.destination, 'available', good), inv.balance(source))
                self.transport.unload(sid, 0)
            if trip.mode == TransitMode.COMPLETED:
                del self.transport.shipments[sid]; del self.shipments[sid]
                if vehicle.id in self.manual:
                    self.manual.remove(vehicle.id); del self.vehicles[vehicle.id]

    def end_cycle(self, ctx):
        """P7 passive requests use actual skill mixes and finite local vehicles."""
        if not ctx.clock.is_cycle_end(ctx.tick):
            return
        for origin, pop in sorted(self.s.initial.populations.items(), key=lambda x: digest(ctx.seed, 'migration', ctx.cycle, x[0])):
            warning = pop.warning
            if warning == 'GOV_WARN_DISCONTENT':
                eligible, rate = ('general_worker', 'skilled_worker'), Q(3, 1000)
            elif warning in ('GOV_ALERT_STRIKE', 'GOV_CRISIS_PARALYSIS'):
                eligible, rate = ('skilled_worker', 'engineer'), Q(8, 1000)
            else:
                continue
            owner = self.s.authority.regions[self.s.initial.map.hubs[origin].region].controller
            destinations, paths = {}, {}
            for h, p in self.s.initial.populations.items():
                if h == origin or not self.controls(owner, h) or p.support_mU < 450 or self.s.initial.housing_mU.get(h, 0) <= p.total_mU + self.housing_reserved.get(h, 0):
                    continue
                try:
                    paths[h] = self.shortest(owner, origin, h)
                    destinations[h] = max(1, p.support_mU - 450)
                except RuleViolation:
                    continue
            count = floor(sum(pop.skills_mU.get(k, 0) for k in eligible) * rate)
            # Weights are preferences rather than per-destination caps.
            total_weight = sum(destinations.values())
            if not total_weight:
                continue
            demand = {h: ceil(Q(count * w, total_weight)) for h, w in destinations.items()}
            portions = proportional(count, demand, key=(ctx.seed, 'migration_destination', ctx.cycle, origin))
            for dest, count in sorted(portions.items()):
                try:
                    if not count:
                        continue
                    mix = proportional(count, {k: pop.skills_mU.get(k, 0) for k in eligible}, key=(ctx.seed, 'migration_skills', ctx.cycle, origin))
                    self.prepare(owner, {'id': f'passive:{ctx.cycle}:{origin}:{dest}', 'kind': 'governance', 'op': 'relocate_population',
                        'origin_hub': origin, 'dest_hub': dest, 'skills_mU': {k: mix.get(k, 0) for k in SKILLS},
                        'path': list(paths[dest])}, ctx, passive=True).commit()
                except RuleViolation:
                    continue

    def snapshot(self):
        return {'shipments': self.shipments, 'vehicles': self.vehicles, 'trips': self.transport.shipments,
                'housing_reserved': self.housing_reserved, 'serial': self.serial,
                'pending': self.pending, 'manual': self.manual}

    def visible(self, owner):
        return {'shipments': [r for r in self.shipments.values() if r.owner == owner],
                'plans': [o for p, o, _ in self.pending.values() if p == owner]}

    def audit(self):
        self.transport.audit()
        expected = {}
        for r in self.shipments.values():
            if not r.delivered:
                expected[r.destination] = expected.get(r.destination, 0) + r.housing_reserved_mU
            if any(type(q) is not int or q < 0 for q in r.skills_mU.values()):
                raise InvariantViolation('invalid passenger custody')
        if any(q != expected.get(h, 0) for h, q in self.housing_reserved.items()):
            raise InvariantViolation('passenger housing custody mismatch')
        for vehicle in self.vehicles.values():
            if vehicle.id not in self.manual and self.s.initial.inventory.balance(vehicle.physical_account) != 1000:
                raise InvariantViolation('passenger fleet lacks physical vehicle')


class RelocationCommands:
    OPERATIONS = frozenset({('governance', 'relocate_population')})

    def __init__(self, state):
        self.s = state

    def prepare(self, owner, order, ctx):
        return self.s.passengers.prepare(owner, order, ctx)

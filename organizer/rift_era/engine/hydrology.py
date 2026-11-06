"""Directed basin flow, real reservoir custody and actual extraction returns.

The drainage atlas is an explicit generator profile. A region drains toward the
nearest ocean through a deterministic descending drainage rank. Terrain carving
and atlas acceptance remain part of the versioned world-generator gate.
"""

from collections import defaultdict, deque
import math

from .determinism import digest
from .errors import InvariantViolation
from .ledger import Account
from .materials import Origin
from .numeric import Q, clip, floor, proportional, split_payment, units
from .provenance import Use
from .weather import rain_step


HYDROLOGY_VERSION = 'rift_drainage_atlas_v1'


class Hydrology:
    def __init__(self, state):
        self.state = state
        world = state.initial.map
        distance = {r.id: 0 for r in world.regions.values() if r.terrain == 'ocean'}
        queue = deque(sorted(distance))
        while queue:
            rid = queue.popleft()
            for neighbor in world.regions[rid].adjacent:
                if neighbor not in distance:
                    distance[neighbor] = distance[rid] + 1
                    queue.append(neighbor)
        if len(distance) != len(world.regions):
            raise InvariantViolation('land drainage is disconnected from the ocean')
        self.downstream = {}
        atlas_rivers = getattr(world, 'river_downstream', None)
        if atlas_rivers:
            # world_atlas_v1 carries its own validated descending river DAG
            # (one outlet per land region, strictly lower elevation), so
            # upstream-first order is simply descending elevation.
            for rid, region in world.regions.items():
                if region.terrain == 'ocean':
                    continue
                if rid not in atlas_rivers:
                    raise InvariantViolation('atlas land region has no river outlet')
                self.downstream[rid] = tuple(atlas_rivers[rid])
            self.order = tuple(sorted(self.downstream, key=lambda rid: (-world.regions[rid].elevation, rid)))
        else:
            for rid, region in world.regions.items():
                if region.terrain == 'ocean':
                    continue
                choices = [n for n in region.adjacent if distance[n] < distance[rid]]
                # Multiple outlets are a fixed split, never duplicate full outflow.
                chosen = sorted(choices, key=lambda n: digest(world.seed, 'river_outlet', world.attempt, rid, n))[:2]
                self.downstream[rid] = tuple(chosen)
            self.order = tuple(sorted(self.downstream, key=lambda rid: (-distance[rid], rid)))
        self.baseline = {}
        incoming = defaultdict(int)
        natural = units(state.endowments['local_natural_water_per_cycle'])
        for rid in self.order:
            self.baseline[rid] = incoming[rid] + natural
            portions = split_payment(self.baseline[rid], dict.fromkeys(self.downstream[rid], Q(1)),
                                      key=(world.seed, 'river_baseline', rid))
            for dest, amount in portions.items():
                incoming[dest] += amount
        self.rain = dict.fromkeys(self.downstream, Q(1))
        self.natural_cycle_mU = dict.fromkeys(self.downstream, natural)
        self.outflow_cycle_mU = dict(self.baseline)
        self.reservoir_mU = {}
        self.reservoir_region = {}
        self.last_allocated = {}
        self.natural_total_mU = 0
        self.ocean_total_mU = 0
        self.extracted_total_mU = 0
        self.last_tick = -1

    @staticmethod
    def source(hub):
        return Account('nature', f'basin:{hub}', 'deposit', 'water')

    def flow_ratio(self, hub):
        rid = self.state.initial.map.hubs[hub].region
        return clip(Q(self.outflow_cycle_mU.get(rid, 0), max(1, self.baseline.get(rid, 0))), Q(0), Q(6, 5))

    def pumped_site(self, hub):
        region = self.state.initial.map.regions[self.state.initial.map.hubs[hub].region]
        return region.terrain == 'mountain' and region.id in self.downstream and self.baseline[region.id] > 0

    def hydro_multiplier(self, hub):
        state = self.state
        region = state.initial.map.hubs[hub].region
        installed = sum(p.spec.nameplate_mU for lid, p in state.initial.plants.items()
                        if p.spec.recipe == 'P112' and state.initial.map.hubs[state.initial.lines[lid].hub].region == region)
        # The explicit atlas assigns hydro potential from baseline volume. It is
        # immutable and shared by all plants in the same physical catchment.
        potential = self.baseline.get(region, 0)
        return self.flow_ratio(hub) * min(Q(1), Q(potential, max(1, installed)))

    def advance(self, ctx):
        if ctx.tick <= self.last_tick:
            raise InvariantViolation('river tick repeated or backdated')
        state, inventory, world = self.state, self.state.initial.inventory, self.state.initial.map
        cycle_start = ctx.tick % ctx.clock.cycle_ticks == 0
        if cycle_start:
            season = Q(round((.8 + .2 * math.sin(2 * math.pi * ctx.cycle / 240)) * 1000), 1000)
            for rid in self.order:
                self.rain[rid] = rain_step(ctx.seed, world.attempt, ctx.cycle, rid, self.rain[rid], state.global_emissions_mU)
                self.natural_cycle_mU[rid] = floor(units(state.endowments['local_natural_water_per_cycle']) * season
                                                   * self.rain[rid] * state.climate.multiplier(rid))
            for hub in state.rain:
                region = world.hubs[hub].region
                state.rain[hub] = self.rain.get(region, Q(0)) * state.climate.multiplier(region)
                state.water_used_mU[hub] = 0
        returns = defaultdict(int)
        for hub, allocated in self.last_allocated.items():
            unused = inventory.balance(self.source(hub))
            if unused > allocated:
                raise InvariantViolation('unaccounted water entered natural extraction custody')
            self.extracted_total_mU += allocated - unused
            returns[world.hubs[hub].region] += unused
        reservoirs = defaultdict(list)
        for sid, system in state.systems.instances.items():
            if system.kind == 'reservoir':
                rid = world.hubs[system.hub].region
                self.reservoir_mU.setdefault(sid, 0)
                capacity = floor(200000 * state.systems.structural_factor(system))
                spill = max(0, self.reservoir_mU[sid] - capacity)
                self.reservoir_mU[sid] -= spill
                returns[rid] += spill
                if system.online and state.systems.availability(system) > 0:
                    reservoirs[rid].append(sid)
        for sid in tuple(self.reservoir_mU):
            if sid not in state.systems.instances:
                # Destroyed/dismantled reservoirs spill to their recorded region.
                rid = self.reservoir_region[sid]
                returns[rid] += self.reservoir_mU.pop(sid)
        self.reservoir_region = {sid: world.hubs[state.systems.instances[sid].hub].region for sid in self.reservoir_mU}
        requests = defaultdict(lambda: defaultdict(int))
        for mid, mine in state.initial.mines.items():
            if mine.good != 'water':
                continue
            amount = floor(state.extraction_lines[mid].recipe.outputs['water'] * state.initial.assets[mid].availability / 10)
            requests[world.hubs[mine.hub].region][mine.hub] += amount
        incoming, allocated_by_hub, outflows = defaultdict(int), {}, {}
        phase = ctx.tick % ctx.clock.cycle_ticks
        for rid in self.order:
            natural = self.natural_cycle_mU[rid]
            natural_tick = natural * (phase + 1) // ctx.clock.cycle_ticks - natural * phase // ctx.clock.cycle_ticks
            self.natural_total_mU += natural_tick
            flow = natural_tick + incoming[rid] + returns[rid]
            active = sorted(reservoirs[rid])
            if active:
                gamma = min(Q(7, 10), Q(35, 100) * len(active))
                target = round((1 - gamma) * flow + gamma * Q(self.baseline[rid], ctx.clock.cycle_ticks))
                if flow > target:
                    free = {sid: max(0, floor(200000 * state.systems.structural_factor(state.systems.instances[sid])) - self.reservoir_mU[sid]) for sid in active}
                    stored = proportional(flow - target, free, key=(ctx.seed, 'reservoir_store', ctx.tick, rid))
                    for sid, amount in stored.items():
                        self.reservoir_mU[sid] += amount
                    flow -= sum(stored.values())
                else:
                    released = proportional(target - flow, {sid: self.reservoir_mU[sid] for sid in active},
                                            key=(ctx.seed, 'reservoir_release', ctx.tick, rid))
                    for sid, amount in released.items():
                        self.reservoir_mU[sid] -= amount
                    flow += sum(released.values())
            grants = proportional(flow * 4 // 5, requests[rid], key=(ctx.seed, 'river_withdrawal', ctx.tick, rid))
            allocated_by_hub.update(grants)
            remaining = flow - sum(grants.values())
            outflows[rid] = remaining * ctx.clock.cycle_ticks
            branches = split_payment(remaining, dict.fromkeys(self.downstream[rid], Q(1)),
                                      key=(ctx.seed, 'river_branch', ctx.tick, rid))
            for dest, amount in branches.items():
                if dest in self.downstream:
                    incoming[dest] += amount
                else:
                    self.ocean_total_mU += amount
        old = {self.source(h): inventory.balance(self.source(h)) for h in self.last_allocated}
        new = {self.source(h): q for h, q in allocated_by_hub.items()}
        inventory.transform(old, new, reason='river_reservations_and_actual_returns',
                            origins={a: Origin('nature', a.location.split(':', 1)[1]) for a in new}, use=Use.EXTRACTION)
        self.last_allocated = allocated_by_hub
        self.outflow_cycle_mU = outflows
        for hub in state.natural_water_mU:
            state.natural_water_mU[hub] = allocated_by_hub.get(hub, 0) * ctx.clock.cycle_ticks
        self.last_tick = ctx.tick
        self.audit()

    def audit(self):
        current = sum(self.last_allocated.values())
        held = sum(self.reservoir_mU.values())
        if self.natural_total_mU != self.ocean_total_mU + self.extracted_total_mU + current + held:
            raise InvariantViolation('basin water created or lost')
        if any(v < 0 for v in self.reservoir_mU.values()):
            raise InvariantViolation('negative reservoir custody')

    def snapshot(self):
        return {'version': HYDROLOGY_VERSION, 'downstream': self.downstream, 'baseline': self.baseline,
                'rain': self.rain, 'natural_cycle_mU': self.natural_cycle_mU, 'outflow_cycle_mU': self.outflow_cycle_mU,
                'reservoir_mU': self.reservoir_mU, 'reservoir_region': self.reservoir_region,
                'last_allocated': self.last_allocated, 'natural_total_mU': self.natural_total_mU,
                'ocean_total_mU': self.ocean_total_mU, 'extracted_total_mU': self.extracted_total_mU, 'last_tick': self.last_tick}

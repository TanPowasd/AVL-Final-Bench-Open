"""Built high-voltage circuits and inventory-backed irrigation canals.

Electricity remains dispatch-only. Canal water is drawn from actually extracted
local inventory, held before pumping, then physically transferred with explicit
evaporation. Neither system borrows cargo corridor capacity.
"""
from dataclasses import dataclass
from collections import defaultdict

from .determinism import digest
from .errors import InvariantViolation, RuleViolation
from .ledger import Account, Transfer
from .numeric import Q, ceil, floor, proportional
from .power import PowerEdge
from .provenance import Leg, Use


@dataclass
class UtilityLink:
    id: str
    owner: str
    a: str
    b: str
    mode: str
    level: int
    cells: tuple[int, ...]
    asset_id: str
    enabled: bool = True
    component_assets: tuple[str, ...] = ()


class UtilityNetwork:
    def __init__(self, state):
        self.s = state
        self.links = {}
        self.power_requests = {}
        self.water_reserved = {}
        self.water_sent_mU = {}
        self.water_received_mU = {}
        self.water_lost_mU = {}
        self.loss_remainders = {}
        self.last_tick = -1

    def operator(self, link):
        rights = self.s.authority.assets.get(link.asset_id)
        return rights.operator if rights else link.owner

    def availability(self, link):
        assets = [self.s.initial.assets.get(aid) for aid in (link.asset_id, *link.component_assets)]
        return min((asset.availability if asset else Q(0) for asset in assets), default=Q(0)) if link.enabled else Q(0)

    def power_rating(self, link):
        owner = self.operator(link)
        known = self.s.research[owner].known
        rating = 240000 if 'E09' in known else 120000 if link.level == 2 else 60000
        rate = Q(2, 1000) if 'E09' in known else Q(9, 1000) if 'E07' in known else Q(15, 1000)
        loss = min(Q(35, 100), rate * Q(len(link.cells) - 1, 10))
        if any(self.s.initial.map.terrain_cells[c] == 0 for c in link.cells):
            loss = min(loss, Q(8, 100))  # E12 intercontinental link: loss capped at 8%
        # 10 C15: AI grid dispatch (AI06) cuts line loss by the same factor as forced
        # outages; a line takes the better of its two endpoint hubs (couplings_v1).
        deployment = getattr(self.s, 'ai_deployment', None)
        if deployment is not None and 'AI06' in known:
            loss *= min(deployment.grid_multiplier(owner, link.a), deployment.grid_multiplier(owner, link.b))
        return rating, 1 - loss

    def dispatch_edges(self, ctx=None):
        edges = []
        for link in sorted(self.links.values(), key=lambda e: e.id):
            if link.mode != 'power':
                continue
            rating, survival = self.power_rating(link)
            capacity = floor(Q(rating, 10) * self.availability(link))
            if capacity:
                edges.append(PowerEdge(link.id, link.a, link.b, capacity, survival, self.operator(link)))
        return tuple(edges)

    def water_lock(self, link):
        return Account(link.owner, link.a, f'locked_canal:{link.id}', 'water')

    def pump_demand(self, link, amount):
        world = self.s.initial.map
        rise = world.regions[world.hubs[link.b].region].elevation > world.regions[world.hubs[link.a].region].elevation
        if not rise:
            return 0
        # A full rated canal consumes the table's power per ten physical cells;
        # partial loads scale continuously, rather than paying for empty flow.
        rated = 16000 if link.level == 2 else 4000
        nominal = Q(1200 if link.level == 2 else 500, 10) * Q(len(link.cells) - 1, 10)
        return ceil(nominal * Q(amount, rated))

    def reserve(self, ctx):
        inv = self.s.initial.inventory
        # Release last tick's unpumped water before making another reservation.
        for link in self.links.values():
            if link.mode == 'water':
                lock = self.water_lock(link)
                inv.transfer(lock, Account(link.owner, link.a, 'available', 'water'), inv.balance(lock))
        self.water_reserved = {}; self.power_requests = {}
        by_source = defaultdict(dict)
        for link in self.links.values():
            if link.mode == 'water':
                rated = 16000 if link.level == 2 else 4000
                capacity = floor(rated * self.availability(link))
                if self.operator(link) != link.owner or not self.s.authority.controls_hub(link.owner, link.a):
                    capacity = 0
                by_source[Account(link.owner, link.a, 'available', 'water')][link.id] = capacity
        transfers = []
        for source, requests in by_source.items():
            grants = proportional(inv.balance(source), requests, key=(ctx.seed, 'canal_reserve', ctx.tick, source.owner, source.location))
            for eid, amount in grants.items():
                link = self.links[eid]
                transfers.append(Transfer(source, self.water_lock(link), amount))
                self.water_reserved[eid] = amount
                demand = self.pump_demand(link, amount)
                if demand:
                    entity = f'canal_pump:{eid}'
                    self.power_requests[entity] = (link.a, 2, demand)
                    self.s.frame.work_context[entity] = (link.owner, link.a, 'treasury')
        inv.transfer_many(transfers)

    def power_demands(self):
        return self.power_requests

    def advance(self, ctx):
        if ctx.tick <= self.last_tick:
            raise InvariantViolation('utility transport repeated or backdated')
        inv = self.s.initial.inventory
        self.water_sent_mU = {}; self.water_received_mU = {}; self.water_lost_mU = {}
        occupied = self.s.warehouse_load()
        demands = defaultdict(dict)
        for eid, amount in self.water_reserved.items():
            link = self.links[eid]
            power = self.power_requests.get(f'canal_pump:{eid}', ('', 0, 0))[2]
            if power:
                amount = floor(amount * min(Q(1), Q(self.s.frame.power_received.get(f'canal_pump:{eid}', 0), power)))
            demands[link.b][eid] = amount
        for hub, requests in demands.items():
            free = max(0, floor(self.s.initial.warehouse_mU.get(hub, 0) - occupied.get(hub, 0)))
            grants = proportional(free, requests, key=(ctx.seed, 'canal_destination', ctx.tick, hub))
            for eid, amount in grants.items():
                link = self.links[eid]
                amount = min(amount, self.water_reserved[eid])
                loss = min(Q(1), Q(1 if link.level == 2 else 2, 100) * Q(len(link.cells) - 1, 10))
                exact = self.loss_remainders.get(eid, Q(0)) + amount * loss
                lost = min(amount, floor(exact))
                self.loss_remainders[eid] = exact - lost
                received = amount - lost
                source = self.water_lock(link)
                mobile = Account(link.owner, eid, 'canal_flow', 'water')
                inv.transfer_many((Transfer(source, mobile, amount, destination_hub=link.a),))
                inv.record_transport((mobile,), Leg(link.a, link.b, self.operator(link), len(link.cells) - 1))
                inv.transform({mobile: lost}, {}, reason='canal_evaporation_and_seepage', use=Use.DESTRUCTION)
                inv.transfer(mobile, Account(link.owner, link.b, 'available', 'water'), received)
                self.water_sent_mU[eid], self.water_received_mU[eid], self.water_lost_mU[eid] = amount, received, lost
        self.last_tick = ctx.tick
        self.audit()

    def snapshot(self):
        return {'links': self.links, 'power_requests': self.power_requests, 'water_reserved': self.water_reserved,
                'water_sent_mU': self.water_sent_mU, 'water_received_mU': self.water_received_mU,
                'water_lost_mU': self.water_lost_mU, 'loss_remainders': self.loss_remainders, 'last_tick': self.last_tick}

    def visible(self, owner):
        return {'links': [l for l in self.links.values() if l.owner == owner]}

    def receipts(self):
        """Exact physical telemetry; losses never count as consumption."""
        return [{'link_id': eid, 'sent_mU': sent, 'received_mU': self.water_received_mU[eid],
                 'lost_mU': self.water_lost_mU[eid]} for eid, sent in sorted(self.water_sent_mU.items())]

    def audit(self):
        for eid, sent in self.water_sent_mU.items():
            if sent != self.water_received_mU[eid] + self.water_lost_mU[eid]:
                raise InvariantViolation('canal water created or lost')
        if any(not 0 <= q < 1 for q in self.loss_remainders.values()):
            raise InvariantViolation('invalid canal evaporation remainder')


class UtilityConstruction:
    def __init__(self, state, industry):
        self.s, self.industry = state, industry

    def prepare(self, owner, order, ctx):
        from .construction import WorkOrder
        from .world import SIZE
        a, b = order['origin_hub'], order['dest_hub']
        start, end = self.industry._owned_hub(owner, a), self.industry._owned_hub(owner, b)
        mode = {'grid': 'power', 'canal': 'water'}.get(order['mode'], order['mode'])
        level, cells = order['level'], tuple(order['path_cells'])
        if a == b or level not in (1, 2) or not 2 <= len(cells) <= 512 or len(set(cells)) != len(cells) or any(type(c) is not int or not 0 <= c < SIZE * SIZE for c in cells):
            raise RuleViolation('ERR_SCHEMA')
        if cells[0] != start.y * SIZE + start.x or cells[-1] != end.y * SIZE + end.x or any(abs(p % SIZE - q % SIZE) + abs(p // SIZE - q // SIZE) != 1 for p, q in zip(cells, cells[1:])):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        palette = sorted(self.s.initial.map.regions)
        ocean = {c for c in cells if self.s.initial.map.terrain_cells[c] == 0}
        if ocean:
            # 04 E12: an undersea superconducting DC power link, one segment <= 60 cells.
            if mode != 'power' or len(cells) - 1 > 60:
                raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'utility line crosses ocean without bridge')
            self.industry._tech(owner, {'E12'})
        if any(not self.s.authority.controls_hub(owner, h) for h in (a, b)) or any(self.s.authority.regions[palette[self.s.initial.map.region_cells[c]]].controller != owner for c in cells if c not in ocean):
            raise RuleViolation('ERR_NO_PERMISSION')
        self.industry._tech(owner, {'E02'} if mode == 'power' else {'A01'} if level == 1 else {'A01', 'L06'})
        links = self.s.utilities.links
        existing = next((l for l in links.values() if l.mode == mode and l.cells in (cells, tuple(reversed(cells)))), None)
        if existing and existing.level >= level:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'utility already built at this level')
        if any(j.kind == 'build_corridor' and j.payload.get('mode') == mode and j.payload.get('cells') in (cells, tuple(reversed(cells))) for j in self.s.work.jobs.values()):
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        if len(links) + len(self.s.cargo.corridors) + sum(j.kind == 'build_corridor' for j in self.s.work.jobs.values()) >= self.s.catalog.runtime['limits']['corridors_global']:
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        blocks = Q(len(cells) - 1, 10)
        if mode == 'power':
            coefficients, periods = {'copper': 6 * level, 'steel': 4 * level, 'cement': 2 * level}, 8 * level
        elif level == 1:
            coefficients, periods = {'cement': 3, 'stone': 4}, 4
        else:
            coefficients, periods = {'cement': 12, 'steel': 6, 'basic_parts': 2}, 12
        materials = {g: ceil(q * 1000 * blocks) for g, q in coefficients.items()}
        if ocean:
            materials['superconductor'] = materials.get('superconductor', 0) + 40000
        duration = ceil(periods * blocks) * ctx.clock.cycle_ticks
        if existing:
            installed = [self.s.initial.assets[aid] for aid in (existing.asset_id, *existing.component_assets)]
            materials = {g: max(0, q - floor(sum((old.book_nmU.get(g, Q(0)) for old in installed), Q(0)) / 1000)) for g, q in materials.items()}
            duration = max(2 * ctx.clock.cycle_ticks, duration - ceil((8 if mode == 'power' else 4) * blocks) * ctx.clock.cycle_ticks)
        jid, aid = self.industry._ids(owner, order)
        asset = self.industry._asset(aid, owner, a, materials)
        eid = existing.id if existing else f'{mode}:{aid}'
        job = WorkOrder(jid, str(int(order['id'])), owner, a, 'build_corridor', aid, duration,
                        materials, self.industry._service_cost(owner, blocks), temporary_asset=aid,
                        payload={'corridor_id': eid, 'a': a, 'b': b, 'cells': cells, 'level': level, 'mode': mode,
                                 'previous_asset_id': existing.asset_id if existing else None})
        return self.s.work.prepare_start(job, lambda: self.s.initial.assets.__setitem__(aid, asset))

    @staticmethod
    def complete(state, job):
        data = job.payload
        state.asset_lifecycle.commission(job.target)
        previous = data.get('previous_asset_id')
        # Old structure retains its original provenance/book/economic title.
        # Upgrade availability is limited by both real installed components.
        old_link = state.utilities.links.get(data['corridor_id'])
        components = ((previous, *old_link.component_assets) if previous and old_link else ())
        state.utilities.links[data['corridor_id']] = UtilityLink(data['corridor_id'], job.owner,
            data['a'], data['b'], data['mode'], data['level'], data['cells'], job.target, component_assets=components)

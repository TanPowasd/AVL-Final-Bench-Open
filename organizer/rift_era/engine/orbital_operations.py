"""Physical satellite launch queues and finite orbital berths.

Tickets do not reserve inventory before winning a berth and a launch site.
Preparation is powered work at the actual site. Payload remains a real locked
whole good until the completed ascent; installed orbit inventory decays once.
"""
from dataclasses import dataclass

from .command_schema import fields
from .determinism import digest
from .errors import RuleViolation, InvariantViolation
from .ledger import Account, Transfer
from .numeric import Q
from .protocol import PreparedCommand
from .provenance import Use, Leg


@dataclass
class Launch:
    id: str
    owner: str
    command_id: str
    hub: str
    berth: tuple[int, int, int]
    reusable: bool
    site: str | None = None
    launcher: str | None = None
    elapsed: int = 0
    status: str = 'QUEUED'
    payload: str = 'satellite'


@dataclass
class Launcher:
    id: str
    owner: str
    hub: str
    uses: int = 0
    busy: str | None = None


@dataclass
class Satellite:
    id: str
    owner: str
    launch_hub: str
    berth: tuple[int, int, int]
    commissioned_tick: int
    expires_tick: int


@dataclass
class Platform:
    """03 N11 orbital industrial platform (orbital_platform_v1): one line, P081/P087 only."""
    id: str
    owner: str
    launch_hub: str      # its ferry base: uplink source and downlink destination
    berth: tuple[int, int, int]
    commissioned_tick: int
    expires_tick: int
    recipe: str | None = None
    version: int = 0


ORBITAL_RECIPES = ('P081', 'P087')
FERRY_LOAD = 20000           # 20 standard loads per platform per cycle, each direction
FERRY_COSTS = {'fuel': 2000, 'superalloy': 500}


class OrbitalOperations:
    def __init__(self, state):
        self.s = state
        self.expanded = False
        self.launches = {}
        self.launchers = {}
        self.satellites = {}
        self.platforms = {}
        self.recent = {p: [] for p in state.initial.known}

    @staticmethod
    def payload_account(job):
        return Account(job.owner, job.hub, 'launch:' + job.id, job.payload)

    @staticmethod
    def platform_account(platform):
        return Account(platform.owner, platform.id, 'orbital_installed', 'orbital_platform')

    @staticmethod
    def platform_stock(platform, good):
        return Account(platform.owner, platform.id, 'orbital_stock', good)

    @staticmethod
    def platform_inbound(platform, good):
        return Account(platform.owner, platform.id, 'orbital_inbound', good)

    @staticmethod
    def platform_downlink(platform, good):
        return Account(platform.owner, platform.launch_hub, 'orbital_downlink:' + platform.id, good)

    @staticmethod
    def launcher_account(launcher):
        return Account(launcher.owner, launcher.hub, 'launcher:' + launcher.id, 'reusable_launcher')

    @staticmethod
    def satellite_account(satellite):
        return Account(satellite.owner, satellite.id, 'orbital_installed', 'satellite')

    def sites(self, owner, hub):
        state = self.s
        return [system for system in state.systems.instances.values()
                if system.owner == owner and system.hub == hub and system.kind == 'launch_site'
                and state.systems.availability(system) > 0
                and state.initial.facilities[system.host].profile in ('S3', 'S5')
                and state.authority.controls_hub(owner, hub)]

    def costs(self, reusable, payload='satellite'):
        return {payload: 1000, 'fuel': 2400 if reusable else 8000,
                'pure_chemicals': 2000} | ({'superalloy': 500} if reusable else {})

    def ready_launcher(self, owner, hub):
        return next((r for r in sorted(self.launchers.values(), key=lambda r: r.id)
                     if r.owner == owner and r.hub == hub and r.busy is None and r.uses < 20), None)

    def discover_capacity(self):
        self.expanded |= any('N12' in research.known for research in self.s.research.values())

    def begin_tick(self, ctx):
        state = self.s
        self.discover_capacity()
        for key, satellite in tuple(self.satellites.items()):
            if ctx.tick >= satellite.expires_tick:
                state.initial.inventory.transform({self.satellite_account(satellite): 1000}, {},
                    reason='satellite_end_of_life', use=Use.DESTRUCTION)
                del self.satellites[key]

    def reserve(self, ctx):
        state = self.s
        occupied = {s.berth for s in self.satellites.values()}
        occupied.update(p.berth for p in self.platforms.values())
        occupied.update(j.berth for j in self.launches.values() if j.site is not None)
        busy_sites = {j.site for j in self.launches.values() if j.site is not None}
        # One global ordering covers both contested berths and shared launch sites.
        queued = sorted((j for j in self.launches.values() if j.site is None),
                        key=lambda j: digest(ctx.seed, 'orbital_admission', ctx.tick, *j.berth, j.owner, j.command_id))
        for job in queued:
            if job.berth in occupied:
                continue
            sites = [s for s in self.sites(job.owner, job.hub) if s.id not in busy_sites]
            if not sites:
                continue
            site = min(sites, key=lambda s: digest(ctx.seed, 'launch_site', ctx.tick, job.id, s.id))
            costs = self.costs(job.reusable, job.payload)
            launcher = self.ready_launcher(job.owner, job.hub) if job.reusable else None
            if job.reusable and launcher is None:
                costs['reusable_launcher'] = 1000
            sources = {Account(job.owner, job.hub, 'available', g): q for g, q in costs.items()}
            if any(state.initial.inventory.balance(a) < q for a, q in sources.items()):
                continue
            if job.reusable and launcher is None:
                launcher = Launcher('launcher:' + job.id, job.owner, job.hub)
            transfers = []
            for source, quantity in sources.items():
                if source.asset == 'reusable_launcher':
                    target = self.launcher_account(launcher)
                else:
                    target = Account(job.owner, job.hub, 'launch:' + job.id, source.asset)
                transfers.append(Transfer(source, target, quantity))
            state.initial.inventory.transfer_many(transfers)
            job.site = site.id
            job.status = 'PREPARING'
            if launcher is not None:
                self.launchers[launcher.id] = launcher
                launcher.busy = job.id
                job.launcher = launcher.id
            occupied.add(job.berth)
            busy_sites.add(site.id)

    def advance(self, ctx):
        state = self.s
        # Refresh already-effective prerequisites. New research unlocks are
        # promoted at the P8 next-tick boundary, including public capacity.
        self.discover_capacity()
        for job in tuple(self.launches.values()):
            if job.site is None:
                continue
            site = state.systems.instances.get(job.site)
            if (site is None or not site.online or state.systems.availability(site) <= 0
                    or not state.authority.controls_hub(job.owner, job.hub)):
                job.status = 'PAUSED'
                continue
            job.status = 'PREPARING'
            job.elapsed += 1
            if job.elapsed < 2 * ctx.clock.cycle_ticks:
                continue
            costs = {g: q for g, q in self.costs(job.reusable, job.payload).items() if g != job.payload}
            consumed = {Account(job.owner, job.hub, 'launch:' + job.id, g): q for g, q in costs.items()}
            state.initial.inventory.transform(consumed, {}, reason='satellite_launch_operations', use=Use.CIVIL_LOGISTICS)
            if job.payload == 'orbital_platform':
                # 03 N11: in orbit for 360 cycles; it holds a berth like a satellite.
                platform = Platform('platform:' + job.id, job.owner, job.hub, job.berth, ctx.tick + 1,
                                    ctx.tick + 1 + 360 * ctx.clock.cycle_ticks)
                state.initial.inventory.transfer_many((Transfer(self.payload_account(job), self.platform_account(platform),
                    1000, legs=(Leg(job.hub, platform.id, job.owner, 1, eligible=False),)),))
                self.platforms[platform.id] = platform
                self._release_launcher(job)
                self._finish(job, 'COMPLETED')
                continue
            life = 270 if 'N12' in state.research[job.owner].known else 180
            life_ticks = state.asat.new_lifetime(job.berth[0], ctx.tick + 1, life * ctx.clock.cycle_ticks)
            satellite = Satellite('satellite:' + job.id, job.owner, job.hub, job.berth,
                                  ctx.tick + 1, ctx.tick + 1 + life_ticks)
            # Vertical ascent has no eligible L1 transit credit. The path token
            # records the completed relocation only; see the implementation contract.
            state.initial.inventory.transfer_many((Transfer(self.payload_account(job), self.satellite_account(satellite),
                1000, legs=(Leg(job.hub, satellite.id, job.owner, 1, eligible=False),)),))
            self.satellites[satellite.id] = satellite
            self._release_launcher(job)
            self._finish(job, 'COMPLETED')
        if ctx.clock.is_cycle_end(ctx.tick):
            self._platform_cycle(ctx)

    def _use_launcher(self, launcher):
        launcher.uses += 1
        launcher.busy = None
        if launcher.uses == 20:
            self.s.initial.inventory.transform({self.launcher_account(launcher): 1000}, {},
                reason='launcher_end_of_life', use=Use.DESTRUCTION)
            del self.launchers[launcher.id]

    def _release_launcher(self, job):
        if job.launcher is not None:
            self._use_launcher(self.launchers[job.launcher])

    # -- 03 N11 orbital industry (orbital_platform_v1) ---------------------------
    def _platform_cycle(self, ctx):
        """Cycle end, per platform: last cycle's ferry loads arrive, the line runs
        one cycle from orbital stock (x1.5 output), then one reusable-launcher round
        trip brings products down and next cycle's inputs up (each <= 20 loads)."""
        state = self.s
        inv = state.initial.inventory
        weights = state.cargo_weights
        for pid in sorted(self.platforms):
            platform = self.platforms[pid]
            held = {a: q for a, q in inv.balances().items() if a.location == platform.id
                    or a.bucket == 'orbital_downlink:' + platform.id}
            if ctx.tick + 1 >= platform.expires_tick:
                inv.transform({self.platform_account(platform): 1000,
                               **{a: q for a, q in held.items() if a.bucket != 'orbital_installed'}}, {},
                              reason='orbital_platform_end_of_life', use=Use.DESTRUCTION)
                del self.platforms[pid]
                self.recent[platform.owner].append({'id': pid, 'status': 'EXPIRED', 'tick': ctx.tick})
                continue
            # 1. arrivals sent last cycle
            moves = []
            for account, quantity in sorted(held.items()):
                if account.bucket == 'orbital_inbound':
                    moves.append(Transfer(account, self.platform_stock(platform, account.asset), quantity))
                elif account.bucket == 'orbital_downlink:' + platform.id:
                    moves.append(Transfer(account, Account(platform.owner, platform.launch_hub, 'available', account.asset), quantity))
            if moves:
                inv.transfer_many(tuple(moves))
            # 2. one cycle of production
            recipe = state.catalog.recipes.get(platform.recipe) if platform.recipe else None
            if recipe is not None:
                need = {self.platform_stock(platform, g): q * 1000 for g, q in recipe['inputs'].items()}
                if all(inv.balance(a) >= q for a, q in need.items()):
                    out = {self.platform_stock(platform, g): q * 1500 for g, q in recipe['outputs'].items()}
                    from .materials import Origin
                    inv.transform(need, out, reason='orbital_production', use=Use.INDUSTRY,
                                  origins={a: Origin(platform.owner, platform.id) for a in out})
            # 3. ferry round trip
            launcher = self.ready_launcher(platform.owner, platform.launch_hub)
            base = {g: Account(platform.owner, platform.launch_hub, 'available', g) for g in FERRY_COSTS}
            if launcher is None or any(inv.balance(base[g]) < q for g, q in FERRY_COSTS.items()):
                continue
            down, budget = [], FERRY_LOAD
            products = set(recipe['outputs']) if recipe is not None else set()
            for account, quantity in sorted(inv.balances().items()):
                if account.location != platform.id or account.bucket != 'orbital_stock':
                    continue
                if recipe is not None and account.asset in recipe['inputs'] and account.asset not in products:
                    continue
                amount = min(quantity, int(budget / weights[account.asset]))
                if amount > 0:
                    down.append(Transfer(account, self.platform_downlink(platform, account.asset), amount,
                                         legs=(Leg(platform.id, platform.launch_hub, platform.owner, 1, eligible=False),)))
                    budget -= amount * weights[account.asset]
            up, budget = [], FERRY_LOAD
            if recipe is not None:
                for good, q in sorted(recipe['inputs'].items()):
                    # top the orbital stock up to one cycle of this input
                    source = Account(platform.owner, platform.launch_hub, 'available', good)
                    amount = min(max(0, q * 1000 - inv.balance(self.platform_stock(platform, good))),
                                 inv.balance(source), int(budget / weights[good]))
                    if amount > 0:
                        up.append(Transfer(source, self.platform_inbound(platform, good), amount,
                                           legs=(Leg(platform.launch_hub, platform.id, platform.owner, 1, eligible=False),)))
                        budget -= amount * weights[good]
            if not down and not up:
                continue
            inv.transfer_many(tuple(down + up))
            inv.transform({base[g]: q for g, q in FERRY_COSTS.items()}, {}, reason='orbital_ferry_operations',
                          use=Use.CIVIL_LOGISTICS)
            self._use_launcher(launcher)

    def _finish(self, job, status):
        self.recent[job.owner].append({'command_id': job.command_id, 'id': job.id, 'status': status})
        self.recent[job.owner] = self.recent[job.owner][-64:]
        del self.launches[job.id]
        self.s.command_streams[job.owner].plans.pop(job.command_id, None)

    def cancel(self, owner, command_id):
        job = next((j for j in self.launches.values() if j.owner == owner and j.command_id == command_id), None)
        if job is None:
            return None
        inventory = self.s.initial.inventory
        transfers = [Transfer(a, Account(owner, job.hub, 'available', a.asset), quantity)
                     for a, quantity in inventory.balances().items()
                     if a.owner == owner and a.location == job.hub and a.bucket == 'launch:' + job.id]
        plan = inventory.prepare_transfers(transfers)
        def commit():
            plan.commit()
            if job.launcher is not None:
                self.launchers[job.launcher].busy = None
            self._finish(job, 'CANCELLED')
        return PreparedCommand(commit)

    def coverage(self, owner):
        state = self.s
        count = sum(s.owner == owner for s in self.satellites.values())
        known = state.research[owner].known
        ground_control = any(s.owner == owner and s.kind in ('launch_site', 'fusion_sensor') and s.online
                             and state.systems.availability(s) > 0 for s in state.systems.instances.values())
        if not ground_control:
            return None
        if count >= 8 and 'N12' in known:
            return 'land_sea', Q(9, 10)
        if count >= 3 and {'N08', 'C08'} <= known:
            return 'sea', Q(4, 5)
        return None

    def visible(self, owner):
        return {'capacity': 48 if self.expanded else 24,
                'satellites': [{'id': s.id, 'orbit_plane': s.berth[0], 'slot': s.berth[1], 'berth': s.berth[2],
                                'expires_tick': s.expires_tick} for s in self.satellites.values() if s.owner == owner],
                'launches': [{'id': j.id, 'command_id': j.command_id, 'hub_id': j.hub, 'status': j.status,
                              'elapsed_ticks': j.elapsed} for j in self.launches.values() if j.owner == owner],
                'launchers': [{'id': r.id, 'hub_id': r.hub, 'uses': r.uses, 'busy': r.busy is not None}
                              for r in self.launchers.values() if r.owner == owner],
                **({'platforms': [{'id': p.id, 'base_hub': p.launch_hub, 'recipe': p.recipe, 'expires_tick': p.expires_tick,
                                   'version': p.version} for p in self.platforms.values() if p.owner == owner]}
                   if any(p.owner == owner for p in self.platforms.values()) else {}),
                'recent': list(self.recent[owner])}

    def audit(self):
        occupied = [s.berth for s in self.satellites.values()]
        occupied += [p.berth for p in self.platforms.values()]
        occupied += [j.berth for j in self.launches.values() if j.site is not None]
        if any(not 0 <= p < 3 or not 0 <= s < 8 or b not in (0, 1) or b == 1 and not self.expanded for p, s, b in occupied):
            raise InvariantViolation('orbital berth outside public capacity')
        if len(occupied) != len(set(occupied)):
            raise InvariantViolation('orbital berth multiply occupied')
        inventory = self.s.initial.inventory
        for row in self.satellites.values():
            if inventory.balance(self.satellite_account(row)) != 1000 or row.expires_tick <= row.commissioned_tick:
                raise InvariantViolation('satellite lost its physical payload or lifetime')
        for row in self.launchers.values():
            if not 0 <= row.uses < 20 or inventory.balance(self.launcher_account(row)) != 1000:
                raise InvariantViolation('invalid physical reusable launcher')
        for job in self.launches.values():
            if job.site is not None and inventory.balance(self.payload_account(job)) != 1000:
                raise InvariantViolation('launch preparation lost its locked payload')
        for row in self.platforms.values():
            if inventory.balance(self.platform_account(row)) != 1000 or row.recipe not in (None, *ORBITAL_RECIPES):
                raise InvariantViolation('orbital platform lost its hull or runs a ground recipe')

    def snapshot(self):
        return {'expanded': self.expanded, 'launches': self.launches, 'launchers': self.launchers,
                'satellites': self.satellites, 'recent': self.recent,
                **({'platforms': self.platforms} if self.platforms else {})}


class OrbitalCommands:
    OPERATIONS = frozenset({('intelligence', 'orbital_launch')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        fields(order, {'hub_id': str, 'payload': str, 'orbit_plane': int, 'slot': int, 'berth': int, 'use_reusable': bool})
        state = self.state
        if (order['payload'] not in ('satellite', 'orbital_platform') or not 0 <= order['orbit_plane'] < 3
                or not 0 <= order['slot'] < 8 or order['berth'] not in (0, 1)):
            raise RuleViolation('ERR_SCHEMA')
        known = state.research[owner].known
        if 'N06' not in known or order['use_reusable'] and 'N09' not in known or order['berth'] == 1 and 'N12' not in known:
            raise RuleViolation('ERR_TECH_LOCKED')
        if order['payload'] == 'orbital_platform' and 'N11' not in known:
            raise RuleViolation('ERR_TECH_LOCKED')
        hub = order['hub_id']
        if not state.orbit.sites(owner, hub):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        command_id = str(int(order['id']))
        job = Launch('launch:'+digest(owner,command_id).hex()[:24], owner, command_id, hub,
                     (order['orbit_plane'], order['slot'], order['berth']), order['use_reusable'],
                     payload=order['payload'])
        return PreparedCommand(lambda: state.orbit.launches.__setitem__(job.id, job), persistent=True)

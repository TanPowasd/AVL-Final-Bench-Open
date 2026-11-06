"""Finite public-slot installations from the command table in chapter 06."""

from dataclasses import dataclass, field

from .command_schema import fields
from .construction import WorkOrder
from .determinism import digest
from .errors import InvariantViolation, RuleViolation
from .numeric import Q, ceil
from .protocol import PreparedCommand


@dataclass(frozen=True)
class SystemSpec:
    technology: str
    materials: tuple[tuple[str, int], ...]
    power_mU_cycle: int = 100
    rank: int = 0
    workers_mU_cycle: int = 0
    profiles: tuple[str, ...] = ('S1', 'S2', 'S3', 'S4', 'S5')


SYSTEMS = {
    'reservoir': SystemSpec('A01', (('cement', 20000), ('steel', 5000)), 0, 1, 1000),
    'academy': SystemSpec('R02', (('lab_supplies', 2000),)),
    'radio_backbone': SystemSpec('C02', (('communication_equipment', 1000),)),
    'smart_grid': SystemSpec('E07', (('control_module', 1000),)),
    'fusion_sensor': SystemSpec('C08', (('computing_equipment', 1000),)),
    'environment': SystemSpec('V01', (('sensor', 1000),)),
    'water_recycle': SystemSpec('V04', (('basic_parts', 2000), ('control_module', 1000)), 2000, 1),
    'climate_model': SystemSpec('V06', (('computing_equipment', 1000),), 2000, 4),
    'counterintel': SystemSpec('S04', (('communication_equipment', 1000), ('crypto_module', 1000)), 1000, 0),
    'resilience': SystemSpec('S07', (('control_module', 1000), ('crypto_module', 1000)), 1000, 0),
    'crypto': SystemSpec('S01', (('crypto_module', 1000),)),
    'post_quantum': SystemSpec('S05', (('chip', 2000),)),
    'listening': SystemSpec('S02', (('communication_equipment', 2000),)),
    'automated_terminal': SystemSpec('L07', (('automation_module', 2000), ('communication_equipment', 1000))),
    'launch_site': SystemSpec('N06', (('communication_equipment', 1000), ('computing_equipment', 1000)), profiles=('S3', 'S5')),
    # 04 R08: off-site research backup centre (computing + communication).
    'research_backup': SystemSpec('R08', (('computing_equipment', 1000), ('communication_equipment', 1000))),
    'big_science': SystemSpec('R09', (('computing_equipment', 2000),), 40000, 2, profiles=('S5',)),
    'closed_ecology': SystemSpec('A12', (('control_module', 2000),), 4000, 1),
    'cryptanalysis_defense': SystemSpec('C12', (('quantum_module', 1000),), 2000, 4),
    'deception_defense': SystemSpec('S06', (('cyber_suite', 1000), ('computing_equipment', 1000)), 2000, 4),
    'system_defense': SystemSpec('S08', (('cyber_suite', 2000), ('ew_module', 1000)), 4000, 4),
}


@dataclass
class InstalledSystem:
    id: str  # physical slot asset, not a second copy of its installed equipment
    owner: str
    hub: str
    host: str
    kind: str
    flags: set[str] = field(default_factory=set)
    online: bool = False
    power_received_mU: int = 0
    collapse_until_tick: int = 0


class PublicSystems:
    def __init__(self, state):
        self.state = state
        self.instances: dict[str, InstalledSystem] = {}

    @staticmethod
    def entity(sid):
        return f'sys:{sid}'

    def availability(self, system):
        assets = self.state.initial.assets
        if system.id not in assets or system.host not in assets:
            return Q(0)
        return assets[system.id].availability * assets[system.host].availability

    def operations(self):
        for system in self.instances.values():
            spec = SYSTEMS[system.kind]
            available = self.availability(system) > 0
            yield self.entity(system.id), system, spec, available

    def structural_factor(self, system):
        """Physical capacity survives a maintenance or staffing outage."""
        assets = self.state.initial.assets
        if system.id not in assets or system.host not in assets:
            return Q(0)
        return Q(assets[system.id].health_mU * assets[system.host].health_mU, 1000000)

    def update_power(self, ctx):
        frame = self.state.frame
        cash = self.state.initial.cash
        for entity, system, spec, available in self.operations():
            needed_labor = ceil(Q(spec.workers_mU_cycle, ctx.clock.cycle_ticks))
            needed_power = ceil(Q(spec.power_mU_cycle, ctx.clock.cycle_ticks))
            system.power_received_mU = frame.power_received.get(entity, 0)
            workers = frame.labor.get(entity, {})
            labor = sum(workers.values()) >= needed_labor
            wages = ceil(self.state.wage_cost(system.owner, workers))
            lock = frame.wage_locks.get(entity)
            labor &= not wages or lock is not None and cash.balance(lock).native_mU >= wages
            if system.kind == 'closed_ecology':
                hosts = {system.host}
                hosts.update(s.host for s in self.instances.values() if s.hub == system.hub and s.kind == 'water_recycle')
                if any(self.state.initial.assets[h].maintenance_debt > Q(1, 5) for h in hosts):
                    system.collapse_until_tick = max(system.collapse_until_tick, ctx.tick + 3 * ctx.clock.cycle_ticks)
            system.online = bool(available and labor and system.power_received_mU >= needed_power
                                 and ctx.tick >= system.collapse_until_tick
                                 and not (self.state.cyber_frozen_until and self.state.cyber.system_frozen(system, ctx.tick)))
            frame.worked[entity] = dict(workers) if system.online else {}
        self.reindex()

    def reindex(self):
        index = {}
        for system in sorted(self.instances.values(), key=lambda s: (not s.online, s.id)):
            row = index.setdefault(system.hub, {})
            row.setdefault(system.kind, system.id)
            for flag in system.flags:
                row.setdefault(flag, system.id)
        self.state.installed_systems = index

    def online(self, hub, kind, *, owner=None):
        return any(s.hub == hub and (s.kind == kind or kind in s.flags) and s.online and self.availability(s) > 0
                   and (owner is None or s.owner == owner) for s in self.instances.values())

    def communication_components(self, owner):
        """Actual powered C02 endpoints joined by operational road adjacency.

        A disconnected collection of radios is not a network. This read-only
        derived graph also serves future distributed-research/AI consumers.
        """
        state = self.state
        hubs = {system.hub for system in self.instances.values()
                if system.owner == owner and system.kind == 'radio_backbone'
                and system.online and self.availability(system) > 0}
        if not hubs:
            return {}
        if 'N08' in state.research[owner].known and state.orbit.coverage(owner) is not None:
            return {hub: 'orbital:' + owner for hub in hubs}
        neighbors = {hub: set() for hub in hubs}
        for edge in state.cargo.corridors.values():
            asset_id = state.cargo.road_assets.get(edge.id)
            physical = state.initial.assets.get(asset_id) if asset_id else None
            if (edge.a in hubs and edge.b in hubs and edge.health_mU > 0
                    and (physical is None or physical.health_mU > 0)):
                neighbors[edge.a].add(edge.b)
                neighbors[edge.b].add(edge.a)
        result = {}
        for hub in sorted(hubs):
            if hub in result:
                continue
            pending = [hub]
            result[hub] = hub
            while pending:
                current = pending.pop()
                for other in sorted(neighbors[current]):
                    if other not in result:
                        result[other] = hub
                        pending.append(other)
        return result

    def remove(self, sid):
        self.instances.pop(sid, None)
        self.reindex()

    def audit(self):
        for sid, system in self.instances.items():
            if (sid not in self.state.initial.assets or system.host not in self.state.initial.facilities
                    or self.state.slot_use.get(sid) != f'system:{sid}'):
                raise InvariantViolation('public system lost its exclusive physical slot')

    def snapshot(self):
        return self.instances


class SystemCommands:
    OPERATIONS = frozenset({('industry', 'install_system')})

    def __init__(self, state, industry):
        self.state, self.industry = state, industry

    def prepare(self, owner, order, ctx):
        fields(order, {'hub_id': str, 'system_id': str})
        state = self.state
        hub, kind = order['hub_id'], order['system_id']
        self.industry._owned_hub(owner, hub)
        spec = SYSTEMS.get(kind)
        if spec is None:
            raise RuleViolation('ERR_SCHEMA')
        self.industry._tech(owner, {spec.technology})
        jid, temporary = self.industry._ids(owner, order)
        existing = None
        if kind == 'post_quantum':
            existing = next((s for s in state.systems.instances.values() if s.hub == hub and s.owner == owner
                             and s.kind == 'crypto' and 'post_quantum' not in s.flags
                             and state.initial.assets[s.id].health_mU > 0), None)
            if existing is None:
                raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if kind == 'closed_ecology':
            recipes = {l.recipe.id for l in state.initial.lines.values() if l.owner == owner and l.hub == hub}
            if 'water_recycle' not in state.installed_systems.get(hub, {}) or not {'P030', 'P090'} <= recipes:
                raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if existing is None:
            existing = next((s for s in state.systems.instances.values() if s.hub == hub and s.owner == owner
                             and s.kind == kind and state.initial.assets[s.id].health_mU == 0), None)
        if existing:
            slot, host = existing.id, existing.host
            self.industry._idle(slot)
        else:
            choices = [(lid, f.id) for f in state.initial.facilities.values() if f.owner == owner and f.hub == hub
                       and f.commissioned and f.profile in spec.profiles for lid in f.line_ids
                       if state.slot_use.get(lid) == 'unassigned' and not state.initial.assets[lid].work_locked]
            if not choices:
                raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'no free compatible public slot')
            slot, host = min(choices, key=lambda pair: digest(ctx.seed, 'public_slot', ctx.tick, jid, pair[0]))
        costs = dict(spec.materials)
        asset = self.industry._asset(temporary, owner, hub, costs)
        job = WorkOrder(jid, str(int(order['id'])), owner, hub, 'install_system', slot, 2 * ctx.clock.cycle_ticks,
                        costs, temporary_asset=temporary, payload={'kind': kind, 'host': host, 'previous': existing.kind if existing else None})

        def register():
            state.initial.assets[temporary] = asset
            state.initial.assets[slot].work_locked = True
            state.initial.assets[slot].version += 1
            if existing is None:
                state.slot_use[slot] = f'building:{jid}'

        return state.work.prepare_start(job, register)

    @staticmethod
    def complete(state, industry, job):
        industry._merge_asset(job)
        if job.payload['kind'] == 'post_quantum':
            state.systems.instances[job.target].flags.add('post_quantum')
        else:
            state.systems.instances[job.target] = InstalledSystem(job.target, job.owner, job.hub, job.payload['host'], job.payload['kind'])
        state.slot_use[job.target] = f'system:{job.target}'
        state.systems.reindex()

    @staticmethod
    def cancelled(state, job):
        state.slot_use[job.target] = f'system:{job.target}' if job.target in state.systems.instances else 'unassigned'

"""Physical road work orders on explicitly validated L1 cell paths."""
from .command_schema import fields
from .construction import WorkOrder
from .errors import RuleViolation
from .logistics import Corridor
from .numeric import Q, ceil
from .world import SIZE


class CorridorCommands:
    OPERATIONS = frozenset({('logistics', 'build_corridor')})

    def __init__(self, state, industry):
        self.state, self.industry = state, industry

    def prepare(self, owner, order, ctx):
        fields(order, {'origin_hub': str, 'dest_hub': str, 'mode': str, 'level': int, 'path_cells': list})
        state, industry, ops = self.state, self.industry, self.state.cargo
        a, b = order['origin_hub'], order['dest_hub']
        start, end = industry._owned_hub(owner, a), industry._owned_hub(owner, b)
        cells, level = tuple(order['path_cells']), order['level']
        if a == b or level not in (1, 2) or not 2 <= len(cells) <= 512:
            raise RuleViolation('ERR_SCHEMA')
        if order['mode'] in ('power', 'water', 'grid', 'canal'):
            from .utility_network import UtilityConstruction
            return UtilityConstruction(state, industry).prepare(owner, order, ctx)
        if order['mode'] not in ('road', 'rail', 'river', 'sea', 'air'):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'unsupported corridor mode')
        mode = order['mode']
        if any(type(c) is not int or not 0 <= c < SIZE * SIZE for c in cells) or len(set(cells)) != len(cells):
            raise RuleViolation('ERR_SCHEMA')
        if cells[0] != start.y * SIZE + start.x or cells[-1] != end.y * SIZE + end.x:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'path endpoints differ from hubs')
        if any(abs(u % SIZE - v % SIZE) + abs(u // SIZE - v // SIZE) != 1 for u, v in zip(cells, cells[1:])):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'non-adjacent path cells')
        palette = sorted(state.initial.map.regions)
        # 04/03 L12: a strait bridge or undersea tunnel carries a road or railway
        # over at most 8 sea cells (megaproject_v1).
        sea = {c for c in cells if state.initial.map.terrain_cells[c] == 0} if mode in ('road', 'rail') else set()
        if sea:
            if len(sea) > 8:
                raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'land corridor crosses ocean')
            industry._tech(owner, {'L12'})
        if order['mode'] not in ('sea', 'air') and any(state.initial.map.regions[palette[state.initial.map.region_cells[c]]].sovereign != owner for c in cells if c not in sea):
            raise RuleViolation('ERR_NO_PERMISSION', 'path traverses uncontrolled territory')
        if mode != 'road' and level != 1:
            raise RuleViolation('ERR_SCHEMA', 'only roads have a second construction grade')
        industry._tech(owner, {'rail': {'L03'}, 'river': {'L04'},
                               'sea': {'L04', 'N04'}, 'air': {'L05'}, 'road': set()}[mode])
        if mode == 'sea':
            for hub in (start, end):
                region = state.initial.map.regions[hub.region]
                if not any(state.initial.map.regions[r].terrain == 'ocean' for r in region.adjacent):
                    raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'ports require coastal hubs')
        if mode == 'river':
            # Channelization develops a real mapped watercourse. An arbitrary
            # land path cannot be relabelled a free river by a command.
            if not any(r.mode == 'river' and r.cells in (cells, tuple(reversed(cells)))
                       for r in state.initial.map.routes.values()):
                raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'no mapped river on channelization path')
        if level == 2 and mode == 'road':
            industry._tech(owner, {'L01', 'L02'})
        existing = next((e for e, path in ops.paths.items()
                         if ops.corridors[e].mode == mode and path in (cells, tuple(reversed(cells)))), None)
        if existing is not None and ops.levels[existing] >= level:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'road already exists at this level')
        if any(j.kind == 'build_corridor' and
               j.payload.get('mode', 'road') == mode and
               (j.payload['cells'] == cells or j.payload['cells'] == tuple(reversed(cells)))
               for j in state.work.jobs.values()):
            raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'path already under construction')
        if len(ops.corridors) + sum(j.kind == 'build_corridor' for j in state.work.jobs.values()) >= state.catalog.runtime['limits']['corridors_global']:
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        distance = len(cells) - 1
        blocks = Q(distance, 10)
        coefficients = ({'stone': 4, 'cement': 2} if level == 1 else {'steel': 3, 'cement': 6, 'stone': 4}) if mode == 'road' else {
            'rail': {'steel': 8, 'cement': 6, 'basic_parts': 2},
            'river': {'cement': 6, 'steel': 3},
            'sea': {'steel': 20, 'cement': 24, 'basic_parts': 8},
            'air': {'steel': 16, 'cement': 28, 'electronic_parts': 6},
        }[mode]
        if mode in ('sea', 'air'):
            coefficients = {g: q * 2 for g, q in coefficients.items()}
        scale = Q(1) if mode in ('sea', 'air') else blocks
        materials = {g: ceil(q * 1000 * scale) for g, q in coefficients.items()}
        duration = ceil((4 if level == 1 else 8) * blocks if mode == 'road' else (12 if mode == 'rail' else 8 if mode == 'river' else 20 if mode == 'sea' else 18) * scale) * ctx.clock.cycle_ticks
        if sea:
            for good, q in {'structure_module': 40, 'steel': 80, 'cement': 120, 'metamaterial': 4}.items():
                materials[good] = materials.get(good, 0) + q * 1000
            duration = max(duration, 120 * ctx.clock.cycle_ticks)
        jid, aid = industry._ids(owner, order)
        target = existing or f'{mode}:{aid}'
        asset = industry._asset(aid, owner, a, materials)
        job = WorkOrder(jid, str(int(order['id'])), owner, a, 'build_corridor', aid, duration,
                        materials, industry._service_cost(owner, blocks), temporary_asset=aid,
                        payload={'corridor_id': target, 'a': a, 'b': b, 'cells': cells, 'level': level, 'mode': mode,
                                 **({'megaproject': 'L12_bridge'} if sea else {})})
        return state.work.prepare_start(job, lambda: state.initial.assets.__setitem__(aid, asset))

    @staticmethod
    def complete(state, job):
        if job.payload.get('mode') in ('power', 'water'):
            from .utility_network import UtilityConstruction
            return UtilityConstruction.complete(state, job)
        state.asset_lifecycle.commission(job.target)
        ops, data = state.cargo, job.payload
        eid = data['corridor_id']
        # Preserve the edge identity during a G1 -> G2 upgrade, including every
        # vehicle already traveling on it. Completion does not move the fleet.
        if eid not in ops.corridors:
            ops.corridors[eid] = Corridor(eid, data['a'], data['b'], len(data['cells']) - 1, 30000)
        ops.levels[eid] = data['level']
        ops.corridors[eid].mode = data['mode']
        ops.paths[eid] = data['cells'] if ops.corridors[eid].a == data['a'] else tuple(reversed(data['cells']))
        ops.road_assets[eid] = job.target
        ops.corridors[eid].capacity_mU_cycle = ({'road': 30000 if data['level'] == 1 else 100000,
            'rail': 320000, 'river': 180000, 'sea': 240000, 'air': 60000}[data['mode']])
        if data.get('megaproject') == 'L12_bridge':
            ops.corridors[eid].capacity_mU_cycle = 800000  # L12: 800 standard loads per cycle
            state.megaprojects[eid] = {'kind': 'L12_bridge', 'owner': job.owner, 'asset': job.target}
        if data['mode'] in ('sea', 'air'):
            # One commissioned terminal at each endpoint is required. The
            # work order paid both facilities' materials above; no free route
            # capacity exists before this completion hook.
            for hub in (data['a'], data['b']):
                ops.terminals[(hub, data['mode'])] = job.target

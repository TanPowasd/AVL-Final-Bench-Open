"""Physical industrial commands and their finite work-order lifecycle."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

from .assets import Asset
from .command_schema import fields, version
from .construction import WorkOrder
from .determinism import fingerprint
from .errors import InvariantViolation, RuleViolation
from .initialization import Facility, InitialLinePolicy, MineSite
from .ledger import Account
from .numeric import Q, ceil, floor, units
from .power import Plant, PLANTS
from .production import Line, Production, Recipe
from .protocol import PreparedCommand
from .provenance import Use


class IndustryCommands:
    OPERATIONS = frozenset(('industry', op) for op in (
        'build_facility', 'build_mine', 'warehouse_expand', 'upgrade_facility',
        'set_line', 'plant_toggle', 'repair_asset', 'demolish_facility'))

    def __init__(self, state):
        self.state = state
        state.work.on_complete = self.complete
        state.work.on_cancel = self.cancelled

    def _owned_hub(self, owner, hub):
        site = self.state.initial.map.hubs.get(hub)
        if site is None or not self.state.authority.controls_hub(owner, hub):
            raise RuleViolation('ERR_NO_PERMISSION')
        return site

    def _owned_asset(self, owner, aid, order):
        asset = self.state.initial.assets.get(aid)
        if asset is None or asset.owner != owner:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        version(order, asset)
        return asset

    def _idle(self, aid):
        if any(job.target == aid for job in self.state.work.jobs.values()):
            raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'asset already has pending work')

    def _tech(self, owner, needed):
        if not set(needed) <= self.state.research[owner].known:
            raise RuleViolation('ERR_TECH_LOCKED')

    def _slots(self, owner, hub, *, housing=False):
        state = self.state
        if sum(f.owner == owner for f in state.initial.facilities.values()) >= state.catalog.runtime['limits']['facilities_per_nation']:
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        active_houses = sum(f.hub == hub and f.kind == 'housing' and f.commissioned and state.initial.assets[f.id].availability > 0
                            for f in state.initial.facilities.values())
        capacity = floor(20 * (1 + Q(active_houses, 10)))
        occupied = sum(f.hub == hub and f.kind != 'housing' for f in state.initial.facilities.values())
        if not housing and occupied >= capacity:
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')

    def _ids(self, owner, command):
        suffix = fingerprint((owner, str(int(command['id']))))[:24]
        return f'work:{suffix}', f'asset:{suffix}'

    def _service_cost(self, owner, count=1):
        # C09: percentage points per year, not an annual fractional rate.
        interest = Q(self.state.interest_bp[owner], 100)
        cpi_ratio = min(Q(3), self.state.cpi[owner])
        return ceil(150000 * count * (1 + interest / 20) * cpi_ratio)

    def _materials(self, owner, order, costs, cycles):
        fraction = order.get('use_modular_mU', 0)
        if not 0 <= fraction <= 500:
            raise RuleViolation('ERR_SCHEMA')
        if not fraction:
            return costs, cycles
        self._tech(owner, {'L08'})
        steel, cement = costs.get('steel', 0), costs.get('cement', 0)
        if not steel or not cement:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        # Paired substitution is bounded by both component limits; even mU of
        # module makes the 1:1.5 structural conversion exact in warehouse units.
        modules = 2 * (min(floor(Q(steel * fraction, 1000)), floor(Q(cement * fraction * 2, 3000))) // 2)
        costs = dict(costs)
        costs['steel'] -= modules
        costs['cement'] -= modules * 3 // 2
        costs['structure_module'] = modules
        actual = max(Q(modules, steel), Q(modules * 3, 2 * cement))
        return costs, max(1, ceil(cycles * (1 - Q(7, 10) * actual)))

    def _asset(self, aid, owner, hub, costs):
        return Asset(aid, owner, hub, {g: q * 1000 for g, q in costs.items()},
                     book_nmU={g: Q(0) for g in costs}, under_construction=True)

    def prepare(self, owner, order, ctx):
        op = order['op']
        if op == 'build_facility':
            return self.build(owner, order, ctx)
        if op == 'build_mine':
            return self.mine(owner, order, ctx)
        if op == 'warehouse_expand':
            fields(order, {'hub_id': str, 'modules': int})
            hub, count = order['hub_id'], order['modules']
            self._owned_hub(owner, hub)
            if not 1 <= count <= 10:
                raise RuleViolation('ERR_SCHEMA')
            target = f'warehouse:{hub}'
            self._idle(target)
            jid, aid = self._ids(owner, order)
            costs = {'steel': 4000 * count, 'cement': 6000 * count}
            asset = self._asset(aid, owner, hub, costs)
            job = WorkOrder(jid, str(int(order['id'])), owner, hub, op, target, 4 * ctx.clock.cycle_ticks, costs,
                            self._service_cost(owner, count), temporary_asset=aid, payload={'modules': count})
            return self.state.work.prepare_start(job, lambda: self.state.initial.assets.__setitem__(aid, asset))
        if op == 'set_line':
            return self.set_line(owner, order, ctx)
        if op == 'upgrade_facility':
            return self.upgrade(owner, order, ctx)
        if op == 'repair_asset':
            fields(order, {'asset_id': str, 'target_health_mU': int, 'work_hub_id': str}, {'expected_version': int})
            asset = self._owned_asset(owner, order['asset_id'], order)
            self._idle(asset.id)
            self._owned_hub(owner, order['work_hub_id'])
            if asset.hub != order['work_hub_id'] or asset.under_construction:
                raise RuleViolation('ERR_SITE_INCOMPATIBLE')
            repair = asset.plan_repair(order['target_health_mU'], self.state.work.whole)
            jid, _ = self._ids(owner, order)
            job = WorkOrder(jid, str(int(order['id'])), owner, asset.hub, op, asset.id,
                repair.duration_ticks, dict(repair.materials_mU), labor_mU=repair.total_labor_mU,
                electricity_mU=repair.total_electricity_mU)

            def register():
                asset.repair = repair
                asset.version += 1

            return self.state.work.prepare_start(job, register)
        if op == 'demolish_facility':
            fields(order, {'facility_id': str}, {'expected_version': int})
            asset = self._owned_asset(owner, order['facility_id'], order)
            if asset.id not in self.state.initial.facilities and asset.id not in self.state.initial.mines:
                raise RuleViolation('ERR_SITE_INCOMPATIBLE')
            self._idle(asset.id)
            jid, _ = self._ids(owner, order)
            job = WorkOrder(jid, str(int(order['id'])), owner, asset.hub, op, asset.id, 2 * ctx.clock.cycle_ticks, {})
            def register():
                asset.work_locked = True
                asset.version += 1
            return self.state.work.prepare_start(job, register)
        fields(order, {'facility_id': str, 'online': bool}, {'expected_version': int})
        asset = self._owned_asset(owner, order['facility_id'], order)
        self._idle(asset.id)
        plant_ids = ([asset.id] if asset.id in self.state.initial.plants else
                     [lid for lid in self.state.initial.facilities.get(asset.id, Facility('', '', '', '', '', ())).line_ids
                      if lid in self.state.initial.plants])
        if not plant_ids:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')

        def toggle():
            for lid in plant_ids:
                self.state.initial.plants[lid].online = order['online']
            asset.version += 1

        return PreparedCommand(toggle)

    def build(self, owner, order, ctx):
        fields(order, {'hub_id': str, 'facility_type': str, 'profile_id': str}, {'good': str, 'use_modular_mU': int})
        profile, hub, kind = order['profile_id'], order['hub_id'], order['facility_type']
        site = self._owned_hub(owner, hub)
        if profile in ('G1', 'G2'):
            if kind != 'public':
                raise RuleViolation('ERR_SCHEMA')
            good = order.get('good')
            deposits = sorted(mid for mid, mine in self.state.resource_sites.items() if mine.hub == hub and mine.good == good)
            if not deposits:
                raise RuleViolation('ERR_SITE_INCOMPATIBLE')
            return self._mine(owner, str(int(order['id'])), hub, deposits[0], good, profile, ctx,
                              modular=order.get('use_modular_mU', 0))
        housing = profile == 'P110'
        if profile not in self.state.catalog.profiles and not housing:
            raise RuleViolation('ERR_SCHEMA')
        if 'good' in order:
            raise RuleViolation('ERR_SCHEMA')
        allowed = {r['facility'] for r in self.state.catalog.recipes.values()} | {'public'}
        if kind not in allowed or housing and kind not in ('住宅区', 'public') or not housing and kind == '住宅区':
            raise RuleViolation('ERR_SCHEMA')
        if profile in ('S2', 'S3', 'S4'):
            self._tech(owner, {'L01'})
        region = self.state.initial.map.regions[site.region]
        if kind == '农场' and region.terrain not in ('plain', 'hill'):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if kind == '船厂' and not any(self.state.initial.map.regions[r].terrain == 'ocean' for r in region.adjacent):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        self._slots(owner, hub, housing=housing)
        costs = {'cement': 20000, 'steel': 5000} if housing else {
            g: units(q) for g, q in self.state.catalog.profiles[profile]['build'].items()}
        cycles = 8 if housing else self.state.catalog.profiles[profile]['build_cycles']
        costs, cycles = self._materials(owner, order, costs, cycles)
        jid, aid = self._ids(owner, order)
        asset = self._asset(aid, owner, hub, costs)
        facility = Facility(aid, owner, hub, profile, 'housing' if housing else kind, (), False)
        job = WorkOrder(jid, str(int(order['id'])), owner, hub, 'build_facility', aid,
            cycles * ctx.clock.cycle_ticks, costs, self._service_cost(owner) if housing else 0,
            temporary_asset=aid, payload={'profile': profile, 'kind': facility.kind})

        def register():
            self.state.initial.assets[aid] = asset
            self.state.initial.facilities[aid] = facility

        return self.state.work.prepare_start(job, register)

    def mine(self, owner, order, ctx):
        fields(order, {'hub_id': str, 'deposit_id': str, 'good': str, 'level': str})
        return self._mine(owner, str(int(order['id'])), order['hub_id'], order['deposit_id'], order['good'], order['level'], ctx)

    def _mine(self, owner, command, hub, deposit, good, level, ctx, *, modular=0):
        self._owned_hub(owner, hub)
        if level not in ('G1', 'G2') or good not in self.state.catalog.runtime['extraction']:
            raise RuleViolation('ERR_SCHEMA')
        original = self.state.resource_sites.get(deposit)
        if original is None or original.hub != hub or original.good != good:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        pending = sum(j.owner == owner and j.kind == 'build_mine' for j in self.state.work.jobs.values())
        if sum(m.owner == owner for m in self.state.initial.mines.values()) + pending >= self.state.catalog.runtime['limits']['mines_per_nation']:
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        if level == 'G2':
            self._tech(owner, {'L01', 'I01'})
        costs = {'steel': 12000, 'cement': 12000, 'basic_parts': 4000} if level == 'G2' else {'steel': 4000, 'cement': 4000, 'basic_parts': 1000}
        costs, cycles = self._materials(owner, {'use_modular_mU': modular}, costs, 12 if level == 'G2' else 4)
        jid, aid = self._ids(owner, {'id': command})
        asset = self._asset(aid, owner, hub, costs)
        job = WorkOrder(jid, command, owner, hub, 'build_mine', aid, cycles * ctx.clock.cycle_ticks,
                        costs, temporary_asset=aid, payload={'deposit': deposit, 'good': good, 'level': level})
        return self.state.work.prepare_start(job, lambda: self.state.initial.assets.__setitem__(aid, asset))

    def upgrade(self, owner, order, ctx):
        fields(order, {'facility_id': str, 'target_profile': str}, {'expected_version': int})
        asset = self._owned_asset(owner, order['facility_id'], order)
        self._idle(asset.id)
        target_profile = order['target_profile']
        mine = self.state.initial.mines.get(asset.id)
        facility = self.state.initial.facilities.get(asset.id)
        if mine:
            if target_profile != 'G2' or mine.level != 'G1':
                raise RuleViolation('ERR_SCHEMA')
            self._tech(owner, {'L01', 'I01'})
            target = {'steel': 12000, 'cement': 12000, 'basic_parts': 4000}
            cycles = 8
        elif facility and facility.profile in self.state.catalog.profiles:
            if target_profile not in ('S2', 'S3', 'S4', 'S5') or target_profile <= facility.profile or not facility.commissioned:
                raise RuleViolation('ERR_SCHEMA')
            if target_profile in ('S2', 'S3', 'S4'):
                self._tech(owner, {'L01'})
            target = {g: units(q) for g, q in self.state.catalog.profiles[target_profile]['build'].items()}
            cycles = max(2, self.state.catalog.profiles[target_profile]['build_cycles'] - self.state.catalog.profiles[facility.profile]['build_cycles'])
        else:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        costs = {g: max(0, ceil(q - asset.book_nmU.get(g, Q(0)) / 1000)) for g, q in target.items()}
        jid, temporary = self._ids(owner, order)
        new_asset = self._asset(temporary, owner, asset.hub, costs)
        job = WorkOrder(jid, str(int(order['id'])), owner, asset.hub, 'upgrade_facility', asset.id,
                        cycles * ctx.clock.cycle_ticks, costs, temporary_asset=temporary, payload={'profile': target_profile})

        def register():
            asset.work_locked = True
            asset.version += 1
            self.state.initial.assets[temporary] = new_asset

        return self.state.work.prepare_start(job, register)

    def set_line(self, owner, order, ctx):
        fields(order, {'line_id': str, 'recipe': (str, type(None)), 'mode': str},
            {'high_temp': bool, 'gene_edited': bool, 'target_stock_mU': int, 'addons': list, 'expected_version': int})
        state = self.state
        lid, rid, mode = order['line_id'], order['recipe'], order['mode']
        asset = self._owned_asset(owner, lid, order)
        line = state.initial.lines.get(lid)
        if line is None:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        self._idle(lid)
        facility = state.initial.facilities[lid.rsplit('/line', 1)[0]]
        if not facility.commissioned or state.slot_use.get(lid, 'production') not in ('unassigned', 'production'):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if mode not in state.catalog.modes or order.get('target_stock_mU', 0) < 0:
            raise RuleViolation('ERR_SCHEMA')
        addons = order.get('addons', [])
        if any(type(g) is not str or g not in ('sensor', 'carbon_capture_unit', 'ai_core', 'quantum_module') for g in addons) or len(set(addons)) != len(addons):
            raise RuleViolation('ERR_SCHEMA')
        if rid is None:
            def pause():
                state.paused_lines.add(lid)
                if lid in state.initial.plants:
                    state.initial.plants[lid].online = False
                asset.version += 1
            return PreparedCommand(pause)
        row = state.catalog.recipes.get(rid)
        if row is None:
            raise RuleViolation('ERR_SCHEMA')
        self._tech(owner, row['requires_all'])
        if row['facility'] != facility.kind and facility.kind != 'public' or row['grade'] > state.catalog.profiles[facility.profile]['recipe_grade']:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        eligible = row['facility'] in state.catalog.tables['industry']['mode_eligible_facilities']
        if not eligible and mode != 'general':
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if eligible:
            self._tech(owner, state.catalog.modes[mode]['requires_all'])
        for good in addons:
            self._tech(owner, {'sensor': {'C01'}, 'carbon_capture_unit': {'V05'}, 'ai_core': {'C10'}, 'quantum_module': {'C11'}}[good])
        if 'carbon_capture_unit' in addons and rid not in state.catalog.runtime['environment']['emissions']:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if order.get('high_temp'):
            self._tech(owner, {'E08'})
            if rid not in {'P001', 'P002', 'P004', 'P005', 'P066', 'P067', 'P008', 'P014'}:
                raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if order.get('gene_edited'):
            self._tech(owner, {'A10'})
            if rid not in ('P028', 'P029'):
                raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        options = {'high_temp': order.get('high_temp', False), 'gene_edited': order.get('gene_edited', False),
                   'addons': tuple(sorted(addons)), 'target_stock_mU': order.get('target_stock_mU')}
        old = state.line_options.get(lid, {'high_temp': False, 'gene_edited': False, 'addons': (), 'target_stock_mU': None})
        mode_id = mode if eligible else None
        if rid == line.recipe.id and mode_id == state.initial.line_policies[lid].manufacturing_mode and all(options[k] == old[k] for k in ('high_temp', 'gene_edited', 'addons')):
            def update():
                state.line_options[lid] = options
                state.paused_lines.discard(lid)
                asset.version += 1
            return PreparedCommand(update)
        wanted = {g: units(q) for g, q in row['installed'].items()}
        for g, q in (state.catalog.modes[mode]['installation'].items() if eligible else ()):
            wanted[g] = wanted.get(g, 0) + units(q)
        for g in addons:
            wanted[g] = wanted.get(g, 0) + 1000
        if options['high_temp']:
            wanted['basic_parts'] = wanted.get('basic_parts', 0) + 2000
        costs = {g: max(0, ceil(q - asset.book_nmU.get(g, Q(0)) / 1000)) for g, q in wanted.items()}
        for good in self.state.work.whole:
            if good in costs and costs[good] % 1000:
                costs[good] = ceil(Q(costs[good], 1000)) * 1000
        if facility.kind == 'public':
            for g, q in state.catalog.profiles[facility.profile]['build'].items():
                costs[g] = costs.get(g, 0) + units(q)
        cycles = state.catalog.modes[mode]['switch_cycles'] if eligible else Q(2)
        if mode == 'specialized' and 'R04' in state.research[owner].known:
            cycles = Q(6)
        if 'C10' in state.research[owner].known and any('ai_core' in state.line_options.get(other.id, {}).get('addons', ())
                for other in state.initial.lines.values() if other.owner == owner and other.hub == line.hub):
            cycles /= 2
        jid, temporary = self._ids(owner, order)
        new_asset = self._asset(temporary, owner, line.hub, costs)
        job = WorkOrder(jid, str(int(order['id'])), owner, line.hub, 'set_line', lid,
            max(1, ceil(cycles * ctx.clock.cycle_ticks)), costs, temporary_asset=temporary,
            payload={'recipe': rid, 'mode': mode_id, 'options': options})

        def register():
            state.initial.assets[temporary] = new_asset
            asset.work_locked = True
            asset.version += 1

        return state.work.prepare_start(job, register)

    def _new_lines(self, facility):
        count = self.state.catalog.profiles[facility.profile]['lines']
        ids = list(facility.line_ids)
        for index in range(len(ids), count):
            lid = facility.id + ('/line' if index == 0 else f'/line{index}')
            self.state.initial.lines[lid] = Line(lid, facility.owner, facility.hub, Recipe('__idle__', {}, {}, frozenset(), frozenset()))
            self.state.initial.assets[lid] = Asset(lid, facility.owner, facility.hub, {})
            self.state.initial.line_policies[lid] = InitialLinePolicy(1000, None)
            self.state.slot_use[lid] = 'unassigned'
            self.state.paused_lines.add(lid)
            ids.append(lid)
        return replace(facility, line_ids=tuple(ids), commissioned=True)

    def _merge_asset(self, job, *, warehouse=False):
        state = self.state
        target, temporary = state.initial.assets[job.target], state.initial.assets[job.temporary_asset]
        if temporary.book_nmU != {g: Q(q) for g, q in temporary.original_nmU.items()}:
            raise InvariantViolation('cannot install incomplete work-in-progress capital')
        draft = state.initial.inventory.materials.draft()
        for good, amount in temporary.book_nmU.items():
            pieces = draft.take(state.asset_lifecycle.account(temporary, good), amount / 1000)
            destination = state.asset_lifecycle.account(target, good)
            title=state.asset_title(target.id)
            draft.deliver(state.asset_lifecycle.account(temporary,good),destination,pieces,
                          virtual=True,retitle=True,economic_owner=title)
            titled={}
            for origin,q in pieces.items():
                origin=replace(origin,economic_owner=title)
                titled[origin]=titled.get(origin,Q(0))+q
            draft.capitalize(destination,titled,use=Use.CONSTRUCTION,reason='asset_work_completed')
        state.initial.inventory.transform({}, {}, reason='asset_work_completed', material_plan=draft.finish())
        for good, amount in temporary.book_nmU.items():
            target.book_nmU[good] = target.book_nmU.get(good, Q(0)) + amount
        if warehouse:
            for good, amount in temporary.original_nmU.items():
                target.original_nmU[good] = target.original_nmU.get(good, 0) + amount
        else:
            target.original_nmU = dict(target.book_nmU)
            target.health_mU = 1000
        target.work_locked = False
        target.version += 1
        del state.initial.assets[job.temporary_asset]

    def _maintenance(self, aid, costs, *, commissioned_tick):
        state = self.state
        asset = state.initial.assets[aid]
        suppliers = {g: state.seller(asset.owner, g) for g in costs}
        if aid in state.maintenance.targets:
            old = state.maintenance.targets[aid]
            from types import MappingProxyType
            state.maintenance.targets[aid] = replace(old, materials_mU=MappingProxyType(dict(costs)),
                payer=state.operating_account(aid), suppliers=MappingProxyType(dict(old.suppliers) | suppliers))
        else:
            state.maintenance.register(aid, costs, state.operating_account(aid), suppliers, commissioned_tick=commissioned_tick)

    def complete(self, job):
        state, initial = self.state, self.state.initial
        if job.kind == 'build_corridor':
            from .corridor_commands import CorridorCommands
            CorridorCommands.complete(state, job)
            return
        if job.kind == 'install_storage':
            from .energy_storage import Store
            self._merge_asset(job)
            state.storage.stores[job.target] = Store(job.target, job.owner, job.hub, job.payload['host'], job.payload['storage_kind'])
            state.slot_use[job.target] = f'storage:{job.target}'
            return
        if job.kind == 'install_system':
            from .public_systems import SystemCommands
            SystemCommands.complete(state, self, job)
            return
        if job.kind == 'repair_asset':
            initial.assets[job.target].repair = None
            return
        if job.kind == 'demolish_facility':
            facility = initial.facilities.get(job.target)
            for lid in facility.line_ids if facility else ():
                state.systems.remove(lid)
                state.storage.remove(lid)
                research = state.research[job.owner]
                previous = research.focus.pop(lid, None)
                if previous is not None:
                    research.projects[previous].interrupt('R01' in research.known)
                for vid, validation in tuple(state.validation_jobs.items()):
                    if validation.lab_id == lid:
                        if validation.active and not validation.interrupted:
                            research.projects[validation.tech_id].validate_tick(validation.required_ticks,
                                fully_supplied=False, standard_records='R01' in research.known)
                        Production(initial.map.seed, initial.inventory).discard_and_switch(validation.line, Recipe('__idle__', {}, {}, frozenset(), frozenset()))
                        del state.validation_jobs[vid]
                Production(initial.map.seed, initial.inventory).discard_and_switch(initial.lines[lid], Recipe('__idle__', {}, {}, frozenset(), frozenset()))
                state.asset_lifecycle.dismantle(lid, event_id=f'demolish:{job.id}')
                initial.lines.pop(lid)
                initial.plants.pop(lid, None)
                initial.line_policies.pop(lid, None)
                state.slot_use.pop(lid, None)
                state.paused_lines.discard(lid)
                del initial.assets[lid]
            state.asset_lifecycle.dismantle(job.target, event_id=f'demolish:{job.id}')
            initial.facilities.pop(job.target, None)
            if job.target in initial.mines:
                Production(initial.map.seed, initial.inventory).discard_and_switch(state.extraction_lines[job.target], Recipe('__idle__', {}, {}, frozenset(), frozenset()))
                initial.mines.pop(job.target)
                state.extraction_lines.pop(job.target)
                state.extraction_sources.pop(job.target)
                state.mine_deposit.pop(job.target)
            state.maintenance.unregister(job.target)
            del initial.assets[job.target]
            return
        if job.kind == 'build_facility':
            state.asset_lifecycle.commission(job.target)
            facility = initial.facilities[job.target]
            initial.facilities[job.target] = replace(facility, commissioned=True) if facility.kind == 'housing' else self._new_lines(facility)
            costs = {'cement': 200} if facility.kind == 'housing' else {g: units(q) for g, q in state.catalog.profiles[facility.profile]['maintenance'].items()}
            self._maintenance(job.target, costs, commissioned_tick=state.asset_lifecycle.tick + 1)
            return
        if job.kind == 'build_mine':
            state.asset_lifecycle.commission(job.target)
            original = state.resource_sites[job.payload['deposit']]
            initial.mines[job.target] = MineSite(job.target, job.owner, job.hub, job.payload['good'], original.grade, job.payload['level'])
            state.mine_deposit[job.target] = job.payload['deposit']
            state.extraction_sources[job.target] = state.resource_sources[job.payload['deposit']]
        elif job.kind == 'warehouse_expand':
            target = initial.assets[job.target]
            old_capacity = 300000 + 500000 * initial.warehouse_modules[job.hub][job.target]
            added = 500000 * job.payload['modules']
            target.health_mU = (old_capacity * target.health_mU + added * 1000) // (old_capacity + added)
            self._merge_asset(job, warehouse=True)
            initial.warehouse_modules[job.hub][job.target] += job.payload['modules']
            return
        elif job.kind == 'upgrade_facility':
            self._merge_asset(job)
            if job.target in initial.mines:
                initial.mines[job.target] = replace(initial.mines[job.target], level='G2')
            else:
                facility = replace(initial.facilities[job.target], profile=job.payload['profile'])
                initial.facilities[job.target] = self._new_lines(facility)
                self._maintenance(job.target, {g: units(q) for g, q in state.catalog.profiles[facility.profile]['maintenance'].items()},
                                  commissioned_tick=state.asset_lifecycle.tick + 1)
                return
        elif job.kind == 'set_line':
            old_health_mU = initial.assets[job.target].health_mU
            self._merge_asset(job)
            line = initial.lines[job.target]
            recipe = Recipe.from_catalog(state.catalog, job.payload['recipe'])
            Production(initial.map.seed, initial.inventory).discard_and_switch(line, recipe)
            old_policy = initial.line_policies[line.id]
            fertility = state.initial.map.regions[state.initial.map.hubs[line.hub].region].fertility_mU if recipe.id in ('P028', 'P029') else None
            initial.line_policies[line.id] = InitialLinePolicy(old_policy.load_mU, job.payload['mode'], fertility)
            state.line_options[line.id] = job.payload['options']
            state.paused_lines.discard(line.id)
            state.slot_use[line.id] = 'production'
            fid = line.id.rsplit('/line', 1)[0]
            if initial.facilities[fid].kind == 'public':
                initial.facilities[fid] = replace(initial.facilities[fid], kind=state.catalog.recipes[recipe.id]['facility'])
                self._maintenance(fid, {g: units(q) for g, q in state.catalog.profiles[initial.facilities[fid].profile]['maintenance'].items()},
                                  commissioned_tick=state.asset_lifecycle.tick + 1)
            state.compute.installed(line.id, job.materials_mU,
                (state.asset_lifecycle.tick+1)//10, old_health_mU=old_health_mU)
            initial.plants.pop(line.id, None)
            if recipe.id in PLANTS:
                initial.plants[line.id] = Plant(line.id, PLANTS[recipe.id], online=True,
                                                commissioned_cycle=(state.asset_lifecycle.tick + 1) // 10)
            return
        else:
            raise InvariantViolation('unknown work completion')
        mine = initial.mines[job.target]
        rate = round(units(state.catalog.runtime['extraction'][mine.good]['output']) * mine.grade * (2 if mine.level == 'G2' else 1))
        recipe = Recipe(f'extract:{mine.good}', {mine.good: rate}, {mine.good: rate}, frozenset(), frozenset())
        if mine.id in state.extraction_lines:
            state.extraction_lines[mine.id].recipe = recipe
        else:
            state.extraction_lines[mine.id] = Line(mine.id, mine.owner, mine.hub, recipe)
        self._maintenance(mine.id, {'basic_parts': 200 if mine.level == 'G2' else 100}, commissioned_tick=state.asset_lifecycle.tick + 1)

    def cancelled(self, job):
        state = self.state
        if job.kind == 'install_storage':
            state.slot_use[job.target] = 'unassigned'
        if job.kind == 'install_system':
            from .public_systems import SystemCommands
            SystemCommands.cancelled(state, job)
        if job.kind == 'build_facility':
            state.initial.facilities.pop(job.target, None)
        if job.temporary_asset is not None:
            state.initial.assets.pop(job.temporary_asset, None)
        if job.target in state.initial.assets:
            asset = state.initial.assets[job.target]
            asset.work_locked = False
            if job.kind == 'repair_asset':
                asset.repair = None
            asset.version += 1

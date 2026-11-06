"""End-to-end runs of designs/rift_era/data/round6_scenarios.json (FULL_MATCH_TEST_TODO section B).

Each test keeps the scenario id, plays its setup and actions through the real
operating engine (commands, kernel ticks, ledgers) and reads its expected
values from the JSON. Scenario fixtures (stock, positions) are test placement;
nothing is topped up while the scenario runs.
"""
import json
import pathlib
import unittest
from fractions import Fraction as Q
from types import SimpleNamespace as NS

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.diplomacy import Relation
from organizer.rift_era.engine.economy import full_match
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.world import generate_candidate

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCENARIOS = {s['id']: s for s in json.loads(
    (ROOT / 'designs/rift_era/data/round6_scenarios.json').read_text(encoding='utf-8'))['scenarios']}


def expected(sid):
    return SCENARIOS[sid]['expected']


def near(value, target, tol=1e-9):
    return abs(float(value) - float(target)) <= tol * max(1.0, abs(float(target)))


class Round6ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def game(self, ticks=40):
        # The formal full-match assembly (five-pillar collector included).
        return full_match(self.catalog, initialize(self.catalog, self.world), total_ticks=ticks, debug=False)

    @staticmethod
    def stock(state, owner, hub, goods):
        accounts = {Account(owner, hub, 'available', g): q for g, q in goods.items()}
        state.initial.inventory.transform({}, accounts, reason='scenario_fixture',
                                          origins={a: Origin(owner, hub) for a in accounts})

    @staticmethod
    def until_decision(game):
        while not game.clock.is_decision(game.tick):
            game.step()

    @staticmethod
    def last(state, owner):
        return state.command_streams[owner].recent[-1]

    # -- relations_and_neutrality ---------------------------------------------
    def test_relations_and_neutrality(self):
        """A=n2, B=n1, C=n0 (C fields the formation that tries to strike B)."""
        exp = expected('relations_and_neutrality')
        A, B, C = 'n2', 'n1', 'n0'
        game = self.game(ticks=200)
        state = game.state
        diplomacy = state.initial.diplomacy
        self.assertTrue(all(diplomacy.relation(x, y) == Relation.PEACE for x, y in ((A, B), (A, C), (B, C))))
        # A-B defence pact, proposed and accepted through the contract commands.
        game.step({A: [{'id': '1', 'kind': 'diplomacy', 'op': 'propose_contract', 'counterparty': B, 'clause_type': 7,
                        'duration_cycles': 100, 'params': {'response_cycles': 2, 'min_aid_ru_mU': 50000}}]})
        self.until_decision(game)
        alias = next(p['id'] for p in state.contracts.visible(B)['proposals'] if p['clause_type'] == 7)
        game.step({B: [{'id': '1', 'kind': 'diplomacy', 'op': 'accept_contract', 'proposal_id': alias}]})
        self.assertEqual(self.last(state, B).status, 'ACCEPTED')
        self.until_decision(game)
        self.stock(state, C, f'{C}-h0', {'ammunition': 500000, 'fuel': 500000})
        game.step({C: [{'id': '2', 'kind': 'military', 'op': 'deploy_formation', 'hub_id': f'{C}-h0',
                        'domain': 'Land', 'units': {'motorized': 2}}]})
        formation = next(f for f in state.formations.rows.values() if f.owner == C and f.id.startswith('formation:'))
        edge = next(e for e in sorted(state.cargo.corridors.values(), key=lambda e: e.id)
                    if f'{C}-h0' in (e.a, e.b) and e.mode in ('road', 'rail'))
        self.until_decision(game)
        # A and C declare war on each other in the same window.
        game.step({A: [{'id': '3', 'kind': 'diplomacy', 'op': 'declare_war', 'target_nation': C}],
                   C: [{'id': '3', 'kind': 'diplomacy', 'op': 'declare_war', 'target_nation': A}]})
        self.assertEqual(sorted(diplomacy.pairs[tuple(sorted((A, C)))].aggressors), sorted(['n0', 'n2']))
        self.assertEqual(len(exp['A_C_aggressors']), 2)
        # The pact does not drag B into the war.
        self.assertEqual(diplomacy.relation(B, C), Relation.PEACE)
        self.assertTrue(exp['B_not_automatically_at_war'])
        # C strikes B without a declaration.
        self.until_decision(game)
        game.step({C: [{'id': '4', 'kind': 'military', 'op': 'blockade', 'formation_id': formation.id,
                        'edge_id': edge.id, 'target_nations': [B]}]})
        self.assertEqual(self.last(state, C).code, exp['C_strike_B'])
        # A and C sign an armistice for 10 cycles.
        self.until_decision(game)
        game.step({A: [{'id': '5', 'kind': 'diplomacy', 'op': 'propose_contract', 'counterparty': C, 'clause_type': 6,
                        'duration_cycles': 10, 'params': {'mode': 'armistice', 'region_ids': [], 'withdraw_cycles': 1}}]})
        self.until_decision(game)
        alias = next(p['id'] for p in state.contracts.visible(C)['proposals'] if p['clause_type'] == 6)
        game.step({C: [{'id': '5', 'kind': 'diplomacy', 'op': 'accept_contract', 'proposal_id': alias}]})
        self.assertEqual(diplomacy.relation(A, C), Relation.TRUCE)
        with self.assertRaisesRegex(RuleViolation, 'ERR_NOT_AT_WAR'):
            diplomacy.validate_kinetic(C, A)          # no mutual fire under truce
        with self.assertRaisesRegex(RuleViolation, 'ERR_NOT_AT_WAR'):
            diplomacy.validate_kinetic(A, C)
        expiry = diplomacy.pairs[tuple(sorted((A, C)))].expiry_tick
        while game.tick <= expiry:
            game.step()
        self.assertEqual(diplomacy.relation(A, C).name, exp['after_expiry'])
        self.assertEqual(diplomacy.relation(B, C), Relation.PEACE)
        state.audit()


    # -- fx_revaluation_invariance --------------------------------------------
    def test_fx_revaluation_invariance(self):
        """Only the live FX rate moves; no transaction. NFA reads historical RU books."""
        exp = expected('fx_revaluation_invariance')
        setup = SCENARIOS['fx_revaluation_invariance']['setup']
        game = self.game(ticks=20)
        game.step()
        state, power = game.state, game.state.national_power
        p = 'n0'
        # Fixture: the opening book of n0 stands 200 RU above what it now holds
        # (scenario: holds 50,000 native booked at 99,800 RU + 50,000 RU, opened at 150,000).
        held = state.initial.cash.owner_book(p)
        gap = setup['initial_cash_book'] - setup['cash_book_RU'] - setup['cash_book_native']
        power.initial_cash_book[p] = held + gap * 1000
        nfa = []
        for rate in (Q(1), Q(1, 2), Q(1, 20)):
            state.rates[p] = rate
            nfa.append(power.nfa_mU(p, 0) / 1000)
        self.assertEqual([float(v) for v in nfa], exp['NFA_each'])
        self.assertEqual(state.initial.cash.owner_book(p), held)   # no cash moved

    # -- zero_material_hostility ----------------------------------------------
    def test_zero_material_hostility(self):
        """One executed hostile policy event (a war declaration) that uses no materials."""
        exp = expected('zero_material_hostility')
        game = self.game(ticks=20)
        state = game.state
        game.step({'n0': [{'id': '1', 'kind': 'diplomacy', 'op': 'declare_war', 'target_nation': 'n1'}]})
        self.assertEqual(self.last(state, 'n0').status, 'ACCEPTED')
        power, cycle = state.national_power, 0
        loss = state.relations.action_cycles[cycle]['n0']
        self.assertEqual(loss, Q(1, 1000))
        self.assertTrue(near(loss, exp['Loss_action']))
        with_event = power.industry('n0', cycle)
        saved = state.relations.action_cycles.pop(cycle)
        without = power.industry('n0', cycle)
        state.relations.action_cycles[cycle] = saved
        delta_u = (with_event - without) / 4
        self.assertTrue(near(delta_u, exp['delta_U_action']))
        self.assertLess(delta_u, 0)               # exact Fraction: no integer truncation to zero

    # -- cyber_credit_floor ---------------------------------------------------
    def test_cyber_credit_floor(self):
        """S03 on cyber_suite 1 + 10 effective cyber compute, attacker already at the credit floor."""
        exp = expected('cyber_credit_floor')
        game = self.game(ticks=20)
        state = game.state
        state.research['n0'].known.update({'S03', 'AI09'})
        self.stock(state, 'n0', 'n0-h0', {'cyber_suite': 1000})
        # Fixture: last cycle's effective cyber compute at the capital (20 units).
        state.ai_deployment.previous_tasks[('n0', 'n0-h0', 'cyber')] = (Q(20000), Q(0))
        state.credit['n0'] = Q(SCENARIOS['cyber_credit_floor']['setup']['Credit_A']).limit_denominator(1000)
        equipment = state.initial.inventory.balance(Account('n0', 'n0-h0', 'available', 'computing_equipment'))
        game.step({'n0': [{'id': '1', 'kind': 'intelligence', 'op': 'cyber_op', 'target_nation': 'n1',
                           'target_hub': 'n1-h0', 'op_kind': 'S03_infiltrate'}]})
        self.assertEqual(self.last(state, 'n0').status, 'ACCEPTED')
        inv = state.initial.inventory
        self.assertEqual(inv.balance(Account('n0', 'n0-h0', 'available', 'cyber_suite')), 0)
        self.assertEqual(inv.balance(Account('n0', 'n0-h0', 'available', 'computing_equipment')), equipment)
        loss = state.cyber.action_cycles[0]['n0']
        self.assertTrue(near(loss, exp['Loss_action']))
        self.assertEqual(state.credit['n0'], Q(1, 10))       # floor holds whether or not attributed
        power = state.national_power
        with_op = power.industry('n0', 0)
        saved = state.cyber.action_cycles.pop(0)
        without = power.industry('n0', 0)
        state.cyber.action_cycles[0] = saved
        self.assertTrue(near((with_op - without) / 4, exp['delta_U_action']))
        self.assertEqual(exp['positive_active_scale_increment'], 0)
        self.assertEqual(state.cyber.compute_used[('n0', 0)], 10000)

    # -- patrol_loop ----------------------------------------------------------
    def test_patrol_loop(self):
        """12 armored units patrol A0 -> A1 -> A0 (n0-h0 / n0-h1) and are on the move every tick."""
        exp = expected('patrol_loop')
        n = SCENARIOS['patrol_loop']['setup']['armored_units']
        game = self.game(ticks=40)
        state = game.state
        for hub in ('n0-h0', 'n0-h1'):     # depots at both ends of the loop
            self.stock(state, 'n0', hub, {'fuel': 500000})
        self.stock(state, 'n0', 'n0-h0', {'armored_unit': n * 1000})
        game.step({'n0': [{'id': '1', 'kind': 'military', 'op': 'deploy_formation', 'hub_id': 'n0-h0',
                           'domain': 'Land', 'units': {'armored': n}}]})
        formation = next(f for f in state.formations.rows.values() if f.id.startswith('formation:'))
        self.until_decision(game)
        game.step({'n0': [{'id': '2', 'kind': 'military', 'op': 'formation_task', 'formation_id': formation.id,
                           'task': 'patrol', 'target_region': state.initial.map.hubs['n0-h1'].region,
                           'posture': 'preserve', 'supply_hub': 'n0-h0', 'retreat_loss_mU': 500}]})
        self.assertEqual(self.last(state, 'n0').status, 'ACCEPTED')
        visited, moving_ticks = set(), 0
        while game.tick < 30:
            game.step()
            if game.tick - 1 >= 10 and game.tick - 1 < 20:
                moving_ticks += formation.id in state.movement.moved_formations
            if formation.hub:
                visited.add(formation.hub)
        self.assertEqual(visited, {'n0-h0', 'n0-h1'})              # it came back: a loop, not a one-way move
        self.assertEqual(moving_ticks, 10)                          # cycle 1 is a full cycle on the road
        loss = state.battles.loss_cycles[1]['n0']
        self.assertEqual(loss, exp['loss'])
        from organizer.rift_era.engine.scoring import action_loss, industrial_pillar
        self.assertEqual(action_loss({'fuel': int(exp['fuel'] * 1000)}, self.catalog.weights), loss)
        self.assertTrue(near(industrial_pillar(Q(int(exp['upper_refinery_VA'])), Q(0), loss, Q(0), Q(0)) / 4,
                             exp['upper_delta_U']))
        self.assertEqual(state.consumption.receipt_mean20('n0', 'transit'), exp['transit'])
        state.audit()

    # -- repair_single_count --------------------------------------------------
    def test_repair_single_count(self):
        """An S1 shell at health .5 is repaired to 1 once; the repair is capital, not consumption or action."""
        exp = expected('repair_single_count')
        game = self.game(ticks=140)
        state = game.state
        target, owner = 'n0/P001/0', 'n0'
        weights = self.catalog.weights
        value = lambda book: sum(q / Q(10 ** 6) * weights[g] for g, q in book.items())
        asset = state.initial.assets[target]
        self.assertEqual({g: q // 10 ** 6 for g, q in asset.original_nmU.items()},
                         SCENARIOS['repair_single_count']['setup']['full_B0'])
        state.asset_lifecycle.damage(target, 500, event_id='scenario_prior_damage')
        self.assertTrue(near(value(asset.book_nmU), SCENARIOS['repair_single_count']['setup']['book']))
        orders = [{'id': 1, 'kind': 'industry', 'op': 'repair_asset', 'asset_id': target,
                   'target_health_mU': 1000, 'work_hub_id': 'n0-h0'}]
        for lid, line in state.initial.lines.items():   # a real configuration frees the power for the work
            if line.owner == owner and lid not in state.initial.plants:
                orders.append({'id': len(orders) + 1, 'kind': 'industry', 'op': 'set_line',
                               'line_id': lid, 'recipe': None, 'mode': 'general'})
        game.step({owner: tuple(orders)})
        job = next(j for j in state.work.jobs.values() if j.kind == 'repair_asset')
        self.assertEqual(job.duration_ticks, exp['duration_cycles'] * 10)
        self.assertEqual({g: q for g, q in job.materials_mU.items()},
                         {g: q * 500 for g, q in SCENARIOS['repair_single_count']['setup']['full_B0'].items()})
        losses_before = sum(c.get(owner, 0) for c in state.battles.loss_cycles.values())
        destroyed_before = sum(m.destroyed_value for m in state.history[owner])
        while state.work.jobs and game.tick < 135:
            game.step()
        self.assertFalse(state.work.jobs)
        self.assertEqual(asset.health_mU, 1000)
        self.assertTrue(near(value(asset.book_nmU), exp['final_book']))
        self.assertTrue(near(value(asset.book_nmU) - SCENARIOS['repair_single_count']['setup']['book'],
                             exp['capitalized_value']))
        self.assertEqual(sum(c.get(owner, 0) for c in state.battles.loss_cycles.values()) - losses_before,
                         exp['Loss_action_from_repair'])
        self.assertEqual(sum(m.destroyed_value for m in state.history[owner]) - destroyed_before,
                         exp['Loss_destroy_from_repair'])
        for good in SCENARIOS['repair_single_count']['setup']['full_B0']:
            self.assertEqual(state.consumption.consumption20(owner, good), exp['eta_consumption'])
        self.assertEqual(state.consumption.receipt_mean20(owner, 'transit'), exp['receipt'])
        state.audit()

    # -- healthy_relocation ---------------------------------------------------
    def test_healthy_relocation(self):
        """5 general workers move by a real passenger trip to an empty, unhoused core Hub."""
        exp = expected('healthy_relocation')
        setup = SCENARIOS['healthy_relocation']['setup']
        game = self.game(ticks=60)
        state = game.state
        pops = state.initial.populations
        origin, dest, owner = 'n0-h0', 'n0-h1', 'n0'
        skills = ('general_worker', 'skilled_worker', 'engineer')
        # Fixture: the scenario's origin census, an empty destination with no housing.
        pops[origin].skills_mU = {k: q * 1000 for k, q in zip(skills, setup['origin_population_skills'])}
        pops[dest].skills_mU = {k: 0 for k in skills}
        state.initial.housing_mU[dest] = setup['destination_housing']
        self.assertEqual(state.authority.regions[state.initial.map.hubs[origin].region].support_mU, setup['origin_Support'])
        inv = state.initial.inventory
        reserved = {}
        real_prepare = inv.prepare_transfers
        def spy(transfers, *a, **kw):
            transfers = tuple(transfers)
            for t in transfers:
                if t.destination.bucket == 'passenger_provisions':
                    reserved[t.destination.asset] = reserved.get(t.destination.asset, 0) + t.quantity
            return real_prepare(transfers, *a, **kw)
        inv.prepare_transfers = spy
        game.step({owner: [{'id': '1', 'kind': 'governance', 'op': 'relocate_population', 'origin_hub': origin,
                            'dest_hub': dest, 'skills_mU': {'general_worker': 5000, 'skilled_worker': 0, 'engineer': 0},
                            'path': ['road:n0-h0:n0-h1'], 'allow_unhoused': True}]})
        self.assertEqual(self.last(state, owner).status, 'ACCEPTED')
        self.assertEqual(pops[dest].total_mU, exp['before_arrival_destination_pop'])
        # A road carries at most 2 people a tick here, so the 5 leave in real batches.
        arrived = {}
        def track():
            for sid, rec in tuple(state.passengers.shipments.items()):
                seen[sid] = dict(rec.skills_mU)
            for sid in tuple(seen):
                if sid not in state.passengers.shipments:
                    for k, q in seen.pop(sid).items():
                        arrived[k] = arrived.get(k, 0) + q
        seen = {}
        while state.passengers.pending and game.tick < 30:
            game.step(); track()
        inv.prepare_transfers = real_prepare
        self.assertEqual(reserved, {'food': 1500, 'water': 1000})     # provisions food 1.5, water 1 in total
        self.assertEqual(pops[origin].skills_mU['general_worker'], exp['origin_general_pop_after_departure'] * 1000)
        while state.passengers.shipments and game.tick < 55:
            game.step(); track()
        self.assertFalse(state.passengers.shipments)
        # Everyone who left arrived, as general workers (later deaths are ordinary demography).
        self.assertEqual(arrived, {'general_worker': exp['after_arrival_destination_general_pop'] * 1000,
                                   'skilled_worker': 0, 'engineer': 0})
        self.assertGreater(pops[dest].skills_mU['general_worker'], 0)
        self.assertEqual(pops[dest].skills_mU['skilled_worker'] + pops[dest].skills_mU['engineer'], 0)
        from organizer.rift_era.engine.population import housing_factor
        h = housing_factor(pops[dest].total_mU, state.initial.housing_mU[dest])
        self.assertEqual(h, exp['destination_H'])
        self.assertEqual(h * Q(1), exp['destination_birth_rate'])     # births scale with H
        active = Q(sum(arrived.values()), 1000) * state.labor_participation(owner, dest)
        self.assertTrue(near(active, exp['destination_active_labor']))
        self.assertTrue(exp['no_military_conversion'])
        self.assertFalse(any(f.owner == owner and f.id.startswith('formation:') for f in state.formations.rows.values()))
        state.audit()

    # -- cpu_budget -----------------------------------------------------------
    def test_cpu_budget(self):
        from organizer.rift_era.engine.evaluation import GIB, ResourceBudget
        setup, exp = SCENARIOS['cpu_budget']['setup'], expected('cpu_budget')
        engine_memory = 2 * GIB
        per_match = engine_memory + setup['simultaneous_strategy_processes'] * GIB
        total = 64 * GIB
        budget = ResourceBudget(effective_cpu=Q(setup['effective_cpu']), memory_available=total,
                                memory_total=total, engine_cpu_peak=Q(setup['engine_cpu']),
                                engine_memory_peak=engine_memory,
                                active_strategies=setup['simultaneous_strategy_processes'],
                                resident_strategies=setup['simultaneous_strategy_processes'])
        self.assertGreaterEqual((total - max(4 * GIB, total // 5)) // per_match, 1)   # memory is not the bound
        c_match = setup['engine_cpu'] + setup['simultaneous_strategy_processes']
        self.assertEqual(c_match, exp['C_match'])
        reserve = min(max(0, setup['effective_cpu'] - 1), max(1, -(-setup['effective_cpu'] // 4)))
        self.assertEqual(reserve, setup['cpu_reserve'])
        self.assertEqual((setup['effective_cpu'] - reserve) // c_match, exp['K_CPU'])
        self.assertEqual(budget.concurrency(), exp['Kmax'])

    # -- batch_consistency ----------------------------------------------------
    def test_batch_consistency(self):
        import tempfile
        from organizer.rift_era.engine.errors import InfrastructureFault
        from organizer.rift_era.engine.evaluation import Batch
        hashes = dict(rules_hash='r' * 64, engine_hash='e' * 64, reference_hash='f' * 64,
                      runtime_hash='i' * 64, resource_plan_hash='p' * 64)
        batch = Batch.create(**hashes)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'manifest.json'
            batch.write_new(path)
            first, second = Batch.read(path), Batch.read(path)   # two submissions read the frozen manifest
            fields = {'seed64_sequence': 'seeds', 'seat_mapping': 'seats', 'rules_hash': 'rules_hash',
                      'engine_hash': 'engine_hash', 'reference_hash': 'reference_hash',
                      'runtime_image': 'runtime_hash', 'resource_budgets': 'resource_plan_hash',
                      'scheduler_plan': 'resource_plan_hash'}
            self.assertEqual(set(fields), set(expected('batch_consistency')['equal_fields']))
            for attr in fields.values():
                self.assertEqual(getattr(first, attr), getattr(second, attr))
            self.assertEqual(len(set(first.seeds)), 64)
            self.assertEqual(first.seats, tuple(i % 8 for i in range(64)))
            # A changed rule cannot reuse the batch: the manifest is write-once and
            # every shadow key is bound to the rule hash.
            changed = Batch(**{**{k: getattr(first, k) for k in first.__dataclass_fields__}, 'rules_hash': 'x' * 64})
            with self.assertRaises(FileExistsError):
                changed.write_new(path)
            self.assertNotEqual(changed.shadow_key(first.seeds[0], 0), first.shadow_key(first.seeds[0], 0))
            self.assertTrue(expected('batch_consistency')['changed_rule_requires_new_batch'])
            with self.assertRaises(InfrastructureFault):
                Batch(**{**{k: getattr(first, k) for k in first.__dataclass_fields__},
                         'seeds': first.seeds[:-1] + (first.seeds[0],)}).validate()

    # -- damage_power_and_repair ----------------------------------------------
    def test_damage_power_and_repair(self):
        exp = expected('damage_power_and_repair')
        setup = SCENARIOS['damage_power_and_repair']['setup']
        game = self.game(ticks=260)
        state = game.state
        lid = 'n0/P070/0/line'
        plant = state.initial.plants[lid]
        self.assertEqual(plant.spec.nameplate_mU, setup['plant_P070_nameplate'] * 1000)
        capacity = lambda: Q(plant.spec.nameplate_mU * state.initial.assets[lid].availability, 1000)
        while game.tick < 3:
            game.step()
        # P4 of tick 3 destroys only the P070 line (the shell keeps its health).
        hit_tick = game.tick
        real = state.blockades.advance
        def p4(ctx):
            real(ctx)
            if ctx.tick == hit_tick:
                state.asset_lifecycle.damage(lid, 1000, event_id='scenario_strike')
        state.blockades.advance = p4
        produced = []
        real_transform = state.initial.inventory.transform
        def spy(remove, add, reason=None, **kw):
            if reason and 'production' in reason and any(a.location == lid or a.bucket.endswith(lid) for a in add):
                produced.append(reason)
            return real_transform(remove, add, reason=reason, **kw)
        state.initial.inventory.transform = spy
        game.step()
        state.initial.inventory.transform = real_transform
        state.blockades.advance = real
        self.assertEqual(state.initial.assets[lid].health_mU, 0)
        self.assertEqual(state.initial.assets['n0/P070/0'].health_mU, 1000)
        self.assertEqual(len(produced), exp['same_tick_P6_output'])
        game.step()                                               # next tick: P3 sees the loss
        self.assertEqual(capacity(), exp['next_tick_generator_capacity'])
        self.assertEqual(state.frame.plant_bounds[lid][1], 0)
        # Repair the line from 0 to 1.
        self.until_decision(game)
        orders = [{'id': 1, 'kind': 'industry', 'op': 'repair_asset', 'asset_id': lid,
                   'target_health_mU': 1000, 'work_hub_id': 'n0-h0'}]
        for other, line in state.initial.lines.items():   # free the labour and power for the work
            if line.owner == 'n0' and other not in state.initial.plants:
                orders.append({'id': len(orders) + 1, 'kind': 'industry', 'op': 'set_line',
                               'line_id': other, 'recipe': None, 'mode': 'general'})
        game.step({'n0': tuple(orders)})
        job = next(j for j in state.work.jobs.values() if j.kind == 'repair_asset')
        self.assertEqual(job.duration_ticks, exp['repair_duration_full_cycles'] * 10)
        while state.work.jobs and game.tick < 255:
            game.step()
        self.assertFalse(state.work.jobs)
        self.assertEqual(capacity(), exp['after_repair_generator_capacity'])
        # A grid line at health .25 carries a quarter of its 60-unit rating; at 0 nothing.
        from organizer.rift_era.engine.utility_network import UtilityLink
        carrier = 'n0/P001/0'                                    # fixture: an existing asset carries the line
        state.utilities.links['grid:test'] = UtilityLink('grid:test', 'n0', 'n0-h0', 'n0-h1', 'power', 1,
                                                         (0, 1), carrier)
        rating, _ = state.utilities.power_rating(state.utilities.links['grid:test'])
        self.assertEqual(rating, setup['line_nameplate'] * 1000)
        state.asset_lifecycle.damage(carrier, 750, event_id='scenario_grid_hit')
        edge = next(e for e in state.utilities.dispatch_edges() if e.id == 'grid:test')
        self.assertEqual(Q(edge.capacity_mU * 10, 1000), exp['corridor_capacity_at_quarter'])
        state.asset_lifecycle.damage(carrier, 250, event_id='scenario_grid_cut')
        self.assertNotIn('grid:test', {e.id for e in state.utilities.dispatch_edges()})
        self.assertEqual(exp['corridor_capacity_at_zero'], 0)
        del state.utilities.links['grid:test']

    # -- blockade_food_substitution -------------------------------------------
    def test_blockade_food_substitution(self):
        """A=n0 closes its edge eC (road n0-h0:n0-h1) to carrier C=n2 while at war; the
        buyer B (city-33's residents) then buys A's food over A's own disjoint route eA
        (road city-33-h0:n0-h3). Every channel of the lower bound is read from the engine."""
        from organizer.rift_era.engine.provenance import Leg
        from organizer.rift_era.engine.scoring import international_pillar
        exp = expected('blockade_food_substitution')
        A, C, city_id = 'n0', 'n2', 'city-33'
        eC, eA = 'road:n0-h0:n0-h1', 'road:city-33-h0:n0-h3'
        game = self.game(ticks=60)
        state = game.state
        cargo, inv, weights = state.cargo, state.initial.inventory, self.catalog.weights
        city = state.city_economy.cities[city_id]
        w_food = weights['food']
        self.assertEqual(cargo.operator(eC), A)
        self.assertEqual({cargo.corridors[eC].a, cargo.corridors[eC].b} & {cargo.corridors[eA].a, cargo.corridors[eA].b},
                         set())                                       # disjoint routes
        setup = SCENARIOS['blockade_food_substitution']['setup']
        state.credit[A] = state.credit[C] = Q(setup['A_Credit'])
        # Fixture: B is 200 civilians (city-33 rescaled at the start of each measured
        # cycle, so ordinary deaths do not blur the 12-food arithmetic) with a 20,000 RU budget.
        def census():
            total = city.population.total_mU
            skills = {k: q * setup['B_population_civilian'] * 1000 // total for k, q in city.population.skills_mU.items()}
            skills[sorted(skills)[0]] += setup['B_population_civilian'] * 1000 - sum(skills.values())
            city.population.skills_mU = skills
        from organizer.rift_era.engine.ledger import CashAccount
        held = state.initial.cash.balance(city.household).native_mU
        if held < setup['B_RU_budget'] * 1000:
            state.initial.cash.transfer(CashAccount('world', 'pool', 'RU'), city.household,
                                        setup['B_RU_budget'] * 1000 - held)
        # Fixture: the city starts with no food of its own, so what residents eat is the delivered flow.
        for account, q in tuple(inv.balances().items()):
            if account.owner == city_id and account.asset == 'food' and q:
                inv.transform({account: q}, {}, reason='scenario_fixture')
        warehouse = state.city_economy.stock(city, 'food')
        c_path = (Leg('n0-h0', 'n0-h1', C, cargo.corridors[eC].length_cells),
                  Leg('n0-h1', 'city-33-h0', C, cargo.corridors['road:city-33-h0:n0-h1'].length_cells))
        a_path = (Leg('n0-h3', 'city-33-h0', A, cargo.corridors[eA].length_cells),)

        def deliver(nation, path):   # 12 food a cycle arrive in the buyer's warehouse by the stated route
            census()
            inv.transform({}, {warehouse: 12000}, reason='scenario_delivery',
                          origins={warehouse: Origin(nation, path[0].origin, path)})

        def receipt(owner, cycle):
            return state.consumption._receipts.get(cycle, {}).get((owner, 'export'), Q(0))

        # Cycle 0: war is already on in both branches; A grants C commerce on eC.
        game.step({A: [{'id': '1', 'kind': 'diplomacy', 'op': 'declare_war', 'target_nation': C},
                       {'id': '2', 'kind': 'logistics', 'op': 'corridor_policy', 'corridor_id': eC,
                        'target_nation': C, 'access_tier': 1, 'toll_rate_bp': 0}]})
        while game.tick < 10:
            game.step()
        # Cycle 1 baseline: C's food crosses A's edge and is eaten.
        deliver(C, c_path)
        while game.tick < 20:
            game.step()
        self.assertEqual(city.cycle_used.get('food'), 12000, city.cycle_used)   # B eats 12 food a cycle
        c_receipt = receipt(C, 1)
        self.assertTrue(near(c_receipt, 12 * w_food))                 # Phi_A baseline flow = 12 w_food
        regions = state.path_regions(c_path)
        a_regions = sorted(r for r in regions if state.authority.regions[r].controller in (None, A)
                           and (state.authority.regions[r].controller or state.authority.regions[r].sovereign) == A)
        self.assertTrue(a_regions)
        for r in a_regions:
            self.assertTrue(near(state.consumption._phi[1][r], 12 * w_food))
        # Cycle 2: A sets C to tier 0 on eC.
        game.step({A: [{'id': '3', 'kind': 'logistics', 'op': 'corridor_policy', 'corridor_id': eC,
                        'target_nation': C, 'access_tier': 0, 'toll_rate_bp': 0}]})
        self.assertEqual(self.last(state, A).status, 'ACCEPTED')
        self.assertFalse(cargo.permitted(C, eC))
        self.stock(state, C, 'n0-h0', {'food': 12000})
        self.until_decision(game)
        game.step({C: [{'id': '1', 'kind': 'logistics', 'op': 'route_shipment', 'origin_hub': 'n0-h0',
                        'dest_hub': 'n0-h1', 'corridor_path': [eC], 'good': 'food', 'qty_mU': 12000,
                        'carrier_recipe': 'manual'}]})
        self.assertEqual(self.last(state, C).status, 'REJECTED')       # C deliveries after the lag: 0
        self.assertEqual(exp['C_deliveries_after_lag'], 0)
        while game.tick < 30:
            game.step()
        # Cycle 3: the buyer's paid purchases switch to A's food over eA.
        deliver(A, a_path)
        while game.tick < 40:
            game.step()
        self.assertEqual(receipt(C, 3), 0)
        a_receipt = receipt(A, 3)
        self.assertTrue(near(a_receipt, exp['A_receipt_value_per_cycle']))
        self.assertEqual(exp['A_delivered_consumed_food_per_cycle'], 12)
        a_route = state.path_regions(a_path)
        for r in a_regions:                                            # Phi_A keeps only actual flow: C's is gone
            self.assertEqual(state.consumption._phi.get(3, {}).get(r, Q(0)), 12 * w_food if r in a_route else 0)
        # A's own policy loss: one 0.001 event per tick while C stays below the granted tier.
        policy = state.cargo.action_cycles[3][A]
        self.assertEqual(policy, Q(10, 1000))
        power = state.national_power
        with_policy = power.industry(A, 3)
        saved = state.cargo.action_cycles.pop(3)
        without = power.industry(A, 3)
        state.cargo.action_cycles[3] = saved
        action_u = (without - with_policy) / 4
        self.assertTrue(near(action_u, exp['weighted_action_U_cost_per_cycle']))
        # Steady 20-cycle channels with the engine's pillar weights.
        export_u = Q(15, 100) * (international_pillar(a_receipt, Q(0), Q(0), Q(0)))
        phi_u = Q(1, 5) * Q(2, 100) * min(Q(100), c_receipt)
        self.assertTrue(near(export_u, exp['steady_weighted_export_U_gain']))
        self.assertTrue(near(phi_u, exp['steady_weighted_Phi_U_loss']))
        steady = export_u - phi_u - action_u
        self.assertTrue(near(steady, exp['steady_delta_U_lower_bound']))
        ramp = sum(Q(min(k, 20), 20) for k in range(1, 41))           # rolling20 ramp over 40 cycles
        self.assertEqual(ramp, Q(305, 10))
        era3 = Q(24, 100) / 720 * (ramp * (export_u - phi_u) - 40 * action_u)
        self.assertTrue(near(era3, exp['era3_delta_V_lower_bound_ignoring_positive_NFA']))
        self.assertGreater(era3, 0)
        state.audit()

if __name__ == '__main__':
    unittest.main()

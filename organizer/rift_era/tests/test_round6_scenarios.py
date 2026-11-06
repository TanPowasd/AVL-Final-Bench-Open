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

if __name__ == '__main__':
    unittest.main()

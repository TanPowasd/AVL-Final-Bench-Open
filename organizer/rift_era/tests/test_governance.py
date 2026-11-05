import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.governance import GovernanceCommands
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.kernel import TickContext
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.population import Population, Demography, grow
from organizer.rift_era.engine.public_systems import InstalledSystem
from organizer.rift_era.engine.world import generate_candidate


class GovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def fixture(self):
        game = opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=20,
                               commerce=True, debug=False)
        return game, game.state, GovernanceCommands(game.state)

    @staticmethod
    def context(tick=0):
        return TickContext(1000, tick, ClockConfig(), {})

    def test_tax_is_cycle_frozen_and_autonomy_divides_actual_receipts(self):
        game, state, _ = self.fixture()
        region = self.world.hubs['n0-h0'].region
        game.step({'n0': ({'id': 1, 'kind': 'governance', 'op': 'fiscal_policy', 'tax_rate_bp': 500,
                          'welfare_budget_mU': 0, 'maint_budget_mU': 0},
                         {'id': 2, 'kind': 'governance', 'op': 'autonomy', 'region_id': region, 'autonomy_mU': 500})})
        self.assertNotEqual(state.tax['n0'], Q(5, 100))
        for _ in range(10):
            game.step()
        self.assertEqual(state.tax['n0'], Q(5, 100))
        self.assertGreater(state.initial.cash.balance(state.native('n0', 'public:n0-h0')).native_mU, 0)
        state.audit()

    def test_mobilization_lock_and_two_cycle_ramp(self):
        _, state, commands = self.fixture()
        order = {'id': 1, 'kind': 'governance', 'op': 'mobilize', 'enabled': True}
        commands.prepare('n0', order, self.context()).commit()
        policy = state.governance.policies['n0']
        state.governance.end_cycle(self.context(9))
        self.assertEqual(policy.mobilization, Q(1, 2))
        state.governance.end_cycle(self.context(19))
        self.assertEqual(policy.mobilization, 1)
        with self.assertRaises(RuleViolation):
            commands.prepare('n0', order | {'enabled': False}, self.context(39))
        self.assertTrue(policy.mobilized)
        commands.prepare('n0', order | {'enabled': False}, self.context(40)).commit()
        for tick in (49, 59):
            state.governance.end_cycle(self.context(tick))
        self.assertEqual(policy.mobilization, 0)
        self.assertEqual(policy.fatigue, Q(2, 100))

    def test_training_keeps_residents_and_food_demand_but_removes_workers(self):
        _, state, commands = self.fixture()
        state.research['n0'].known.add('R02')
        # Component fixture supplies a physical, commissioned academy slot.
        host = next(f for f in state.initial.facilities.values() if f.owner == 'n0' and f.line_ids)
        sid = host.line_ids[0]
        state.systems.instances[sid] = InstalledSystem(sid, 'n0', host.hub, host.id, 'academy', online=True)
        state.slot_use[sid] = f'system:{sid}'
        state.systems.reindex()
        pop = state.initial.populations[host.hub]
        before, workers, skilled = pop.total_mU, pop.skills_mU['general_worker'], pop.skills_mU['skilled_worker']
        order = {'id': 1, 'kind': 'governance', 'op': 'academy_train', 'hub_id': host.hub,
                 'track': 'general_to_skilled', 'trainees_mU': 5000}
        stock = Account('n0', host.hub, 'available', 'lab_supplies')
        supplied = state.initial.inventory.balance(stock)
        commands.prepare('n0', order, self.context()).commit()
        self.assertEqual(pop.total_mU, before)
        self.assertEqual(pop.skills_mU['general_worker'], workers - 5000)
        with self.assertRaises(RuleViolation):
            commands.prepare('n0', order | {'id': 2, 'track': 'skilled_to_engineer'}, self.context())
        self.assertEqual(state.initial.inventory.balance(stock), supplied - 1000)
        state.systems.instances[sid].online = False
        state.initial.inventory.materials.begin_tick(0)
        state.governance.advance_training(self.context())
        self.assertEqual(next(iter(state.governance.training.values())).elapsed, 0)
        state.systems.instances[sid].online = True
        for tick in range(1, 61):
            state.initial.inventory.materials.begin_tick(tick)
            state.governance.advance_training(self.context(tick))
            state.governance.audit()
        self.assertFalse(state.governance.training)
        self.assertEqual(pop.total_mU, before)
        self.assertEqual(pop.skills_mU['skilled_worker'], skilled + 5000)
        state.initial.inventory.audit()
        state.initial.cash.audit()

    def test_training_population_shares_mortality_without_duplication(self):
        population = Population('h', {'general_worker': 5000}, training_mU={'course': 5000})
        before = population.total_mU
        births, deaths = grow(1, 0, population, Demography(Q(0), 100000, Q(0), Q(0), Q(0), Q(0), Q(0)))
        self.assertEqual(population.total_mU, before + births - deaths)
        self.assertLess(population.training_mU['course'], 5000)

    def test_training_cannot_take_people_already_reserved_for_life_support(self):
        _, state, commands = self.fixture()
        state.research['n0'].known.add('R02')
        host = next(f for f in state.initial.facilities.values() if f.owner == 'n0' and f.line_ids)
        sid = host.line_ids[0]
        state.systems.instances[sid] = InstalledSystem(sid, 'n0', host.hub, host.id, 'academy', online=True)
        state.slot_use[sid] = f'system:{sid}'
        state.systems.reindex()
        workers = state.initial.populations[host.hub].skills_mU['general_worker']
        capacity_after = int(Q(workers - 5000, 10) * state.labor_participation('n0', host.hub))
        state.frame.labor['protected'] = {'general_worker': capacity_after + 1}
        state.frame.work_context['protected'] = ('n0', host.hub, 'treasury')
        before = fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation) as rejected:
            commands.prepare('n0', {'id': 1, 'kind': 'governance', 'op': 'academy_train', 'hub_id': host.hub,
                'track': 'general_to_skilled', 'trainees_mU': 5000}, self.context())
        self.assertEqual(rejected.exception.code, 'ERR_CAPACITY_EXCEEDED')
        self.assertEqual(fingerprint(state.snapshot()), before)

    def test_welfare_is_reserved_cash_and_regime_effect_waits_transition(self):
        _, state, commands = self.fixture()
        state.research['n0'].known.update(('F05', 'F07'))
        commands.prepare('n0', {'id': 1, 'kind': 'governance', 'op': 'fiscal_policy', 'tax_rate_bp': 2000,
                               'welfare_budget_mU': 4000, 'maint_budget_mU': 0}, self.context()).commit()
        before = state.initial.cash.balance(state.native('n0', 'treasury')).native_mU
        state.governance.begin_tick(self.context())
        self.assertEqual(state.initial.cash.balance(state.native('n0', 'treasury')).native_mU, before - 4000)
        state.governance.settle_welfare(self.context(9))
        self.assertEqual(state.governance.social_ratio('n0'), Q(4000, 9700))
        commands.prepare('n0', {'id': 2, 'kind': 'governance', 'op': 'set_regime',
                               'group': 'economic_doctrine', 'choice_tech_id': 'F07'}, self.context()).commit()
        self.assertFalse(state.governance.active('n0', 'F07'))
        state.governance.begin_tick(self.context(599))
        self.assertFalse(state.governance.active('n0', 'F07'))
        state.governance.begin_tick(self.context(600))
        self.assertTrue(state.governance.active('n0', 'F07'))
        state.initial.cash.audit()


if __name__ == '__main__':
    unittest.main()

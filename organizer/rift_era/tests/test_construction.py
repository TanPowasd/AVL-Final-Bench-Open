import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.world import generate_candidate


class ConstructionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.world = generate_candidate(1000)

    def game(self, ticks=110):
        return opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=ticks,
                               debug=False, commerce=True)

    def test_housing_cannot_operate_before_real_materials_and_construction_time(self):
        game = self.game(80)
        state = game.state
        order = {'id': 1, 'kind': 'industry', 'op': 'build_facility', 'hub_id': 'n0-h0',
                 'facility_type': '住宅区', 'profile_id': 'P110'}
        game.step({'n0': (order,)})
        job, = state.work.jobs.values()
        target = job.target
        self.assertEqual(state.initial.housing_mU['n0-h0'], 300000)
        self.assertTrue(state.initial.assets[target].under_construction)
        for _ in range(78):
            game.step()
        self.assertEqual(state.initial.housing_mU['n0-h0'], 300000)
        game.step()
        self.assertEqual(state.initial.housing_mU['n0-h0'], 400000)
        self.assertEqual(job.consumed_mU, {'cement': 20000, 'steel': 5000})
        self.assertEqual(job.cash_paid_mU, 187500)
        self.assertEqual(state.initial.assets[target].book_nmU, {'cement': 20000000, 'steel': 5000000})
        self.assertFalse(state.work.jobs)
        self.assertEqual(state.work.recent['n0'][-1]['status'], 'COMPLETED')
        state.audit()

    def test_cancel_destroys_only_spent_work_and_refunds_real_unused_locks(self):
        game = self.game(10)
        state = game.state
        order = {'id': 1, 'kind': 'industry', 'op': 'build_facility', 'hub_id': 'n0-h0',
                 'facility_type': 'public', 'profile_id': 'P110'}
        game.step({'n0': (order,)})
        job, = state.work.jobs.values()
        for _ in range(4):
            game.step()
        spent = dict(job.consumed_mU)
        game.step({'n0': ({'id': 2, 'kind': 'cancel', 'op': 'cancel_by_id', 'target_id': '1'},)})
        self.assertNotIn(job.target, state.initial.assets)
        self.assertNotIn(job.target, state.initial.facilities)
        self.assertEqual(state.initial.housing_mU['n0-h0'], 300000)
        for good, amount in spent.items():
            self.assertEqual(state.consumption.sink_debt[('n0', good)], amount)
            self.assertEqual(state.initial.inventory.balance(state.work.stock_lock(job, good)), 0)
        expected_loss = sum(Q(q, 1000) * self.catalog.weights[g] for g, q in spent.items())
        self.assertEqual(state.cycles['n0'].destroyed_value, expected_loss)
        state.audit()

    def test_factory_needs_a_completed_shell_and_separate_line_configuration(self):
        game = self.game()
        state = game.state
        order = {'id': 1, 'kind': 'industry', 'op': 'build_facility', 'hub_id': 'n0-h0',
                 'facility_type': '冶金厂', 'profile_id': 'S1'}
        game.step({'n0': (order,)})
        target = next(iter(state.work.jobs.values())).target
        for _ in range(79):
            game.step()
        lid, = state.initial.facilities[target].line_ids
        self.assertEqual(state.initial.lines[lid].recipe.id, '__idle__')
        game.step({'n0': ({'id': 2, 'kind': 'industry', 'op': 'set_line', 'line_id': lid,
                           'recipe': 'P001', 'mode': 'general'},)})
        for _ in range(19):
            game.step()
        self.assertEqual(state.initial.lines[lid].recipe.id, 'P001')
        self.assertFalse(state.initial.lines[lid].used_nmU)
        for _ in range(10):
            game.step()
        self.assertGreater(state.initial.lines[lid].used_nmU.get('iron_ore', 0), 0)
        state.audit()

    def test_repair_pays_real_resources_and_restores_book_without_consumption_rewards(self):
        game = self.game(100)
        state = game.state
        target = 'n0/P001/0'
        state.asset_lifecycle.damage(target, 500, event_id='fixture_prior_damage')
        orders = [{'id': 1, 'kind': 'industry', 'op': 'repair_asset', 'asset_id': target,
                   'target_health_mU': 1000, 'work_hub_id': 'n0-h0'}]
        # An actual player configuration makes enough power available for the
        # repair; this fixture does not inject electricity or labor quotas.
        for lid, line in state.initial.lines.items():
            if line.owner == 'n0' and lid not in state.initial.plants:
                orders.append({'id': len(orders) + 1, 'kind': 'industry', 'op': 'set_line',
                               'line_id': lid, 'recipe': None, 'mode': 'general'})
        for tick in range(100):
            game.step({'n0': tuple(orders)} if tick == 0 else None)
        self.assertFalse(state.work.jobs)
        asset = state.initial.assets[target]
        self.assertEqual(asset.health_mU, 1000)
        self.assertEqual(asset.book_nmU, asset.original_nmU)
        self.assertEqual(state.consumption.sink_debt[('n0', 'steel')], 3000)
        self.assertEqual(state.consumption.consumption20('n0', 'steel'), 0)
        state.audit()


if __name__ == '__main__':
    unittest.main()

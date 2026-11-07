"""V08 climate_intervention and grant_climate_consent (climate_v1)."""
import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.world import generate_candidate


class ClimateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.world = generate_candidate(1000)

    def game(self, ticks=40):
        return opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=ticks,
                               debug=False, commerce=True)

    def stock(self, state, owner='n0', amount=20000):
        account = Account(owner, f'{owner}-h0', 'available', 'remediation_agent')
        state.initial.inventory.transform({}, {account: amount}, reason='test_fixture',
                                          origins={account: Origin(owner, f'{owner}-h0')})

    def region(self, state, owner='n0'):
        return state.initial.map.hubs[f'{owner}-h0'].region

    def last(self, state, owner='n0'):
        return state.command_streams[owner].recent[-1]

    def test_requires_v08_and_respects_cooldown(self):
        game = self.game()
        state = game.state
        order = {'id': 1, 'kind': 'governance', 'op': 'climate_intervention', 'target_region': self.region(state), 'mode': 'enhance'}
        game.step({'n0': (order,)})
        self.assertEqual(self.last(state).code, 'ERR_TECH_LOCKED')
        state.research['n0'].known.add('V08')
        for _ in range(4):
            game.step()
        game.step({'n0': (dict(order, id=2),)})
        self.assertEqual(self.last(state).status, 'ACCEPTED')
        for _ in range(4):
            game.step()
        game.step({'n0': (dict(order, id=3),)})
        self.assertEqual(self.last(state).code, 'ERR_CAPACITY_EXCEEDED')

    def test_paid_intervention_shifts_rain_and_unconsented_spillover_costs_credit(self):
        game = self.game()
        state = game.state
        state.research['n0'].known.add('V08')
        self.stock(state)
        target = self.region(state)
        climate = state.climate
        affected = climate.downwind[target]
        foreign = [r for r in affected if climate.controller(r) not in (None, 'n0')]
        before = state.credit['n0']
        game.step({'n0': ({'id': 1, 'kind': 'governance', 'op': 'climate_intervention', 'target_region': target,
                           'mode': 'enhance'},)})
        run = climate.interventions['n0']
        while game.tick < 31:
            game.step()
        self.assertGreater(run.effective_cycles, 0, 'energized and paid cycles must take effect')
        self.assertTrue(run.active)
        self.assertEqual(climate.offset(target), Q(15, 100))
        for r in affected:
            self.assertEqual(climate.offset(r), Q(-15, 100))
        hub = f'n0-h0'
        self.assertEqual(state.rain[hub], state.hydrology.rain[target] * Q(115, 100))
        if foreign:
            self.assertEqual(state.credit['n0'], before - Q(1, 10))
            self.assertTrue(climate.action_cycles)
        else:
            self.assertEqual(state.credit['n0'], before)
        state.audit()

    def test_consent_must_come_from_the_controller_and_can_be_revoked(self):
        game = self.game()
        state = game.state
        mine = self.region(state, 'n1')
        game.step({'n1': ({'id': 1, 'kind': 'diplomacy', 'op': 'grant_climate_consent', 'target_nation': 'n0',
                           'region_ids': [self.region(state, 'n0')], 'duration_cycles': 10},)})
        self.assertEqual(self.last(state, 'n1').code, 'ERR_NO_PERMISSION')
        for _ in range(4):
            game.step()
        game.step({'n1': ({'id': 2, 'kind': 'diplomacy', 'op': 'grant_climate_consent', 'target_nation': 'n0',
                           'region_ids': [mine], 'duration_cycles': 10},)})
        self.assertEqual(self.last(state, 'n1').status, 'ACCEPTED')
        self.assertTrue(state.climate.consented('n0', mine, 0))
        for _ in range(4):
            game.step()
        # Revoked during cycle 1's commands: still valid for cycle 0, gone from cycle 1 on.
        game.step({'n1': ({'id': 3, 'kind': 'cancel', 'op': 'cancel_by_id', 'target_id': '2'},)})
        self.assertEqual(self.last(state, 'n1').status, 'ACCEPTED')
        self.assertFalse(state.climate.consented('n0', mine, 1))
        self.assertFalse(state.climate.consents)


if __name__ == '__main__':
    unittest.main()

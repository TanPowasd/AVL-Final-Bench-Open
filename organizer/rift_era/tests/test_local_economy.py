import json
import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.own_observation import owned_entities
from organizer.rift_era.engine.world import generate_candidate


class LocalEconomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def fixture(self):
        return opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=10,
                               commerce=True, debug=False)

    def test_food_in_capital_does_not_feed_unhoused_remote_residents(self):
        game = self.fixture()
        state = game.state
        # Component genesis with a second inhabited Hub; no in-game teleport API.
        state.initial.populations['n0-h0'].reserve_people({'general_worker': 10000})
        state.initial.populations['n0-h1'].receive({'general_worker': 10000})
        state.initial.cash.transfer(state.native('n0', 'household:n0-h0'), state.native('n0', 'household:n0-h1'), 100000)
        for _ in range(10):
            game.step()
        self.assertEqual(state.last_satisfaction['n0-h0']['food'], 1)
        self.assertEqual(state.last_satisfaction['n0-h1']['food'], 0)
        self.assertEqual(state.last_satisfaction['n0-h1']['electricity'], 0)
        self.assertLess(state.initial.populations['n0-h1'].total_mU, 10000)
        self.assertGreater(state.local_history['n0-h0'][-1].emitted_mU, 0)
        self.assertEqual(state.local_history['n0-h1'][-1].emitted_mU, 0)
        state.audit()

    def test_owner_entities_are_plain_json_and_foreign_state_does_not_leak(self):
        game = self.fixture()
        state = game.state
        before = json.dumps(owned_entities(state, 'n0'), sort_keys=True)
        enemy = next(a for a in state.initial.assets.values() if a.owner == 'n1')
        enemy.version += 12
        state.initial.populations['n1-h0'].support_mU = 123
        self.assertEqual(before, json.dumps(owned_entities(state, 'n0'), sort_keys=True))
        for _ in range(5):
            game.step()
        snapshot = state.observations['n0']
        self.assertEqual(len(snapshot['facilities']), 27)
        self.assertEqual(len(snapshot['mines']), 9)
        self.assertTrue(all('version' in line for line in snapshot['lines']))
        json.dumps(snapshot)


if __name__ == '__main__':
    unittest.main()

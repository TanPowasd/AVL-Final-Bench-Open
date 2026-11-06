"""03/04 N10 military/hypersonic_strike (hypersonic_v1)."""
import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.world import generate_candidate


class HypersonicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def scenario(self, war=True, spot=True):
        game = opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=30, commerce=True, debug=False)
        state = game.state
        state.research['n0'].known.add('N10')
        hubs = sorted(h for h, x in state.initial.map.hubs.items() if x.owner == 'n0')
        pairs = []
        for f in sorted(state.initial.facilities.values(), key=lambda f: f.id):
            if f.owner == 'n0' or f.owner not in state.initial.known or f.profile not in ('S1', 'S2', 'S3') or not f.line_ids:
                continue
            region = state.initial.map.hubs[f.hub].region
            for h in hubs:
                d = state.sensors.distances(state.initial.map.hubs[h].region).get(region, 99)
                if d <= 8:
                    pairs.append((d, f.id, h))
        self.assertTrue(pairs, 'no foreign S1-S3 facility within 8 hops of n0')
        _, fid, origin = min(pairs)
        target = state.initial.facilities[fid]
        region = state.initial.map.hubs[target.hub].region
        self.enemy = target.owner
        stock = Account('n0', origin, 'available', 'hypersonic_missile')
        state.initial.inventory.transform({}, {stock: 5000}, reason='test_fixture', origins={stock: Origin('n0', origin)})
        orders = [{'id': '1', 'kind': 'military', 'op': 'deploy_formation', 'hub_id': 'n0-h0', 'domain': 'Land',
                   'units': {'motorized': 1}}]
        if war:
            orders.append({'id': '2', 'kind': 'diplomacy', 'op': 'declare_war', 'target_nation': self.enemy})
        game.step({'n0': orders})
        while game.tick < 5:
            game.step()
        if spot:  # test placement: a spotter standing at the target Hub gives q >= .5
            spotter = next(f for f in state.formations.rows.values() if f.id.startswith('formation:') and f.owner == 'n0')
            spotter.region, spotter.hub = region, target.hub
            state.movement.positions.pop(spotter.id, None)
            for uid in spotter.unit_ids:  # fixture: optics bonus, since hill/mountain cells occlude plain sight
                state.initial.units[uid].sensor_bonus = Q(1, 5)
        return game, state, target, origin

    def order(self, target, origin, qty=2, i='5'):
        return {'id': i, 'kind': 'military', 'op': 'hypersonic_strike', 'origin_hub': origin,
                'target_id': target.id, 'missile_qty': qty}

    def last(self, state):
        return state.command_streams['n0'].recent[-1]

    def test_requires_war_and_visibility(self):
        game, state, target, origin = self.scenario(war=False)
        game.step({'n0': [self.order(target, origin)]})
        self.assertEqual(self.last(state).code, 'ERR_NOT_AT_WAR')
        game, state, target, origin = self.scenario(spot=False)
        game.step({'n0': [self.order(target, origin)]})
        self.assertEqual(self.last(state).code, 'ERR_NO_VISIBLE_TARGET')

    def test_unintercepted_missile_destroys_one_line_and_costs_action_loss(self):
        game, state, target, origin = self.scenario()
        alive = [l for l in target.line_ids if state.initial.assets[l].health_mU > 0]
        game.step({'n0': [self.order(target, origin, qty=1)]})
        self.assertEqual(self.last(state).status, 'ACCEPTED')
        self.assertEqual(state.initial.inventory.balance(Account('n0', origin, 'available', 'hypersonic_missile')), 4000)
        self.assertTrue(state.hypersonics.strikes)
        game.step()
        report = state.hypersonics.recent['n0'][-1]
        self.assertEqual((report['hits'], report['intercepted']), (1, 0))
        dead = [l for l in alive if state.initial.assets[l].health_mU == 0]
        self.assertEqual(len(dead), 1)
        self.assertGreater(state.hypersonics.action_cycles[0]['n0'], 0)
        state.audit()


if __name__ == '__main__':
    unittest.main()

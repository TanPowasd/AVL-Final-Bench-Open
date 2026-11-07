"""03 section 7 military/blockade (blockade_v1)."""
import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.world import generate_candidate


class BlockadeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def setup_game(self, war=True):
        game = opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=30, commerce=True, debug=False)
        state = game.state
        for good in ('ammunition', 'fuel'):
            account = Account('n0', 'n0-h0', 'available', good)
            state.initial.inventory.transform({}, {account: 500000}, reason='test_fixture',
                                              origins={account: Origin('n0', 'n0-h0')})
        orders = [{'id': '1', 'kind': 'military', 'op': 'deploy_formation', 'hub_id': 'n0-h0', 'domain': 'Land',
                   'units': {'motorized': 2}}]
        if war:
            orders.append({'id': '2', 'kind': 'diplomacy', 'op': 'declare_war', 'target_nation': 'n1'})
        game.step({'n0': orders})
        formation = next(f for f in state.formations.rows.values() if f.id.startswith('formation:') and f.owner == 'n0')
        edge = next(e for e in sorted(state.cargo.corridors.values(), key=lambda e: e.id)
                    if 'n0-h0' in (e.a, e.b) and e.mode in ('road', 'rail'))
        while game.tick < 5:
            game.step()
        return game, state, formation, edge

    def order(self, i, formation, edge, targets=('n1',)):
        return {'id': str(i), 'kind': 'military', 'op': 'blockade', 'formation_id': formation.id,
                'edge_id': edge.id, 'target_nations': list(targets)}

    def last(self, state):
        return state.command_streams['n0'].recent[-1]

    def test_requires_war_and_a_valid_endpoint(self):
        game, state, formation, edge = self.setup_game(war=False)
        game.step({'n0': [self.order(3, formation, edge)]})
        self.assertEqual(self.last(state).code, 'ERR_NOT_AT_WAR')
        far = next(e for e in state.cargo.corridors.values() if 'n0-h0' not in (e.a, e.b))
        for _ in range(4):
            game.step()
        game.step({'n0': [self.order(4, formation, far)]})
        self.assertIn(self.last(state).code, ('ERR_SITE_INCOMPATIBLE', 'ERR_NOT_AT_WAR'))

    def test_effective_blockade_cuts_target_capacity_and_costs_action_loss(self):
        game, state, formation, edge = self.setup_game()
        game.step({'n0': [self.order(3, formation, edge)]})
        self.assertEqual(self.last(state).status, 'ACCEPTED')
        game.step()
        b = edge.blocked.get('n1')
        self.assertIsNotNone(b)
        self.assertGreater(b, 0)
        self.assertNotIn('n2', edge.blocked)          # neutral third parties are untouched
        self.assertLess(edge.capacity_tick('n1'), edge.capacity_tick('n2'))
        losses = state.blockades.action_cycles[game.clock.cycle(game.tick - 1)]
        self.assertGreater(losses['n0'], 0)
        state.audit()

    def test_cancel_lifts_the_blockade_next_tick(self):
        game, state, formation, edge = self.setup_game()
        game.step({'n0': [self.order(3, formation, edge)]})
        while game.tick < 10:
            game.step()
        self.assertGreater(edge.blocked.get('n1', 0), 0)
        game.step({'n0': [{'id': '4', 'kind': 'cancel', 'op': 'cancel_by_id', 'target_id': '3'}]})
        self.assertEqual(self.last(state).status, 'ACCEPTED')
        self.assertEqual(edge.blocked.get('n1', 0), 0)


    def until_decision(self, game):
        while not game.clock.is_decision(game.tick):
            game.step()

    def test_g12_relays_the_blockade_task(self):
        game, state, formation, edge = self.setup_game()
        game.step({'n0': [{'id': '5', 'kind': 'military', 'op': 'deploy_formation', 'hub_id': 'n0-h0',
                           'domain': 'Land', 'units': {'motorized': 1}}]})
        self.until_decision(game)
        heir = next(f for f in sorted(state.formations.rows.values(), key=lambda f: f.id)
                    if f.owner == 'n0' and f.id.startswith('formation:') and f.id != formation.id)
        self.until_decision(game)
        game.step({'n0': [self.order(7, formation, edge)]})
        game.step()
        self.assertGreater(edge.blocked.get('n1', 0), 0)
        # Test placement: the holder leaves the endpoint (bypassing a march).
        away = next(h for h in sorted(state.initial.map.hubs) if h.startswith('n0-') and h not in (edge.a, edge.b))
        formation.hub, formation.region = away, state.initial.map.hubs[away].region
        state.movement.positions.pop(formation.id, None)
        game.step()
        self.assertEqual(edge.blocked.get('n1', 0), 0)   # without G12 the task lapses
        self.assertIn(formation.id, state.blockades.plans)
        state.research['n0'].known.add('G12')
        game.step()
        self.assertNotIn(formation.id, state.blockades.plans)
        self.assertEqual(state.blockades.plans[heir.id].command_id, '7')
        self.assertEqual(state.blockades.recent['n0'][-1]['status'], 'RELAYED')
        self.assertGreater(edge.blocked.get('n1', 0), 0)
        # Cancelling by the original command id still ends the relayed task.
        self.until_decision(game)
        game.step({'n0': [{'id': '8', 'kind': 'cancel', 'op': 'cancel_by_id', 'target_id': '7'}]})
        self.assertNotIn(heir.id, state.blockades.plans)
        state.audit()

    def test_g12_relay_supply_hubs_only_same_region_online_bases(self):
        from organizer.rift_era.engine.battle_operations import relay_supply_hubs
        game, state, formation, edge = self.setup_game()
        self.assertEqual(relay_supply_hubs(state, formation, game.tick), ())
        state.research['n0'].known.add('G12')
        hubs = relay_supply_hubs(state, formation, game.tick)
        region = state.initial.map.hubs[formation.hub].region
        self.assertNotIn(formation.hub, hubs)
        self.assertTrue(all(state.initial.map.hubs[h].region == region for h in hubs))
        self.assertEqual(list(hubs), sorted(hubs))

class AclDenialShareTests(unittest.TestCase):
    """03 s7.3: Blockade_r uses B = max(ACL denial flag, b) per in-edge."""

    def test_explicit_tier_zero_counts_as_full_denial(self):
        from fractions import Fraction as Q
        catalog = Catalog()
        game = opening_economy(catalog, initialize(catalog, generate_candidate(1000)), total_ticks=10, debug=False)
        state = game.state
        hubs = state.initial.map.hubs
        region = hubs['n1-h0'].region
        edges = [e for e in state.cargo.corridors.values() if region in (hubs[e.a].region, hubs[e.b].region)]
        total = sum(e.capacity_mU_cycle for e in edges)
        self.assertEqual(state.blockades.region_share(region, 'n0'), 0)
        first, second = sorted(edges, key=lambda e: e.id)[:2]
        first.access_tier['n0'] = 0                       # operator closed it to n0
        second.blocked['n0'] = Q(1, 2)
        second.access_tier['n0'] = 2                       # open tier: b alone counts
        expected = Q(first.capacity_mU_cycle + second.capacity_mU_cycle / 2) / total
        self.assertEqual(state.blockades.region_share(region, 'n0'), expected)
        first.blocked['n0'] = Q(1, 3)                      # max, never a sum
        self.assertEqual(state.blockades.region_share(region, 'n0'), expected)
        self.assertEqual(state.blockades.region_share(region, 'n2'), 0)


if __name__ == '__main__':
    unittest.main()

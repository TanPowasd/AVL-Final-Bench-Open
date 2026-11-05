"""03 section 7 item 5 military/interdict (blockade_v1 interdiction)."""
import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.world import generate_candidate


class InterdictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def scenario(self, war=True):
        game = opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=40, commerce=True, debug=False)
        state = game.state
        for good in ('ammunition', 'fuel'):
            account = Account('n0', 'n0-h0', 'available', good)
            state.initial.inventory.transform({}, {account: 500000}, reason='test_fixture',
                                              origins={account: Origin('n0', 'n0-h0')})
        n0 = [{'id': '1', 'kind': 'military', 'op': 'deploy_formation', 'hub_id': 'n0-h0', 'domain': 'Land',
               'units': {'motorized': 2}}]
        if war:
            n0.append({'id': '2', 'kind': 'diplomacy', 'op': 'declare_war', 'target_nation': 'n1'})
        n1 = [{'id': '1', 'kind': 'logistics', 'op': 'route_shipment', 'origin_hub': 'n1-h0', 'dest_hub': 'n1-h1',
               'corridor_path': ['road:n1-h0:n1-h1'], 'good': 'stone', 'qty_mU': 6000, 'carrier_recipe': 'P036'}]
        game.step({'n0': n0, 'n1': n1})
        self.assertEqual(state.command_streams['n1'].recent[-1].status, 'ACCEPTED')
        formation = next(f for f in state.formations.rows.values() if f.id.startswith('formation:') and f.owner == 'n0')
        # Test placement: stand the formation at the far endpoint of the shipment's edge (bypassing a march).
        formation.hub, formation.region = 'n1-h1', state.initial.map.hubs['n1-h1'].region
        state.movement.positions.pop(formation.id, None)
        for good in ('ammunition', 'fuel'):
            onboard = Account('n0', formation.id, 'available', good)
            state.initial.inventory.transform({}, {onboard: 50000}, reason='test_fixture',
                                              origins={onboard: Origin('n0', 'n0-h0')})
        return game, state, formation

    def wait_for_track(self, game, state, owner='n0'):
        while game.tick < 35:
            tracks = {t: s for t, s in state.blockades.tracks(owner).items()
                      if s.quantity_mU and s.mode.value == 'IN_TRANSIT'}
            if tracks and game.clock.is_decision(game.tick):
                return tracks
            game.step()
        self.fail('no observable shipment track')

    def test_track_visible_only_with_a_present_formation(self):
        game, state, formation = self.scenario()
        tracks = self.wait_for_track(game, state)
        self.assertFalse(state.blockades.tracks('n2'))
        (tid, shipment), = tracks.items()
        self.assertTrue(tid.startswith('track:'))
        self.assertNotIn(shipment.id, tid)

    def test_interdiction_destroys_cargo_and_damages_the_vehicle(self):
        game, state, formation = self.scenario()
        tracks = self.wait_for_track(game, state)
        (tid, shipment), = tracks.items()
        before_qty = shipment.quantity_mU
        vehicle = state.cargo.vehicles[shipment.vehicle_id]
        before_hp = vehicle.health_mU
        game.step({'n0': [{'id': '9', 'kind': 'military', 'op': 'interdict', 'formation_id': formation.id,
                           'shipment_track_id': tid}]})
        self.assertEqual(state.command_streams['n0'].recent[-1].status, 'ACCEPTED')
        report = state.blockades.recent['n0'][-1]
        self.assertEqual(report['status'], 'HIT', report)
        self.assertGreater(report['cargo_lost_mU'], 0)
        self.assertLessEqual(report['hit_mU'], 250)
        self.assertEqual(shipment.quantity_mU, before_qty - report['cargo_lost_mU'])
        self.assertLess(vehicle.health_mU, before_hp)
        state.audit()

    def test_requires_war_with_owner_and_carrier(self):
        game, state, formation = self.scenario(war=False)
        tracks = self.wait_for_track(game, state)
        (tid, _), = tracks.items()
        game.step({'n0': [{'id': '9', 'kind': 'military', 'op': 'interdict', 'formation_id': formation.id,
                           'shipment_track_id': tid}]})
        self.assertEqual(state.command_streams['n0'].recent[-1].code, 'ERR_NOT_AT_WAR')


if __name__ == '__main__':
    unittest.main()

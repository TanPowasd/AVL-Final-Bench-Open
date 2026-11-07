"""supply_connected_v1: 07 U_ctrl connection over authorized, undestroyed supply paths."""
import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import full_match
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.world import generate_candidate

P = 'n0'


class SupplyConnectedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def setUp(self):
        self.game = full_match(self.catalog, initialize(self.catalog, self.world), total_ticks=20, debug=False)
        self.game.step()
        self.s = self.game.state
        self.power = self.s.national_power
        self.map = self.s.initial.map
        self.palette = tuple(sorted(self.map.regions))

    def stock(self, hub, goods):
        accounts = {Account(P, hub, 'available', g): q for g, q in goods.items()}
        self.s.initial.inventory.transform({}, accounts, reason='fixture',
                                           origins={a: Origin(P, hub) for a in accounts})

    def cut(self, region, how):
        """Make every corridor touching `region` (endpoint or crossed cell) unusable for P."""
        cargo = self.s.cargo
        hubs = {h for h, x in self.map.hubs.items() if x.region == region}
        for eid, edge in cargo.corridors.items():
            cells = cargo.paths.get(eid, ())
            if edge.a in hubs or edge.b in hubs or any(self.palette[self.map.region_cells[c]] == region for c in cells):
                how(edge)

    def target(self):
        """A connected non-capital region of P that holds a hub."""
        capital = self.map.hubs[f'{P}-h0'].region
        connected = self.power.connected_regions(P)
        return next(self.map.hubs[h].region for h in sorted(self.map.hubs)
                    if self.map.hubs[h].owner == P and self.map.hubs[h].region in connected
                    and self.map.hubs[h].region != capital)

    def test_capital_reaches_its_supply_network(self):
        connected = self.power.connected_regions(P)
        controlled = {r for r, row in self.s.authority.regions.items() if (row.controller or row.sovereign) == P}
        self.assertIn(self.map.hubs[f'{P}-h0'].region, connected)
        self.assertLessEqual(connected, controlled)
        self.assertGreater(len(connected), len(controlled) // 2)

    def test_destroyed_corridors_disconnect_a_region(self):
        region = self.target()
        self.cut(region, lambda edge: setattr(edge, 'health_mU', 0))
        self.assertNotIn(region, self.power.connected_regions(P))

    def test_closed_acl_and_full_blockade_disconnect(self):
        region = self.target()
        self.cut(region, lambda edge: edge.access_tier.__setitem__(P, 0))
        self.assertNotIn(region, self.power.connected_regions(P))
        self.setUp()
        region = self.target()
        self.cut(region, lambda edge: edge.blocked.__setitem__(P, Q(1)))
        self.assertNotIn(region, self.power.connected_regions(P))

    def test_partial_damage_still_connects(self):
        region = self.target()
        self.cut(region, lambda edge: setattr(edge, 'health_mU', 1))
        self.assertIn(region, self.power.connected_regions(P))

    def test_lost_capital_needs_a_substitute_command_hub(self):
        capital = self.map.hubs[f'{P}-h0'].region
        self.s.authority.regions[capital].controller = 'n1'
        self.assertEqual(self.power.connected_regions(P), set())
        hub = next(h for h in sorted(self.map.hubs) if self.map.hubs[h].owner == P
                   and self.map.hubs[h].region != capital)
        # Food alone is not a command post; communication equipment alone is not either.
        food = self.s.initial.inventory.balance(Account(P, hub, 'available', 'food'))
        if food < 1000:
            self.stock(hub, {'food': 1000 - food})
        if self.s.initial.inventory.balance(Account(P, hub, 'available', 'communication_equipment')) < 1000:
            self.assertEqual(self.power.command_hubs(P), [])
            self.stock(hub, {'communication_equipment': 1000})
        self.assertEqual(self.power.command_hubs(P), [hub])
        connected = self.power.connected_regions(P)
        self.assertIn(self.map.hubs[hub].region, connected)
        self.assertNotIn(capital, connected)

    # -- supply_flag_v1: 07 Vready supply_ratio = min(food/water ratio, supply flag) --
    def unit(self):
        return next(u for uid, u in sorted(self.s.initial.units.items()) if u.owner == P)

    def test_supply_reach_extends_connected_regions(self):
        reach = self.power.supply_reach(P)
        self.assertLessEqual(self.power.connected_regions(P), reach)
        self.assertIn(self.map.hubs[f'{P}-h0'].region, reach)

    def test_unit_outside_supply_reach_loses_readiness(self):
        region = self.target()
        unit = self.unit()
        unit.region = region
        supplied = self.power.readiness(P)
        self.cut(region, lambda edge: setattr(edge, 'health_mU', 0))
        self.assertNotIn(region, self.power.supply_reach(P))
        self.assertLess(self.power.readiness(P), supplied)
        unit.region = self.map.hubs[f'{P}-h0'].region  # back on the network: the flag returns
        self.assertEqual(self.power.readiness(P), supplied)

    def test_foreign_region_on_a_supply_path_keeps_the_flag(self):
        controlled = {r for r, row in self.s.authority.regions.items() if (row.controller or row.sovereign) == P}
        foreign = sorted(self.power.supply_reach(P) - controlled)
        if not foreign:
            self.skipTest('seed 1000 gives P no authorized path into a foreign region')
        unit = self.unit()
        home = unit.region
        base = self.power.readiness(P)
        unit.region = foreign[0]
        self.assertEqual(self.power.readiness(P), base)
        unit.region = home


if __name__ == '__main__':
    unittest.main()

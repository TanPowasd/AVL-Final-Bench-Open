"""02 P7 support events: occupation -12, C04 blackout -20, E26 B1 -10*delta, capture reset, 03 bombard -300."""
import unittest
from types import SimpleNamespace

from organizer.rift_era.tests import test_transport_operations as fixture_source
from organizer.rift_era.engine.combat import Shot
from organizer.rift_era.engine.economy_state import CycleEconomy
from organizer.rift_era.engine.local_economy import settle_residents
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.support_events import reset_captured_support, shock_region_support, support_events_mU


class SupportEventTermTests(unittest.TestCase):
    def test_blackout_and_b1_shortfall(self):
        self.assertEqual(support_events_mU(CycleEconomy()), 0)
        self.assertEqual(support_events_mU(CycleEconomy(blackout_ticks=1)), -20)
        self.assertEqual(support_events_mU(CycleEconomy(b1_needed_mU=1000, b1_received_mU=1000)), 0)
        self.assertEqual(support_events_mU(CycleEconomy(b1_needed_mU=1000, b1_received_mU=250)), Q(-15, 2))
        self.assertEqual(support_events_mU(CycleEconomy(blackout_ticks=3, b1_needed_mU=4, b1_received_mU=0)), -30)


class SupportEventStateTests(unittest.TestCase):
    setUpClass = classmethod(fixture_source.CargoCommandTests.setUpClass.__func__)
    fixture = fixture_source.CargoCommandTests.fixture

    def pick(self, state):
        hub = next(h for h, p in sorted(state.initial.populations.items())
                   if p.total_mU and state.initial.map.hubs[h].owner == 'n0')
        return hub, state.initial.map.hubs[hub].region

    def settle(self, state, hub, **metric):
        for h in state.local_cycles:
            state.local_cycles[h] = CycleEconomy(**(metric if h == hub else {}))
        state.initial.populations[hub].support_mU = 500
        settle_residents(state, SimpleNamespace(seed=1000, cycle=0, tick=9))
        return state.initial.populations[hub].support_mU

    def test_occupation_and_grid_events_lower_support(self):
        base_state = self.fixture().state
        hub, region = self.pick(base_state)
        base = self.settle(self.fixture().state, hub)
        blackout = self.settle(self.fixture().state, hub, blackout_ticks=2)
        # -20 inside the shared clip(-45, +25); +-1 for rounding of the clipped sum.
        self.assertLessEqual(abs((base - blackout) - min(20, base - 500 + 45)), 1)
        self.assertGreater(base - blackout, 0)
        occupied_state = self.fixture().state
        occupied_state.authority.regions[region].controller = 'n1'
        occupied = self.settle(occupied_state, hub)
        self.assertLessEqual(abs((base - occupied) - min(12, base - 500 + 45)), 1)
        self.assertGreater(base - occupied, 0)
        # The observed regional support follows the populations of the region.
        rows = [p for h, p in occupied_state.initial.populations.items()
                if occupied_state.initial.map.hubs[h].region == region and p.total_mU]
        self.assertEqual(occupied_state.authority.regions[region].support_mU,
                         sum(p.support_mU * p.total_mU for p in rows) // sum(p.total_mU for p in rows))

    def test_capture_resets_and_bombard_shocks_region_support(self):
        state = self.fixture().state
        hub, region = self.pick(state)
        row = state.authority.regions[region]
        row.controller, row.support_mU = 'n1', 280
        reset_captured_support(state, [(region, 'n0', 'n1')])
        self.assertEqual(state.initial.populations[hub].support_mU, 280)
        shock_region_support(state, region, -300)
        self.assertEqual(state.initial.populations[hub].support_mU, 0)
        self.assertEqual(row.support_mU, 0)

    def test_bombard_once_per_region_and_attacker_per_cycle(self):
        state = self.fixture().state
        hub, region = self.pick(state)
        state.initial.populations[hub].support_mU = 900
        battles = state.battles if hasattr(state, 'battles') else None
        if battles is None:
            battles = next(v for v in vars(state).values() if type(v).__name__ == 'Battles')
        unit = lambda uid, owner, where, posture: SimpleNamespace(id=uid, owner=owner, region=where, posture=posture)
        combatants = {'a': unit('a', 'n1', 'x', 'bombard'), 'b': unit('b', 'n1', 'x', 'assault'),
                      't': unit('t', 'n0', region, 'preserve')}
        credit = state.credit['n1']
        battles.apply_bombard(SimpleNamespace(cycle=5), SimpleNamespace(shots=(Shot('b', 't', Q(1)),)), combatants)
        self.assertEqual(state.initial.populations[hub].support_mU, 900)
        battles.apply_bombard(SimpleNamespace(cycle=5), SimpleNamespace(shots=(Shot('a', 't', Q(1)),)), combatants)
        self.assertEqual(state.initial.populations[hub].support_mU, 600)
        self.assertEqual(state.credit['n1'], max(Q(1, 10), credit - Q(5, 100)))
        battles.apply_bombard(SimpleNamespace(cycle=5), SimpleNamespace(shots=(Shot('a', 't', Q(1)),)), combatants)
        self.assertEqual(state.initial.populations[hub].support_mU, 600)
        self.assertEqual(state.credit['n1'], max(Q(1, 10), credit - Q(5, 100)))
        battles.apply_bombard(SimpleNamespace(cycle=6), SimpleNamespace(shots=(Shot('a', 't', Q(1)),)), combatants)
        self.assertEqual(state.initial.populations[hub].support_mU, 300)


if __name__ == '__main__':
    unittest.main()

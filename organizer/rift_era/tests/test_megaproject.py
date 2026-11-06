"""04 L12 strait bridge / undersea tunnel (megaproject_v1)."""
import unittest

from organizer.rift_era.tests import test_transport_operations as cargo_fixture
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.ledger import Account


class MegaprojectTests(unittest.TestCase):
    setUpClass = classmethod(cargo_fixture.CargoCommandTests.setUpClass.__func__)
    fixture = cargo_fixture.CargoCommandTests.fixture
    context = cargo_fixture.CargoCommandTests.context

    def test_l12_rail_bridge_needs_l12_costs_megaproject_inputs_and_carries_800(self):
        from organizer.rift_era.engine.commands import EngineCommands
        from organizer.rift_era.engine.economy_state import TickEconomy
        state = self.fixture().state
        world = state.initial.map
        path = list(state.cargo.paths['road:n0-h0:n0-h1'])
        original = world.terrain_cells
        terrain = bytearray(original)
        for c in path[5:8]:
            terrain[c] = 0                                  # fixture: a three-cell strait on the route
        object.__setattr__(world, 'terrain_cells', bytes(terrain))
        try:
            state.research['n0'].known.add('L03')
            handlers = EngineCommands(state)
            order = {'id': '7', 'kind': 'logistics', 'op': 'build_corridor', 'origin_hub': 'n0-h0',
                     'dest_hub': 'n0-h1', 'mode': 'rail', 'level': 1, 'path_cells': path}
            with self.assertRaises(RuleViolation) as e:
                handlers.prepare('n0', order, self.context())
            self.assertEqual(e.exception.code, 'ERR_TECH_LOCKED')
            state.research['n0'].known.add('L12')
            stock = {'structure_module': 40000, 'steel': 80000, 'cement': 120000, 'metamaterial': 4000,
                     'basic_parts': 10000}
            state.initial.inventory.transform({}, {Account('n0', 'n0-h0', 'available', g): q for g, q in stock.items()},
                                              reason='megaproject_test_endowment')
            handlers.prepare('n0', order, self.context()).commit()
            job, = state.work.jobs.values()
            self.assertEqual(job.duration_ticks, 1200)
            self.assertEqual(job.materials_mU['metamaterial'], 4000)
            self.assertEqual(job.materials_mU['structure_module'], 40000)
            self.assertEqual(job.materials_mU['steel'], 80000 + 20800)   # bridge + 2.6 blocks of rail
            for tick in range(job.duration_ticks):
                state.initial.inventory.materials.begin_tick(tick)
                state.asset_lifecycle.begin_tick(tick)
                state.frame = TickEconomy()
                state.work.reserve(self.context(tick)); state.work.advance(self.context(tick))
            self.assertFalse(state.work.jobs)
            eid, row = next(iter(state.megaprojects.items()))
            self.assertEqual(row['kind'], 'L12_bridge')
            self.assertEqual(state.cargo.corridors[eid].capacity_mU_cycle, 800000)
            self.assertEqual(state.cargo.corridors[eid].mode, 'rail')
        finally:
            object.__setattr__(world, 'terrain_cells', original)


if __name__ == '__main__':
    unittest.main()

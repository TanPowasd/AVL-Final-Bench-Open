"""04/03 L12 artificial island (artificial_island_v1)."""
import copy
import unittest
from types import SimpleNamespace

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.commands import EngineCommands
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.economy_state import TickEconomy
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.industry_commands import coastal_hub
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.island import COSTS, found, island_cell
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.world import SIZE
from organizer.rift_era.engine.world_atlas import generate_candidate


class IslandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def game(self):
        # The island extends the map in place: never share the class world.
        return opening_economy(self.catalog, initialize(self.catalog, copy.deepcopy(self.world)),
                               total_ticks=40, commerce=True, debug=False)

    def coastal(self, state):
        return next((h, hub.owner) for h, hub in sorted(state.initial.map.hubs.items())
                    if hub.owner and hub.owner.startswith('n') and coastal_hub(state, h)
                    and state.authority.controls_hub(hub.owner, h))

    def context(self, tick=0):
        return SimpleNamespace(tick=tick, seed=1000, clock=ClockConfig(), cycle=tick // 10)

    def test_command_needs_l12_locks_materials_at_site_and_founds_the_island(self):
        state = self.game().state
        hub, owner = self.coastal(state)
        handlers = EngineCommands(state)
        order = {'id': '7', 'kind': 'industry', 'op': 'build_facility', 'hub_id': hub,
                 'facility_type': 'public', 'profile_id': 'L12_island'}
        with self.assertRaises(RuleViolation) as e:
            handlers.prepare(owner, dict(order), self.context())
        self.assertEqual(e.exception.code, 'ERR_TECH_LOCKED')
        state.research[owner].known.add('L12')
        state.initial.inventory.transform({}, {Account(owner, hub, 'available', g): q for g, q in COSTS.items()},
                                          reason='island_test_endowment')
        handlers.prepare(owner, dict(order), self.context()).commit()
        job, = state.work.jobs.values()
        self.assertEqual((job.kind, job.duration_ticks, job.hub), ('build_island', 2400, hub))
        self.assertEqual(job.materials_mU, COSTS)
        cell = job.payload['cell']
        self.assertEqual(state.initial.map.terrain_cells[cell], 0)
        for tick in range(job.duration_ticks):
            state.initial.inventory.materials.begin_tick(tick)
            state.asset_lifecycle.begin_tick(tick)
            state.frame = TickEconomy()
            state.work.reserve(self.context(tick)); state.work.advance(self.context(tick))
        self.assertFalse(state.work.jobs)
        rid, row = next((k, v) for k, v in state.megaprojects.items() if v['kind'] == 'L12_island')
        world = state.initial.map
        island = world.hubs[row['hub']]
        self.assertEqual((island.owner, island.region, island.y * SIZE + island.x), (owner, rid, cell))
        self.assertEqual(world.terrain_cells[cell], 1)
        self.assertEqual(sorted(world.regions)[world.region_cells[cell]], rid)
        self.assertIn(rid, world.regions[world.hubs[hub].region].adjacent)
        self.assertEqual(state.authority.regions[rid].controller, owner)
        self.assertTrue(state.authority.controls_hub(owner, row['hub']))

    def test_founded_island_survives_the_running_game(self):
        game = self.game()
        state = game.state
        hub, owner = self.coastal(state)
        old_palette = sorted(state.initial.map.regions)
        cell = island_cell(state.initial.map, hub)
        aid = next(a for a, asset in sorted(state.initial.assets.items()) if asset.owner == owner)
        hub_id = found(state, SimpleNamespace(owner=owner, hub=hub, target=aid, payload={'cell': cell}))
        new_palette = sorted(state.initial.map.regions)
        self.assertEqual(new_palette[:len(old_palette)], old_palette)   # no old index moved
        for _ in range(25):
            game.step()
        state.audit()
        self.assertIn(hub_id, state.pollution)
        self.assertEqual(state.initial.warehouse_mU[hub_id], 300000)
        # A second island gets the next id and still sorts last.
        cell2 = island_cell(state.initial.map, hub)
        self.assertNotEqual(cell2, cell)
        found(state, SimpleNamespace(owner=owner, hub=hub, target=aid, payload={'cell': cell2}))
        self.assertEqual(sorted(state.initial.map.regions)[-1], '~isl0001')
        for _ in range(10):
            game.step()
        state.audit()


if __name__ == '__main__':
    unittest.main()

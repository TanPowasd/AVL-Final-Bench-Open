import unittest
from organizer.rift_era.engine.world_atlas import generate_candidate, generate_world, VERSION
from organizer.rift_era.engine.world import BootstrapResult
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.determinism import canonical, fingerprint


class AtlasTests(unittest.TestCase):
    def test_seed_components_routes_and_hidden_resources(self):
        for seed in (0, 1, 2):
            world = generate_candidate(seed)
            world.validate_geometry()
            self.assertEqual(world.generator_version, VERSION)
            self.assertEqual(sum(r.terrain != 'ocean' for r in world.regions.values()), 380)
            self.assertEqual(len(world.hubs), 250)
            self.assertGreaterEqual(len(world.city_states), 36)
            self.assertLessEqual(len(world.city_states), 48)
            self.assertGreaterEqual(len(world.routes), 1100)
            self.assertLessEqual(len(world.routes), 1500)
            self.assertEqual(set(world.seat_permutation), set(range(8)))
            # Every same-landmass Hub is connected by actual operational roads;
            # island Hubs have an actual water-only reserved sea path.
            road_graph = {h: set() for h in world.hubs}
            for route in world.routes.values():
                if route.mode == 'road':
                    road_graph[route.a].add(route.b); road_graph[route.b].add(route.a)
            for owner in world.country_regions:
                start = f'{owner}-h0'; visited = set(); queue = [start]
                while queue:
                    h = queue.pop()
                    if h in visited: continue
                    visited.add(h); queue.extend(road_graph[h])
                self.assertTrue(all(f'{owner}-h{i}' in visited for i in range(5)))
            for rid, outlets in world.river_downstream.items():
                seen = {rid}
                while rid in world.river_downstream:
                    target, = world.river_downstream[rid]
                    self.assertLess(world.regions[target].elevation, world.regions[rid].elevation)
                    self.assertNotIn(target, seen)
                    seen.add(target); rid = target
                self.assertEqual(world.regions[rid].terrain, 'ocean')
            for route in world.routes.values():
                if route.mode == 'sea':
                    self.assertIn(route.a, world.coastal_hubs)
                    self.assertIn(route.b, world.coastal_hubs)
                    self.assertTrue(all(world.terrain_cells[c] == 0 for c in route.cells[1:-1]))
            public = canonical(world.public_map())
            self.assertNotIn(b'reserve_mU', public)
            self.assertNotIn(b'hidden_resources', public)
            self.assertTrue(world.hidden_resources)
            self.assertFalse(world.accepted)

    def test_deterministic_generation_and_initial_mine_binding(self):
        world = generate_candidate(17)
        self.assertEqual(fingerprint(world.manifest()), fingerprint(generate_candidate(17).manifest()))
        initial = initialize(Catalog(), world)
        for mid, mine in initial.mines.items():
            self.assertEqual(mine.grade * 1000, world.hidden_resources[mid].grade_mU)
            self.assertEqual(world.hidden_resources[mid].starting_mine, mid)
        self.assertEqual(initial.resource_atlas, world.hidden_resources)
        initial.audit()

    def test_rejection_gate_requires_exact_150_tick_acceptance(self):
        attempts = []
        def bootstrap(world):
            attempts.append(world.attempt)
            return BootstrapResult(True, 149 if len(attempts) == 1 else 150)
        world = generate_world(3, bootstrap)
        self.assertTrue(world.accepted)
        self.assertEqual(attempts, [0, 1])

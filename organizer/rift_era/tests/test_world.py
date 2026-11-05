import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.determinism import fingerprint, canonical
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate, generate_world, BootstrapResult
from organizer.rift_era.engine.ledger import Account, CashAccount
from organizer.rift_era.engine.protocol import paginate, assemble


class WorldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()

    def test_generated_geometry_ranges_multiple_seeds(self):
        for seed in (0, 1, 42, 1000, 2000, 2**64-1):
            with self.subTest(seed=seed):
                world = generate_candidate(seed)
                world.validate_geometry()
                self.assertEqual(len(world.region_cells), 512 * 512)
                self.assertEqual(len(world.country_regions), 8)
                self.assertFalse(world.accepted)

    def test_world_determinism_and_no_seed_in_public_map(self):
        a, b = generate_candidate(1000), generate_candidate(1000)
        self.assertEqual(fingerprint(a.manifest()), fingerprint(b.manifest()))
        public = a.public_map()
        self.assertNotIn('seed', public)
        self.assertNotIn('attempt', public)
        self.assertEqual(len(public['region_rle_rows']), 512)
        self.assertTrue(all(sum(run[1] for run in row) == 512 for row in public['region_rle_rows']))
        self.assertEqual(canonical(assemble(paginate(public))), canonical(public))

    def test_initial_template_counts_and_money_conservation(self):
        state = initialize(self.catalog, generate_candidate(1000))
        self.assertEqual(len(state.lines), 24 * 8)
        self.assertEqual(len(state.facilities), 27 * 8)
        self.assertEqual(len(state.plants), 5 * 8)
        self.assertEqual(len(state.mines), 9 * 8)
        self.assertEqual(len(state.companies), 6 * 8)
        for country in state.map.country_regions:
            hub = f'{country}-h0'
            self.assertEqual(state.populations[hub].total_mU, 194000)
            self.assertEqual(sum(u.crew_mU for u in state.units.values() if u.owner == country), 6000)
            self.assertEqual(state.inventory.balance(Account(country, hub, 'available', 'vehicle')), 3000)
            self.assertNotIn('L02', state.known[country])
            self.assertEqual(state.cash.balance(CashAccount(country, 'treasury', country)).native_mU, 100000000)
            self.assertEqual(state.warehouse_mU[hub], 3300000)
            self.assertEqual(state.housing_mU[hub], 300000)
            self.assertEqual(state.line_policies[f'{country}/P062/0/line'].load_mU, 500)
            self.assertEqual(state.line_policies[f'{country}/P028/0/line'].fertility_mU, 1100)
            for index in range(1, 5):
                self.assertEqual(state.populations[f'{country}-h{index}'].total_mU, 0)
        before = fingerprint(state.snapshot())
        for _ in range(3):
            state.audit()
        self.assertEqual(before, fingerprint(state.snapshot()))

    def test_bootstrap_requires_actual_150_ticks_and_retries_new_attempt(self):
        attempts = []
        def bootstrap(world):
            attempts.append(world.attempt)
            return BootstrapResult(True, 0 if world.attempt == 0 else 150)
        accepted = generate_world(1, bootstrap)
        self.assertTrue(accepted.accepted)
        self.assertEqual(attempts, [0, 1])
        self.assertEqual(accepted.attempt, 1)


if __name__ == '__main__':
    unittest.main()

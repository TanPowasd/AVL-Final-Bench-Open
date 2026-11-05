"""P0-3: world_atlas_v1 is the main-line map (remote deposits, rivers, gate)."""
import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.world import BootstrapResult
from organizer.rift_era.engine import world_atlas
from organizer.rift_era.engine.errors import InfrastructureFault


class AtlasWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.world = world_atlas.generate_candidate(1000)

    def game(self, ticks=10):
        return opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=ticks,
                               debug=False, commerce=True)

    def test_every_atlas_site_is_registered_with_real_nature_stock(self):
        state = self.game().state
        atlas = self.world.hidden_resources
        self.assertEqual(len(atlas), 572)
        self.assertTrue(set(atlas) <= set(state.resource_sites) | set(state.initial.mines))
        for sid, site in atlas.items():
            deposit = state.deposits[sid]
            self.assertEqual(deposit.good, site.good)
            self.assertEqual(deposit.renewable, site.renewable)
            self.assertEqual(state.resource_sites[sid].grade * 1000, site.grade_mU)
            if site.good != 'water':
                self.assertEqual(deposit.initial_mU, site.reserve_mU)
                source = Account(*state.resource_sources[sid], site.good)
                self.assertEqual(state.initial.inventory.balance(source), site.reserve_mU)
        # Remote sites are geology, not producing mines.
        remote = [sid for sid, s in atlas.items() if s.starting_mine is None]
        self.assertEqual(len(remote), 500)
        self.assertFalse(set(remote) & set(state.initial.mines))
        self.assertFalse(set(remote) & set(state.extraction_lines))
        state.audit()

    def test_hydrology_follows_the_atlas_river_dag(self):
        state = self.game().state
        self.assertEqual(state.hydrology.downstream,
                         {rid: tuple(o) for rid, o in self.world.river_downstream.items()})
        position = {rid: i for i, rid in enumerate(state.hydrology.order)}
        for rid, (outlet,) in state.hydrology.downstream.items():
            if outlet in position:
                self.assertLess(position[rid], position[outlet])

    def test_remote_deposit_can_be_mined_and_depletes(self):
        game = self.game(60)
        state = game.state
        owner = 'n0'
        # Construction stock sits at the capital, so use the capital's remote site.
        sid = sorted(sid for sid, s in self.world.hidden_resources.items()
                     if s.starting_mine is None and s.good not in ('water', 'biomass')
                     and s.hub == f'{owner}-h0')[0]
        site = self.world.hidden_resources[sid]
        order = {'id': 1, 'kind': 'industry', 'op': 'build_mine', 'hub_id': site.hub, 'deposit_id': sid,
                 'good': site.good, 'level': 'G1'}
        game.step({owner: (order,)})
        self.assertEqual(state.command_streams[owner].recent[-1].status, 'ACCEPTED')
        job, = state.work.jobs.values()
        while state.work.jobs and game.tick < 59:
            game.step()
        self.assertFalse(state.work.jobs)
        mine_id = job.target
        self.assertIn(mine_id, state.initial.mines)
        self.assertEqual(state.mine_deposit[mine_id], sid)
        self.assertEqual(state.initial.mines[mine_id].grade * 1000, site.grade_mU)
        before = state.deposits[sid].remaining_mU
        while game.tick < 60:
            game.step()
        self.assertLess(state.deposits[sid].remaining_mU, before)
        state.audit()

    def test_world_gate_rejects_failed_bootstraps_without_rescue(self):
        calls = []

        def reject(world):
            calls.append(world.attempt)
            return BootstrapResult(False, 150, ('rejected',))

        def accept_third(world):
            return BootstrapResult(world.attempt >= 2, 150, ())

        accepted = world_atlas.generate_world(1000, accept_third)
        self.assertTrue(accepted.accepted)
        self.assertGreaterEqual(accepted.attempt, 2)
        limit = world_atlas.PROFILE['maximum_attempts']
        world_atlas.PROFILE['maximum_attempts'] = 3
        try:
            with self.assertRaises(InfrastructureFault):
                world_atlas.generate_world(1000, reject)
        finally:
            world_atlas.PROFILE['maximum_attempts'] = limit
        self.assertEqual(calls, [0, 1, 2])


if __name__ == '__main__':
    unittest.main()

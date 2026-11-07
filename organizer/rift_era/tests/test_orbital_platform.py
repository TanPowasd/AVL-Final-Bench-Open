"""03 N11 orbital industrial platform (orbital_platform_v1)."""
import unittest
from types import SimpleNamespace

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.industry_commands import IndustryCommands
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.orbital_operations import Launcher, OrbitalCommands, Platform
from organizer.rift_era.engine.world import generate_candidate


class OrbitalPlatformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def setup(self, ticks=60, life_cycles=360):
        game = opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=ticks,
                               commerce=True, debug=False)
        state = game.state
        orbit, inv = state.orbit, state.initial.inventory
        state.research['n0'].known.update({'N06', 'N09', 'N11', 'I11'})
        # Test placement: a platform already in orbit and one reusable launcher at its base.
        platform = Platform('platform:test', 'n0', 'n0-h0', (0, 0, 0), 0, life_cycles * 10)
        launcher = Launcher('launcher:test', 'n0', 'n0-h0')
        stock = {orbit.platform_account(platform): 1000, orbit.launcher_account(launcher): 1000,
                 **{Account('n0', 'n0-h0', 'available', g): q for g, q in
                    {'composite': 20000, 'clean_substrate': 10000, 'superalloy': 20000, 'rare_ore': 10000,
                     'fuel': 50000}.items()}}
        inv.transform({}, stock, reason='orbital_test_fixture')
        orbit.platforms[platform.id] = platform
        orbit.launchers[launcher.id] = launcher
        return game, state, platform, launcher

    def ctx(self, game):
        return SimpleNamespace(tick=game.tick, cycle=game.tick // 10, seed=1000, clock=game.clock)

    def test_launch_needs_n11_and_platform_runs_only_orbital_recipes(self):
        game, state, platform, _ = self.setup()
        state.research['n1'].known.update({'N06'})
        order = {'id': '3', 'kind': 'intelligence', 'op': 'orbital_launch', 'hub_id': 'n1-h0',
                 'payload': 'orbital_platform', 'orbit_plane': 0, 'slot': 1, 'berth': 0, 'use_reusable': False}
        with self.assertRaises(RuleViolation) as e:
            OrbitalCommands(state).prepare('n1', order, self.ctx(game))
        self.assertEqual(e.exception.code, 'ERR_TECH_LOCKED')
        handler = IndustryCommands(state)
        base = {'id': '4', 'kind': 'industry', 'op': 'set_line', 'line_id': platform.id, 'mode': 'general'}
        with self.assertRaises(RuleViolation):
            handler.set_line('n0', dict(base, recipe='P001'), self.ctx(game))
        with self.assertRaises(RuleViolation):
            handler.set_line('n1', dict(base, recipe='P081'), self.ctx(game))
        handler.set_line('n0', dict(base, recipe='P081'), self.ctx(game)).commit()
        self.assertEqual((platform.recipe, platform.version), ('P081', 1))

    def test_ferry_up_produce_x15_ferry_down(self):
        game, state, platform, launcher = self.setup()
        orbit, inv = state.orbit, state.initial.inventory
        platform.recipe = 'P081'          # composite 2, clean_substrate 1, superalloy 1, rare_ore 1 -> metamaterial 1
        hub = lambda g: inv.balance(Account('n0', 'n0-h0', 'available', g))
        fuel0, alloy0 = hub('fuel'), hub('superalloy')
        while game.tick < 10:
            game.step()
        # Cycle 0 end: nothing in orbit to run; the ferry carried one cycle of inputs up.
        self.assertEqual(inv.balance(orbit.platform_inbound(platform, 'composite')), 2000)
        self.assertEqual(inv.balance(orbit.platform_stock(platform, 'composite')), 0)
        self.assertEqual(launcher.uses, 1)
        self.assertEqual(fuel0 - hub('fuel') >= 2000, True)
        while game.tick < 20:
            game.step()
        # Cycle 1 end: inputs arrived, one batch ran at x1.5, product went down.
        self.assertEqual(inv.balance(orbit.platform_stock(platform, 'composite')), 0)
        self.assertEqual(inv.balance(orbit.platform_downlink(platform, 'metamaterial')), 1500)
        before = hub('metamaterial')
        while game.tick < 30:
            game.step()
        # Cycle 2 end: the product is usable on the ground one cycle after it left orbit.
        self.assertEqual(hub('metamaterial') - before, 1500)
        self.assertEqual(launcher.uses, 3)
        state.audit()

    def test_no_launcher_means_no_ferry_and_expiry_destroys_the_platform(self):
        game, state, platform, launcher = self.setup(ticks=30, life_cycles=2)
        orbit, inv = state.orbit, state.initial.inventory
        platform.recipe = 'P081'
        launcher.busy = 'elsewhere'
        while game.tick < 10:
            game.step()
        self.assertEqual(inv.balance(orbit.platform_inbound(platform, 'composite')), 0)
        launcher.busy = None
        while game.tick < 20:
            game.step()
        self.assertNotIn(platform.id, orbit.platforms)
        self.assertEqual(inv.balance(orbit.platform_account(platform)), 0)
        self.assertEqual(inv.balance(orbit.platform_inbound(platform, 'composite')), 0)
        state.audit()


if __name__ == '__main__':
    unittest.main()

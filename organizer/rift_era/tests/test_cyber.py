"""03 section 4 intelligence/cyber_op and steal_model (cyber_v1)."""
import unittest
from fractions import Fraction as Q
from types import SimpleNamespace as NS

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.cyber import CyberOp
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.world import generate_candidate


class CyberTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def game(self):
        game = opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=30, commerce=True, debug=False)
        state = game.state
        state.research['n0'].known.update({'S03', 'S08'})
        for good in ('cyber_suite', 'computing_equipment', 'ew_module'):
            account = Account('n0', 'n0-h0', 'available', good)
            state.initial.inventory.transform({}, {account: 10000}, reason='test_fixture', origins={account: Origin('n0', 'n0-h0')})
        return game, state

    @staticmethod
    def order(i, kind, hub='n1-h0'):
        return {'id': str(i), 'kind': 'intelligence', 'op': 'cyber_op', 'target_nation': 'n1', 'target_hub': hub, 'op_kind': kind}

    def last(self, state):
        return state.command_streams['n0'].recent[-1]

    def test_s03_costs_inputs_records_loss_and_respects_cooldown(self):
        game, state = self.game()
        stock = Account('n0', 'n0-h0', 'available', 'cyber_suite')
        game.step({'n0': [self.order(1, 'S03_infiltrate')]})
        self.assertEqual(self.last(state).status, 'ACCEPTED')
        self.assertEqual(state.initial.inventory.balance(stock), 9000)
        self.assertGreater(state.cyber.action_cycles[0]['n0'], 0)
        report = state.cyber.recent['n0'][-1]
        self.assertEqual(report['op_kind'], 'S03_infiltrate')
        if report['success']:
            self.assertGreater(state.cyber_frozen_until['n1-h0'], 0)
        for _ in range(4):
            game.step()
        game.step({'n0': [self.order(2, 'S03_infiltrate')]})
        self.assertEqual(self.last(state).code, 'ERR_CAPACITY_EXCEEDED')
        state.audit()

    def test_success_probability_follows_levels_and_s04(self):
        game, state = self.game()
        p, s04 = state.cyber.probability('n0', 'n1', 'n1-h0')
        tier = self.catalog.technologies['S08']['tier']
        self.assertFalse(s04)
        self.assertEqual(p, min(Q(9, 10), Q(3, 5) + Q(8, 100) * tier))

    def test_s08_always_attributed_freezes_plans_and_costs_credit(self):
        game, state = self.game()
        before = state.credit['n0']
        game.step({'n0': [self.order(1, 'S08_paralyze')]})
        self.assertEqual(self.last(state).status, 'ACCEPTED')
        self.assertEqual(state.credit['n0'], before - Q(1, 10))
        self.assertEqual(state.cyber_frozen_until['n1-h0'], 5)
        self.assertTrue(state.cyber.recent['n1'])                      # the target learns who did it
        for _ in range(4):
            game.step()
        game.step({'n0': [self.order(2, 'S08_paralyze', hub='n1-h1')]})
        self.assertEqual(self.last(state).code, 'ERR_CAPACITY_EXCEEDED')

    def test_successful_steal_tech_gives_one_discounted_project(self):
        game, state = self.game()
        victim = state.research['n1']
        lab, tech = next((l, t) for l, t in victim.focus.items() if state.initial.lines[l].hub == 'n1-h0')
        op = CyberOp('cyber:x', '1', 'n0', 'n1', 'n1-h0', 'S03_steal_tech', 0)
        state.cyber._effect(op, Q(1, 2), NS(tick=1, cycle=0, clock=ClockConfig()))
        project = state.research['n0'].projects[tech]
        self.assertTrue(project.cost_modifiers['stolen'])
        plain = state.research['n0'].target_cost(tech, {'licensed': False, 'open_spillover': False, 'stolen': False,
                                                         'source_blockade': False})
        self.assertLess(project.target_mU, plain)
        victim.security[lab] = 'classified'
        del state.research['n0'].projects[tech]
        state.cyber._effect(op, Q(1, 2), NS(tick=2, cycle=0, clock=ClockConfig()))
        self.assertNotIn(tech, state.research['n0'].projects)

    def test_infiltrated_capital_suspends_the_next_auction_once(self):
        game, state = self.game()
        op = CyberOp('cyber:y', '1', 'n0', 'n1', 'n1-h0', 'S03_infiltrate', 0)
        state.cyber._effect(op, Q(1, 2), NS(tick=1, cycle=0, clock=ClockConfig()))
        self.assertEqual(state.cyber_frozen_until['n1-h0'], 1 + 3 * 5)
        self.assertTrue(state.cyber.take_auction_suspension('n1'))
        self.assertFalse(state.cyber.take_auction_suspension('n1'))

    def test_steal_model_needs_a_target_data_centre(self):
        game, state = self.game()
        game.step({'n0': [{'id': '1', 'kind': 'intelligence', 'op': 'steal_model', 'target_nation': 'n1',
                           'target_hub': 'n1-h0'}]})
        has_dc = any(c.owner == 'n1' and c.center.hub == 'n1-h0' for c in state.compute.centers.values())
        self.assertEqual(self.last(state).status if has_dc else self.last(state).code,
                         'ACCEPTED' if has_dc else 'ERR_SITE_INCOMPATIBLE')


if __name__ == '__main__':
    unittest.main()

"""06 C10 governance/conditional_plan (conditional_plan_v1)."""
import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate

LAB = 'n0/P075/0/line'


class ConditionalPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.world = generate_candidate(1000)

    def game(self, ticks=30):
        game = opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=ticks,
                               debug=False, commerce=True)
        return game

    def enable(self, state):
        state.research['n0'].known.add('C10')
        host = next(l.id for l in state.initial.lines.values() if l.owner == 'n0' and l.hub == 'n0-h0' and l.id != LAB)
        state.line_options.setdefault(host, {})['addons'] = ('ai_core',)
        return host

    @staticmethod
    def plan(i, threshold=0, comparator='>=', cooldown=5, action=None):
        return {'id': i, 'kind': 'governance', 'op': 'conditional_plan', 'hub_id': 'n0-h0', 'metric': 'hub_stock',
                'ref_id': 'food', 'comparator': comparator, 'threshold_mU': threshold, 'cooldown_ticks': cooldown,
                'action': action or {'kind': 'research', 'op': 'set_focus', 'line_id': LAB, 'tech_id': None,
                                     'sec_mode': 'standard'}}

    def last(self, state):
        return state.command_streams['n0'].recent[-1]

    def test_registration_requires_c10_and_an_ai_core_host(self):
        game = self.game()
        state = game.state
        game.step({'n0': (self.plan(1),)})
        self.assertEqual(self.last(state).code, 'ERR_TECH_LOCKED')
        state.research['n0'].known.add('C10')
        for _ in range(4):
            game.step()
        game.step({'n0': (self.plan(2),)})
        self.assertEqual(self.last(state).code, 'ERR_SITE_INCOMPATIBLE')
        for _ in range(4):
            game.step()
        bad = self.plan(3, action={'kind': 'governance', 'op': 'conditional_plan'})
        self.enable(state)
        game.step({'n0': (bad, self.plan(4, cooldown=3))})
        self.assertEqual([r.code for r in state.command_streams['n0'].recent[-2:]], ['ERR_SCHEMA', 'ERR_SCHEMA'])

    def test_triggered_action_runs_once_per_window_from_the_next_window(self):
        game = self.game()
        state = game.state
        self.enable(state)
        research = state.research['n0']
        self.assertIn(LAB, research.focus)
        game.step({'n0': (self.plan(1),)})
        self.assertEqual(self.last(state).status, 'ACCEPTED')
        self.assertIn(LAB, research.focus)        # registered, not yet evaluated
        plan = state.conditional_plans['n0']['1']
        while game.tick < 5:
            game.step()
        game.step()                                # tick 5: first P1 of the next window
        self.assertNotIn(LAB, research.focus)
        self.assertEqual((plan.executions, plan.last_result['status']), (1, 'ACCEPTED'))
        while game.tick < 11:
            game.step()
        self.assertEqual(plan.executions, 2)       # cooldown 5 = one execution per window
        state.audit()

    def test_threshold_not_met_never_fires_and_cancel_removes_the_plan(self):
        game = self.game()
        state = game.state
        self.enable(state)
        game.step({'n0': (self.plan(1, threshold=10**15),)})
        while game.tick < 10:
            game.step()
        self.assertEqual(state.conditional_plans['n0']['1'].executions, 0)
        game.step({'n0': ({'id': 2, 'kind': 'cancel', 'op': 'cancel_by_id', 'target_id': '1'},)})
        self.assertEqual(self.last(state).status, 'ACCEPTED')
        self.assertNotIn('1', state.conditional_plans['n0'])

    def test_lost_host_suspends_evaluation(self):
        game = self.game()
        state = game.state
        host = self.enable(state)
        game.step({'n0': (self.plan(1),)})
        state.initial.assets[host].health_mU = 0
        while game.tick < 11:
            game.step()
        self.assertEqual(state.conditional_plans['n0']['1'].executions, 0)


if __name__ == '__main__':
    unittest.main()

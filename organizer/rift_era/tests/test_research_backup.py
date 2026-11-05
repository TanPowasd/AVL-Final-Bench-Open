"""06 research/set_backup_hub and 04 R08 research continuity on laboratory loss."""
import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate

LAB = 'n0/P075/0/line'


class ResearchBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.world = generate_candidate(1000)

    def game(self, ticks=20):
        return opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=ticks,
                               debug=False, commerce=True)

    @staticmethod
    def order(i, primary='n0-h0', backup='n0-h1'):
        return {'id': i, 'kind': 'research', 'op': 'set_backup_hub', 'primary_hub': primary, 'backup_hub': backup}

    def receipt(self, state):
        return state.command_streams['n0'].recent[-1]

    def with_backup_centre(self, state, hub='n0-h1'):
        original = state.systems.online
        state.systems.online = lambda h, kind, owner=None: (kind == 'research_backup' and h == hub and owner in (None, 'n0')) or original(h, kind, owner=owner)

    def test_command_requires_r08_and_an_online_backup_centre(self):
        game = self.game()
        state = game.state
        game.step({'n0': (self.order(1),)})
        self.assertEqual(self.receipt(state).code, 'ERR_TECH_LOCKED')
        state.research['n0'].known.add('R08')
        for _ in range(4):
            game.step()
        game.step({'n0': (self.order(2),)})
        self.assertEqual(self.receipt(state).code, 'ERR_SITE_INCOMPATIBLE')
        for _ in range(4):
            game.step()
        game.step({'n0': (self.order(3, backup='n1-h1'),)})
        self.assertEqual(self.receipt(state).code, 'ERR_NO_PERMISSION')
        self.with_backup_centre(state)
        for _ in range(4):
            game.step()
        game.step({'n0': (self.order(4),)})
        self.assertEqual(self.receipt(state).status, 'ACCEPTED')
        self.assertEqual(state.research['n0'].backups, {'n0-h0': 'n0-h1'})

    def lose_lab(self, backup):
        game = self.game()
        state = game.state
        research = state.research['n0']
        project = research.projects[research.focus[LAB]]
        if backup:
            research.known.add('R08')
            self.with_backup_centre(state)
            research.backups['n0-h0'] = 'n0-h1'
        for _ in range(5):
            game.step()
        self.assertGreater(project.theory_mU, 0)
        state.initial.assets[LAB].health_mU = 0
        while game.tick < 9:
            game.step()
        theory = project.theory_mU  # a zero-health lab produces nothing more
        game.step()  # tick 9 is the cycle end
        self.assertNotIn(LAB, research.focus)
        return theory, project

    def test_unprotected_lab_loss_keeps_thirty_percent_of_theory(self):
        before, project = self.lose_lab(False)
        self.assertLessEqual(project.theory_mU, before * 30 // 100)
        self.assertEqual(project.validation_progress_mU, 0)

    def test_r08_backup_keeps_ninety_percent_of_theory(self):
        before, project = self.lose_lab(True)
        self.assertGreaterEqual(project.theory_mU, before * 90 // 100)
        self.assertLess(project.theory_mU, before + 1)


if __name__ == '__main__':
    unittest.main()

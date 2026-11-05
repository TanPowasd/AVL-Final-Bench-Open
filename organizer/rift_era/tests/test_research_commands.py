import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.world import generate_candidate


class ResearchCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.world = generate_candidate(1000)

    def game(self, ticks=50):
        return opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=ticks,
                               commerce=True, debug=False)

    def command(self, op, **fields):
        return {'id': 1, 'kind': 'research', 'op': op, 'line_id': 'n0/P075/0/line', **fields}

    def ready(self, engine):
        project = engine.state.research['n0'].projects['I01']
        # Explicit fixture precondition: a project whose theory was completed
        # before this scenario. Validation below must still pay every real cost.
        project.advance_theory(project.target_mU)
        return project

    def test_focus_change_affects_this_tick_and_idle_labs_do_not_consume(self):
        engine = self.game(10)
        engine.step({'n0': (self.command('set_focus', tech_id=None, sec_mode='standard'),)})
        line = engine.state.initial.lines['n0/P075/0/line']
        self.assertFalse(line.used_nmU)
        self.assertNotIn(line.id, engine.state.frame.labor)
        for _ in range(4):
            engine.step()
        engine.step({'n0': ({**self.command('set_focus', tech_id='I01', sec_mode='open'), 'id': 2},)})
        self.assertGreater(engine.state.research['n0'].projects['I01'].theory_mU, 0)
        self.assertGreater(line.used_nmU['lab_supplies'], 0)
        engine.state.audit()

    def test_theory_alone_never_unlocks_and_validation_has_real_duration_and_inputs(self):
        engine = self.game()
        project = self.ready(engine)
        self.assertNotIn('I01', engine.state.research['n0'].known)
        engine.step({'n0': (self.command('validate_project', tech_id='I01'),)})
        self.assertEqual(engine.state.command_streams['n0'].recent[-1].status, 'ACCEPTED')
        for _ in range(38):
            engine.step()
        self.assertFalse(project.verified)
        self.assertEqual(project.validation_progress_mU, 39000)
        engine.step()
        self.assertTrue(project.verified)
        self.assertIn('I01', engine.state.initial.known['n0'])
        self.assertEqual(engine.state.consumption.consumption20('n0', 'lab_supplies'), 4000)
        self.assertFalse(engine.state.initial.lines['n0/P075/0/line'].used_nmU)
        self.assertEqual(project.theory_mU, project.target_mU)
        engine.state.audit()

    def test_continuous_outage_causes_one_interruption_not_repeated_progress_destruction(self):
        engine = self.game()
        project = self.ready(engine)
        engine.step({'n0': (self.command('validate_project', tech_id='I01'),)})
        self.assertEqual(project.validation_progress_mU, 1000)
        for lid, plant in engine.state.initial.plants.items():
            if lid.startswith('n0/'):
                plant.online = False
        engine.step()
        self.assertEqual(project.validation_progress_mU, 500)
        for _ in range(3):
            engine.step()
            self.assertEqual(project.validation_progress_mU, 500)
        self.assertNotIn('I01', engine.state.research['n0'].known)
        engine.state.audit()

    def test_pause_validation_does_not_erase_completed_theory(self):
        engine = self.game()
        project = self.ready(engine)
        engine.step({'n0': (self.command('validate_project', tech_id='I01'),)})
        for _ in range(4):
            engine.step()
        engine.step({'n0': ({**self.command('set_focus', tech_id=None, sec_mode='standard'), 'id': 2},)})
        self.assertTrue(project.theory_complete)
        self.assertEqual(project.theory_mU, project.target_mU)
        self.assertEqual(project.validation_progress_mU, 2500)
        self.assertFalse(any(j.active for j in engine.state.validation_jobs.values()))
        self.assertFalse(any(e.startswith('validation:') for e in engine.state.frame.batches))
        engine.state.audit()

    def test_duplicate_validation_cannot_double_the_laboratory_slot_or_progress(self):
        engine = self.game(10)
        self.ready(engine)
        command = self.command('validate_project', tech_id='I01')
        engine.step({'n0': (command, {**command, 'id': 2})})
        receipts = engine.state.command_streams['n0'].recent
        self.assertEqual([r.status for r in receipts], ['ACCEPTED', 'REJECTED'])
        self.assertEqual(receipts[-1].code, 'ERR_CAPACITY_EXCEEDED')
        self.assertEqual(engine.state.research['n0'].projects['I01'].validation_progress_mU, 1000)

    def test_classified_research_pays_surcharge_to_real_local_account(self):
        engine = self.game(10)
        engine.step({'n0': (self.command('set_focus', tech_id='I01', sec_mode='classified'),)})
        state = engine.state
        lid = 'n0/P075/0/line'
        self.assertEqual(state.research['n0'].projects['I01'].theory_mU, 90)
        base = state.frame.operating_paid_mU[lid]
        self.assertGreater(base, 0)
        key = (state.frame.classified_locks[lid], state.native('n0', 'household:n0-h0'), 'classified_operations')
        self.assertEqual(state.payments.residuals[key], base / 5 - int(base / 5))
        self.assertEqual(state.initial.cash.balance(state.frame.classified_locks[lid]).native_mU, 0)
        self.assertGreaterEqual(state.cycles['n0'].costs['corp:tech'], base + int(base / 5))
        state.audit()

    def test_bad_project_and_stale_version_preserve_old_focus(self):
        engine = self.game(10)
        original = engine.state.research['n0'].focus.copy()
        command = self.command('set_focus', tech_id='I12', sec_mode='standard')
        engine.step({'n0': (command, {**command, 'id': 2, 'tech_id': None, 'expected_version': 999})})
        self.assertEqual(engine.state.research['n0'].focus, original)
        self.assertEqual([r.code for r in engine.state.command_streams['n0'].recent], ['ERR_TECH_LOCKED', 'ERR_STALE_VERSION'])


if __name__ == '__main__':
    unittest.main()

"""07: 64-seed batch with shadow baselines, resumable journal and INVALID_EVALUATION."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from organizer.rift_era.engine.batch_runner import INVALID_EVALUATION, BatchRunner
from organizer.rift_era.engine.errors import InfrastructureFault
from organizer.rift_era.engine.evaluation import Batch
from organizer.rift_era.engine.match import seat_score

NATIONS = [f'n{i}' for i in range(8)]


def report(v_seat, seat, other=5):
    return {'scope': 'full_match', 'complete_trajectory': True, 'full_rules_accepted': False,
            'V': {p: ([v_seat, 1] if i == seat else [other, 1]) for i, p in enumerate(NATIONS)}}


class BatchRunnerTests(unittest.TestCase):
    def setUp(self):
        self.batch = Batch.create(rules_hash='r', engine_hash='e', reference_hash='f',
                                  runtime_hash='t', resource_plan_hash='p')
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'journal.jsonl'
        self.calls = []

    def tearDown(self):
        self.dir.cleanup()

    def fake(self, seed, seat, role):
        self.calls.append((seed, role))
        return report(20 if role == 'player' else 10, seat)

    def test_full_batch_scores_mean_of_seat_scores(self):
        result = BatchRunner(self.batch, self.path, self.fake).run()
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.done, 128)
        self.assertFalse(result.certified)
        one = seat_score(20, 10)
        self.assertLessEqual(Decimal(result.score_lower), Decimal(one['score_lower']))
        self.assertGreaterEqual(Decimal(result.score_upper), Decimal(one['score_upper']))
        self.assertLess(Decimal(result.score_upper) - Decimal(result.score_lower), Decimal('1e-40'))
        self.assertGreater(Decimal(result.score_lower), 50)
        # Seats follow k mod 8.
        self.assertEqual([s for s, r in self.calls[:2]], [self.batch.seeds[0]] * 2)

    def test_resume_skips_finished_games(self):
        first = BatchRunner(self.batch, self.path, self.fake).run(limit=10)
        self.assertEqual((first.status, first.done), ('incomplete', 10))
        self.calls.clear()
        second = BatchRunner(self.batch, self.path, self.fake).run()
        self.assertEqual(len(self.calls), 118)
        self.assertEqual(second.status, 'complete')

    def test_host_fault_voids_the_whole_batch(self):
        def flaky(seed, seat, role):
            if len(self.calls) == 7:
                raise InfrastructureFault('reference strategy crashed')
            return self.fake(seed, seat, role)
        result = BatchRunner(self.batch, self.path, flaky).run()
        self.assertEqual(result.status, INVALID_EVALUATION)
        self.assertIn('reference strategy crashed', result.reason)
        # Resuming keeps it invalid and runs nothing more.
        self.calls.clear()
        again = BatchRunner(self.batch, self.path, self.fake).run()
        self.assertEqual(again.status, INVALID_EVALUATION)
        self.assertEqual(self.calls, [])

    def test_short_trajectory_gives_no_score_and_foreign_journal_refused(self):
        def short(seed, seat, role):
            return report(20, seat) | {'complete_trajectory': False}
        result = BatchRunner(self.batch, self.path, short).run()
        self.assertEqual(result.status, 'incomplete')
        self.assertIsNone(result.score_lower)
        other = Batch.create(rules_hash='r', engine_hash='e', reference_hash='f',
                             runtime_hash='t', resource_plan_hash='p')
        with self.assertRaises(InfrastructureFault):
            BatchRunner(other, self.path, self.fake)


if __name__ == '__main__':
    unittest.main()

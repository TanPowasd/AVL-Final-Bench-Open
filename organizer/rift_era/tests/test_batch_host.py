"""batch_host_v1: real full_match games under BatchRunner and fault attribution."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest

from organizer.rift_era.engine.batch_host import ReferenceFault, match_runner
from organizer.rift_era.engine.batch_runner import INVALID_EVALUATION, BatchRunner
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.errors import InfrastructureFault, InvariantViolation
from organizer.rift_era.engine.evaluation import Batch
from organizer.rift_era.engine.ipc import StrategySession
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.tests.test_ipc import TrustedFixture

TICKS = 20  # two decision windows of a real full_match

IDLE = '''
for row in messages():
    if row['type'] == 'init':
        send({'type': 'ready'})
    elif row['type'] == 'obs':
        send({'type': 'orders', 'window': row['window'], 'orders': []})
    else:
        break
'''

CRASH_AT_WINDOW_1 = '''
for row in messages():
    if row['type'] == 'init':
        send({'type': 'ready'})
    elif row['type'] == 'obs':
        if row['window'] >= 1:
            raise SystemExit(3)
        send({'type': 'orders', 'window': row['window'], 'orders': []})
    else:
        break
'''


def seat(source):
    @contextmanager
    def factory(owner, seed, role):
        session = StrategySession(TrustedFixture(source))
        try:
            yield session
        finally:
            session.close()
    return factory


class BatchHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.worlds = {}

    def world(self, seed):
        # The batch's random 64-bit seeds all map to one cached development map
        # here; the seed still drives the game RNG through full_match.
        if 'w' not in self.worlds:
            self.worlds['w'] = generate_candidate(1000)
        return self.worlds['w']

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.batch = Batch.create(rules_hash=self.catalog.fingerprint, engine_hash='e', reference_hash='idle',
                                  runtime_hash='t', resource_plan_hash='p')

    def runner(self, submission=IDLE, reference=IDLE, world=None):
        return match_runner(submission=seat(submission), reference=seat(reference),
                            replay_dir=self.root / 'replays', ticks=TICKS, catalog=self.catalog,
                            world=world or self.world)

    def test_real_games_journal_and_resume(self):
        journal = self.root / 'journal.jsonl'
        first = BatchRunner(self.batch, journal, self.runner()).run(limit=1)
        self.assertEqual((first.status, first.done), ('incomplete', 1))
        second = BatchRunner(self.batch, journal, self.runner()).run(limit=1)
        self.assertEqual((second.status, second.done), ('incomplete', 2))
        rows = [json.loads(line) for line in journal.read_bytes().splitlines()]
        reports = [r for r in rows if r['kind'] == 'report']
        self.assertEqual([r['role'] for r in reports], ['player', 'shadow'])
        seed = self.batch.seeds[0]
        self.assertEqual({r['seed'] for r in reports}, {seed})
        for row in reports:
            report = row['report']
            self.assertEqual(report['scope'], 'full_match')
            self.assertTrue(report['valid'])
            self.assertEqual(report['ticks'], TICKS)
            self.assertFalse(report['complete_trajectory'])      # 20 ticks, not 2880 cycles
            self.assertFalse(any(report['faults'].values()))
        self.assertEqual(reports[0]['report']['submission_owner'], f'n{self.batch.seats[0]}')
        self.assertIsNone(reports[1]['report']['submission_owner'])
        # Same seed, same strategies: player and shadow games are the same game here.
        self.assertEqual(reports[0]['report']['state_hash'], reports[1]['report']['state_hash'])
        self.assertTrue((self.root / 'replays' / f'{seed}-player-{self.batch.seats[0]}.jsonl').exists())

    def test_submission_crash_is_custodial_and_scored(self):
        result = BatchRunner(self.batch, self.root / 'j.jsonl', self.runner(submission=CRASH_AT_WINDOW_1)).run(limit=1)
        self.assertEqual(result.status, 'incomplete')
        rows = [json.loads(line) for line in (self.root / 'j.jsonl').read_bytes().splitlines()]
        report = rows[1]['report']
        owner = report['submission_owner']
        self.assertTrue(report['faults'][owner])
        self.assertTrue(report['strategy_time'][owner]['custodial'])
        self.assertFalse(any(v for p, v in report['faults'].items() if p != owner))

    def test_reference_crash_voids_the_batch(self):
        journal = self.root / 'j.jsonl'
        result = BatchRunner(self.batch, journal, self.runner(reference=CRASH_AT_WINDOW_1)).run()
        self.assertEqual(result.status, INVALID_EVALUATION)
        self.assertIn('ReferenceFault', result.reason)
        self.assertEqual(result.done, 0)
        # The journal keeps the verdict; a resumed runner does not play on.
        again = BatchRunner(self.batch, journal, self.runner())
        self.assertEqual(again.run().status, INVALID_EVALUATION)
        self.assertEqual(len(again.reports), 0)

    def test_engine_fault_voids_the_batch(self):
        from organizer.rift_era.engine import national_power
        original = national_power.NationalPower.advance

        def broken(self, *args, **kwargs):
            raise InvariantViolation('injected judge fault')
        national_power.NationalPower.advance = broken
        try:
            result = BatchRunner(self.batch, self.root / 'j.jsonl', self.runner()).run()
        finally:
            national_power.NationalPower.advance = original
        self.assertEqual(result.status, INVALID_EVALUATION)
        self.assertIn('injected judge fault', result.reason)

    def test_host_fault_before_the_game_voids_the_batch(self):
        def no_map(seed):
            raise OSError('map store unavailable')
        result = BatchRunner(self.batch, self.root / 'j.jsonl', self.runner(world=no_map)).run()
        self.assertEqual(result.status, INVALID_EVALUATION)
        self.assertIn('OSError', result.reason)

    def test_reference_guard_rejects_custodial_decisions(self):
        from organizer.rift_era.engine.batch_host import _GuardedReference
        from organizer.rift_era.engine.ipc import Decision

        class Stub:
            def decide(self, observation):
                return Decision(1, (), 'timeout', ('timeout',), 0, 0, True)
        with self.assertRaises(ReferenceFault):
            _GuardedReference('n3', Stub()).decide({'window': 1})
        self.assertTrue(issubclass(ReferenceFault, InfrastructureFault))


if __name__ == '__main__':
    unittest.main()

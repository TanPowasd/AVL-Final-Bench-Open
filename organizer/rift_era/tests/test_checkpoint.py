"""Development checkpoints: a full_match resumed from disk matches the uninterrupted run."""
import tempfile
import unittest
from pathlib import Path

from organizer.rift_era.engine import checkpoint
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import full_match
from organizer.rift_era.engine.errors import InfrastructureFault
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate


class CheckpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.world = generate_candidate(1000)

    def test_resume_crosses_cycle_ends_bit_for_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'match.ckpt'
            game = full_match(self.catalog, initialize(self.catalog, self.world), total_ticks=40)
            for _ in range(7):
                game.step()
            saved = checkpoint.save(game, path, rules_hash=self.catalog.fingerprint, world_seed=1000,
                                    map_version=self.world.generator_version)
            self.assertEqual(checkpoint.read_header(path)['tick'], 7)
            while game.tick < 25:
                game.step()
            resumed, header = checkpoint.resume(path, self.catalog)
            self.assertEqual((resumed.tick, header['state_hash']), (7, saved))
            while resumed.tick < 25:
                resumed.step()
            self.assertEqual(fingerprint(resumed.state.snapshot()), fingerprint(game.state.snapshot()))
            self.assertEqual(len(resumed.state.national_power.pillars['n0']), 2)

    def test_tampered_or_foreign_checkpoint_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'match.ckpt'
            game = full_match(self.catalog, initialize(self.catalog, self.world), total_ticks=40)
            game.step()
            checkpoint.save(game, path, rules_hash=self.catalog.fingerprint, world_seed=1000,
                            map_version=self.world.generator_version)
            with self.assertRaises(InfrastructureFault):
                checkpoint.load_state(path, rules_hash='other-rules')
            data = bytearray(path.read_bytes())
            data[-10] ^= 0xFF
            path.write_bytes(bytes(data))
            with self.assertRaises(InfrastructureFault):
                checkpoint.load_state(path, rules_hash=self.catalog.fingerprint)
            path.write_bytes(b'garbage')
            with self.assertRaises(InfrastructureFault):
                checkpoint.read_header(path)


if __name__ == '__main__':
    unittest.main()

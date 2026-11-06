"""FULL_MATCH_TEST_TODO section C on the formal full-match assembly (short horizon).

- same rules/seed/seats/command stream under different PYTHONHASHSEED values
  gives the same per-tick state hash chain;
- querying the score 0, 1 or 100 times per cycle changes neither the world,
  the statistics windows nor the result.
"""
import os
import subprocess
import sys
import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import full_match
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate

TICKS = 30
COMMANDS = {
    5: {'n0': [{'id': '1', 'kind': 'diplomacy', 'op': 'declare_war', 'target_nation': 'n1'}],
        'n2': [{'id': '1', 'kind': 'military', 'op': 'deploy_formation', 'hub_id': 'n2-h0',
                'domain': 'Land', 'units': {'motorized': 1}}]},
}

CHAIN = '''
import sys
from organizer.rift_era.tests.test_full_match_determinism import chain
print(chain())
'''


def chain(queries_per_cycle=0):
    catalog = Catalog()
    game = full_match(catalog, initialize(catalog, generate_candidate(1000)), total_ticks=TICKS)
    hashes = []
    for tick in range(TICKS):
        game.step(COMMANDS.get(tick))
        if game.clock.is_cycle_end(tick):
            power = game.state.national_power
            for _ in range(queries_per_cycle):
                for p in power.nations:
                    power.nfa_mU(p, power.cycle - 1)
                    power.phi(p)
                    power.partial_mean(p)
        hashes.append(fingerprint(game.state.snapshot())[:16])
    return ','.join(hashes)


class FullMatchDeterminismTests(unittest.TestCase):
    def test_hash_seed_does_not_change_the_state_chain(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        chains = []
        for seed in ('0', '12345'):
            env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=root)
            out = subprocess.run([sys.executable, '-c', CHAIN], env=env, cwd=root, capture_output=True,
                                 text=True, timeout=600, check=True)
            chains.append(out.stdout.strip())
        self.assertEqual(chains[0], chains[1])
        self.assertEqual(len(chains[0].split(',')), TICKS)

    def test_score_queries_do_not_change_world_or_result(self):
        self.assertEqual(chain(0), chain(1))
        self.assertEqual(chain(0), chain(100))


if __name__ == '__main__':
    unittest.main()

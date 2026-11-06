"""grid_fast_v1: the batched local phase must reproduce the 1 mU oracle exactly."""
import random
import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.power import Load, PowerEdge, Supply, dispatch_reference


def random_grid(rng, scale=1):
    hubs = [f'h{i}' for i in range(rng.randrange(2, 6))]
    edges = []
    for i in range(rng.randrange(1, 7)):
        a, b = rng.sample(hubs, 2)
        edges.append(PowerEdge(f'e{i}', a, b, rng.randrange(0, 60 * scale), Q(rng.randrange(850, 1001), 1000),
                               rng.choice('AB'), Q(rng.randrange(0, 4), 100),
                               denied=frozenset(rng.sample('AB', rng.randrange(0, 2)))))
    supplies = tuple(Supply(f's{i}', rng.choice(hubs), rng.choice('AB'), rng.randrange(0, 50 * scale),
                            Q(rng.randrange(0, 5), rng.choice((1, 2, 10)))) for i in range(rng.randrange(1, 6)))
    loads = []
    for i in range(rng.randrange(1, 7)):
        budget = Q(rng.randrange(0, 200 * scale))
        capped = rng.random() < .4
        loads.append(Load(f'l{i}', rng.choice(hubs), rng.choice('AB'), rng.randrange(0, 40 * scale), rng.randrange(6),
                          Q(rng.randrange(0, 6), rng.choice((1, 2))), budget,
                          user_budget_ru_mU=(budget * Q(rng.randrange(0, 11), 10) if capped else None),
                          user_cap_ru=(Q(rng.randrange(0, 4)) if capped else None)))
    return supplies, tuple(loads), tuple(edges)


class FastGridTests(unittest.TestCase):
    def test_fast_local_phase_matches_the_oracle(self):
        rng = random.Random(20261106)
        for case in range(600):
            supplies, loads, edges = random_grid(rng)
            slow = dispatch_reference(case, 7, supplies, loads, edges)
            fast = dispatch_reference(case, 7, supplies, loads, edges, fast_local=True)
            self.assertEqual((fast.receipts, fast.received, fast.spent, fast.source_remaining, fast.edge_remaining),
                             (slow.receipts, slow.received, slow.spent, slow.source_remaining, slow.edge_remaining),
                             case)

    def test_batched_network_phase_matches_the_oracle(self):
        rng = random.Random(1106)
        batched = 0
        for case in range(250):
            supplies, loads, edges = random_grid(rng, scale=20)
            slow = dispatch_reference(case, 3, supplies, loads, edges)
            fast = dispatch_reference(case, 3, supplies, loads, edges, fast_local=True)
            self.assertEqual((fast.receipts, fast.received, fast.spent, fast.source_remaining, fast.edge_remaining),
                             (slow.receipts, slow.received, slow.spent, slow.source_remaining, slow.edge_remaining),
                             case)
            batched += fast.iterations < slow.iterations
        self.assertGreater(batched, 100)

    def test_denied_source_never_uses_the_line(self):
        edge = PowerEdge('e', 'a', 'b', 100, Q(1), 'A', denied=frozenset({'C'}))
        supplies = (Supply('c', 'a', 'C', 10, Q(0)), Supply('x', 'a', 'A', 10, Q(1, 2)))
        load = Load('l', 'b', 'B', 5, 0, Q(1), Q(100))
        result = dispatch_reference(0, 0, supplies, (load,), (edge,), fast_local=True)
        self.assertEqual({r.source for r in result.receipts}, {'x'})


if __name__ == '__main__':
    unittest.main()

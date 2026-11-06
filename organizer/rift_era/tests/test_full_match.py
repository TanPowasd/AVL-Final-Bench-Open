"""P0-1 full-match assembly and finalizer; P0-2 per-cycle national power."""
import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import full_match, opening_economy
from organizer.rift_era.engine.errors import InfrastructureFault
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.match import finalize_match, seat_score
from organizer.rift_era.engine.scoring import Trajectory, adjusted_end
from organizer.rift_era.engine.world import generate_candidate


class FullMatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.world = generate_candidate(1000)

    def game(self, ticks):
        return full_match(self.catalog, initialize(self.catalog, self.world), total_ticks=ticks)

    def test_opening_stays_capped_and_full_match_uses_the_official_clock(self):
        with self.assertRaises(InfrastructureFault):
            opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=160)
        game = full_match(self.catalog, initialize(self.catalog, self.world))
        self.assertTrue(game.clock.official)
        self.assertEqual(game.clock.total_ticks, 28_800)
        game.step()
        self.assertEqual(game.tick, 1)

    def test_one_pillar_row_per_cycle_and_reads_do_not_change_state(self):
        game = self.game(20)
        state = game.state
        power = state.national_power
        for _ in range(20):
            game.step()
        self.assertEqual(power.cycle, 2)
        for p in power.nations:
            self.assertEqual(len(power.pillars[p]), 2)
            self.assertEqual(power.trajectories[p].cycles, 2)
            self.assertGreater(power.pillars[p][-1].population, 0)
        before = fingerprint(state.snapshot())
        for _ in range(3):
            for p in power.nations:
                power.nfa_mU(p, 1)
                power.phi(p)
                power.partial_mean(p)
            finalize_match(game)
        self.assertEqual(fingerprint(state.snapshot()), before)
        state.audit()

    def test_national_power_is_deterministic(self):
        rows = []
        for _ in range(2):
            game = self.game(10)
            for _ in range(10):
                game.step()
            rows.append(fingerprint(game.state.national_power.snapshot()))
        self.assertEqual(rows[0], rows[1])

    def test_finalizer_refuses_early_and_never_invents_v(self):
        game = self.game(10)
        game.step()
        with self.assertRaises(InfrastructureFault):
            finalize_match(game)
        for _ in range(9):
            game.step()
        report = finalize_match(game)
        self.assertFalse(report['complete_trajectory'])
        self.assertFalse(report['full_rules_accepted'])
        self.assertNotIn('V', report)
        self.assertEqual(set(report['mean_U']), set(game.state.initial.known))
        self.assertTrue(report['coverage_gaps'])

    def test_trajectory_v_and_seat_score(self):
        trajectory = Trajectory()
        for cycle in range(2880):
            trajectory.commit(cycle, Q(20))
        phi = Q(9, 10)
        expected = (Q(8, 100) + Q(14, 100) + Q(24, 100) + Q(29, 100)) * 20 + adjusted_end(Q(20), phi) / 4
        self.assertEqual(trajectory.value(phi), expected)
        same = seat_score(Q(40), Q(40))
        self.assertEqual((same['score_lower'], same['score_upper']), ('50', '50'))
        better = seat_score(Q(60), Q(40))
        self.assertGreater(float(better['score_lower']), 50)


if __name__ == '__main__':
    unittest.main()

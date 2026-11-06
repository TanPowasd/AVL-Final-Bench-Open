"""10 couplings C24 (reserve life), C25 (war-zone outages), C19 (AI drone fire)."""
import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.economy_state import CycleEconomy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.scoring import sqrt_q
from organizer.rift_era.engine.world import generate_candidate


class CouplingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def game(self):
        return opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=20, commerce=True, debug=False)

    def test_c24_short_reserve_life_discounts_only_mining(self):
        state = self.game().state
        market = state.stock_market
        self.assertEqual(market.reserve_life_factor('n0-mining'), 1)    # no extraction history yet
        finite = {state.mine_deposit[m] for m, mine in state.initial.mines.items()
                  if mine.owner == 'n0' and mine.good not in ('water', 'biomass')}
        remaining = sum(state.deposits[d].remaining_mU for d in finite)
        row = CycleEconomy()
        row.extracted = {'coal': remaining // 2}   # 20-cycle mean = remaining/40 -> life 40 cycles
        state.history['n0'].append(row)
        self.assertEqual(market.reserve_life_factor('n0-mining'), sqrt_q(Q(40, 200)))
        self.assertEqual(market.reserve_life_factor('n0-tech'), 1)
        row.extracted = {'coal': remaining * 100}
        self.assertEqual(market.reserve_life_factor('n0-mining'), Q(3, 10))  # floor

    def test_c25_outage_rises_only_in_a_war_zone(self):
        state = self.game().state
        battles = state.battles
        region = state.initial.map.hubs['n0-h0'].region
        self.assertEqual(battles.war_zone_outage('n0', 'n0-h0', 0), 1)
        battles.combat_cycle = {'cycle': 0, 'ticks': {region: 5}}
        # Combat = .5 -> Qsec = .8 -> x1.4
        self.assertEqual(battles.war_zone_outage('n0', 'n0-h0', 0), Q(7, 5))
        battles.combat_cycle = {'cycle': 0, 'ticks': {region: 20}}
        self.assertEqual(battles.war_zone_outage('n0', 'n0-h0', 0), Q(9, 5))
        self.assertEqual(battles.war_zone_outage('n0', 'n0-h0', 1), 1)   # stale cycle

    def test_c19_drone_boost_needs_ai09_and_cyber_compute(self):
        state = self.game().state
        formation = next(f for f in state.formations.rows.values() if f.owner == 'n0' and f.hub)
        drone = state.initial.units[formation.unit_ids[0]]
        drone.unit_type = 'ground_drone'  # fixture: re-type one garrison unit as a drone
        self.assertEqual(state.battles.drone_boost(), {})
        state.research['n0'].known.add('AI09')
        deployment = state.ai_deployment
        deployment.previous_strength = lambda owner, hub, task, threshold: Q(30) if task == 'cyber' else Q(0)
        boosted = state.battles.drone_boost()
        self.assertTrue(boosted)
        self.assertEqual(set(boosted), {drone.id})
        for uid, unit in boosted.items():
            self.assertEqual(unit.tech_multiplier, state.initial.units[uid].tech_multiplier * Q(112, 100))
            self.assertIsNot(unit, state.initial.units[uid])


if __name__ == '__main__':
    unittest.main()

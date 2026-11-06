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

    def test_c18_fusion_narrowing_needs_ai09_and_an_online_fusion_system(self):
        from types import SimpleNamespace as NS
        state = self.game().state
        vision = state.vision
        self.assertEqual(vision.fusion_narrowing('n0'), 0)
        state.research['n0'].known.add('AI09')
        self.assertEqual(vision.fusion_narrowing('n0'), 0)           # no fusion system online
        state.systems.instances['fx'] = NS(owner='n0', kind='fusion_sensor', online=True, hub='n0-h0')
        state.ai_deployment.previous_strength = lambda owner, hub, task, threshold: Q(10)
        self.assertEqual(vision.fusion_narrowing('n0'), Q(1, 20))
        state.ai_deployment.previous_strength = lambda owner, hub, task, threshold: Q(100)
        self.assertEqual(vision.fusion_narrowing('n0'), Q(1, 5))     # capped at 20%
        del state.systems.instances['fx']

    def test_c06_c07_import_parity_floors_net_imports_only(self):
        state = self.game().state
        parity = state.import_parity
        base = state.price('chip', 'n0-h0')
        state.rates['n0'] = Q(1, 2)                  # n0's currency halves against RU
        parity.record('chip', 'n0', 'n1', Q(5000))   # n0 imported chips in cycle 0
        self.assertEqual(state.price('chip', 'n0-h0'), base)          # current cycle not yet counted
        state.completed_cycles = 1
        floor = 10 * self.catalog.weights['chip'] * 2 * Q(1002, 1000)
        self.assertEqual(state.price('chip', 'n0-h0'), max(base, floor))
        self.assertEqual(state.price('chip', 'n1-h0'), state.price('chip', 'n1-h0'))
        self.assertFalse(parity.net_importer('n1', 'chip'))           # the exporter gets no floor
        state.completed_cycles = 7
        self.assertFalse(parity.net_importer('n0', 'chip'))           # outside the 5-cycle window

    def test_a07_precision_farming_cuts_inputs_and_lifts_bad_weather(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine import industry_effects
        from organizer.rift_era.engine.clock import ClockConfig
        state = self.game().state
        from organizer.rift_era.engine.production import Recipe
        farm = next(l for l in state.initial.lines.values() if l.owner == 'n0' and l.recipe.id in ('P028', 'P029'))
        farm.recipe = Recipe.from_catalog(self.catalog, 'P029')   # fixture: a fertilised farm line
        ctx = NS(tick=0, seed=1000, cycle=0, clock=ClockConfig())
        industry_effects.refresh(state, ctx)
        base = dict(farm.recipe.inputs)
        state.research['n0'].known.add('A07')
        state.line_options.setdefault(farm.id, {})['addons'] = ('sensor',)
        industry_effects.refresh(state, ctx)
        self.assertEqual(farm.recipe.inputs, base)                     # no C08 telemetry yet
        state.systems.instances['fx'] = NS(owner='n0', kind='fusion_sensor', online=True, hub='n0-h0')
        industry_effects.refresh(state, ctx)
        self.assertEqual(farm.recipe.inputs['water'], round(base['water'] * Q(3, 4)))
        self.assertEqual(farm.recipe.inputs['fertilizer'], round(base['fertilizer'] * Q(3, 4)))
        self.assertEqual(industry_effects.gene_weather(state, farm, Q(1, 2))[0], Q(65, 100))
        self.assertEqual(industry_effects.gene_weather(state, farm, Q(95, 100))[0], 1)
        del state.systems.instances['fx']

    def test_g07_combined_arms_needs_two_domains_in_network(self):
        state = self.game().state
        land, other = [f for f in state.formations.rows.values() if f.owner == 'n0'][:2]
        other.domain = 'Air'  # fixture: a second domain at the same place
        state.research['n0'].known.add('G07')
        land.slot_a = other.slot_a = 'radio'
        other.region = land.region
        arms = state.battles.combined_arms({})
        self.assertTrue(set(land.unit_ids) <= set(arms))
        uid = land.unit_ids[0]
        self.assertEqual(arms[uid].tech_multiplier, state.initial.units[uid].tech_multiplier * Q(6, 5))
        other.slot_a = None
        self.assertEqual(state.battles.combined_arms({}), {})

    def test_g12_joint_doctrine_adds_ten_percent_and_freezes_on_command_hub_loss(self):
        state = self.game().state
        land, other = [f for f in state.formations.rows.values() if f.owner == 'n0'][:2]
        other.domain = 'Air'
        state.research['n0'].known |= {'G07', 'G12'}
        land.slot_a = other.slot_a = 'radio'
        other.region = land.region
        uid = land.unit_ids[0]
        base = state.initial.units[uid].tech_multiplier
        self.assertEqual(state.battles.combined_arms({}, 5)[uid].tech_multiplier, base * Q(6, 5) * Q(11, 10))
        self.assertEqual(state.battles.combined_arms({})[uid].tech_multiplier, base * Q(6, 5))  # no tick: G07 only
        state.cyber_frozen_until['n0-h0'] = 6                       # command hub cut off at tick 6
        self.assertEqual(state.battles.combined_arms({}, 6)[uid].tech_multiplier, base * Q(6, 5))
        self.assertEqual(state.battles.combined_arms({}, 16)[uid].tech_multiplier, base * Q(6, 5))  # frozen 10 ticks
        self.assertEqual(state.battles.combined_arms({}, 17)[uid].tech_multiplier, base * Q(6, 5) * Q(11, 10))
        region = state.authority.regions[state.initial.map.hubs['n0-h0'].region]
        region.controller, saved = 'n1', region.controller          # capital occupied
        self.assertFalse(state.battles.joint_doctrine_online('n0', 30))
        region.controller = saved
        self.assertFalse(state.battles.joint_doctrine_online('n0', 40))
        self.assertTrue(state.battles.joint_doctrine_online('n0', 41))

    def test_v07_idle_biomass_site_recovers_half_the_degraded_rate_after_120_cycles(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.clock import ClockConfig
        from organizer.rift_era.engine.economy import Economy as E
        from organizer.rift_era.engine.ledger import Account
        state = self.game().state
        mid = next(m for m, d in state.deposits.items() if d.good == 'biomass'
                   and (state.resource_sites.get(m) or state.initial.mines.get(m)).owner == 'n0')
        clock = ClockConfig()
        E._track_v07(state, NS(tick=10, clock=clock))
        self.assertEqual(state.v07_last_extracted, {})               # no V07: nothing tracked
        state.research['n0'].known.add('V07')
        E._track_v07(state, NS(tick=10, clock=clock))
        self.assertEqual(state.v07_last_extracted[mid], 10)          # clock starts with V07
        self.assertFalse(E._v07_restored(state, mid, NS(tick=1209, clock=clock)))
        self.assertTrue(E._v07_restored(state, mid, NS(tick=1210, clock=clock)))
        account = Account(*state.resource_sources[mid], 'biomass')
        state.initial.inventory.transform({account: 1000}, {}, reason='test_draw')
        E._track_v07(state, NS(tick=500, clock=clock))               # a draw restarts the clock
        self.assertEqual(state.v07_last_extracted[mid], 500)
        state.research['n0'].known.discard('V07')

    def test_active_scale_meters_for_g07_g12_v07_l12(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.active_scale import ActiveScale
        from organizer.rift_era.engine.clock import ClockConfig
        state = self.game().state
        scale, ctx = ActiveScale(state), NS(tick=20, cycle=2, clock=ClockConfig())
        meter = lambda tech, name: scale._meter('n0', tech, {'kind': 'meter', 'meter': name}, ctx)
        self.assertEqual(meter('G07', 'ready_combined_arms_formations'), 0)
        land, other = [f for f in state.formations.rows.values() if f.owner == 'n0'][:2]
        other.domain, other.region = 'Air', land.region
        land.slot_a = other.slot_a = 'radio'
        state.research['n0'].known |= {'G07', 'G12'}
        self.assertEqual(meter('G07', 'ready_combined_arms_formations'), 2)
        self.assertEqual(meter('G12', 'ready_joint_command_formations'), 2)
        state.cyber_frozen_until['n0-h0'] = 20
        self.assertEqual(meter('G12', 'ready_joint_command_formations'), 0)
        state.research['n0'].known -= {'G07', 'G12'}
        self.assertEqual(meter('L12', 'completed_megaprojects'), 0)
        aid = next(a for a, x in state.initial.assets.items() if x.owner == 'n0' and not x.under_construction)
        state.megaprojects['rail:x'] = {'kind': 'L12_bridge', 'owner': 'n0', 'asset': aid}
        self.assertEqual(meter('L12', 'completed_megaprojects'), 1)
        mid = next(m for m, d in state.deposits.items() if d.good == 'biomass'
                   and (state.resource_sites.get(m) or state.initial.mines.get(m)).owner == 'n0')
        state.v07_last_extracted[mid] = 20 - 1200
        self.assertEqual(meter('V07', 'restored_closed_mines'), 1)
        self.assertEqual(meter('V08', 'consented_climate_regions'), 0)

    def test_i12_molecular_lines_cut_inputs_only_in_s5_with_ai_core(self):
        from dataclasses import replace
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine import industry_effects
        from organizer.rift_era.engine.clock import ClockConfig
        from organizer.rift_era.engine.production import Recipe
        state = self.game().state
        fid, facility = next((k, f) for k, f in state.initial.facilities.items() if f.owner == 'n0' and f.line_ids)
        line = state.initial.lines[facility.line_ids[0]]
        line.recipe = Recipe.from_catalog(self.catalog, 'P013')     # fixture
        ctx = NS(tick=0, seed=1000, cycle=0, clock=ClockConfig())
        state.research['n0'].known.add('I12')
        state.line_options.setdefault(line.id, {})['addons'] = ('ai_core',)
        state.initial.facilities[fid] = replace(facility, profile='S4')
        industry_effects.refresh(state, ctx)
        base = dict(line.recipe.inputs)
        state.initial.facilities[fid] = replace(facility, profile='S5')
        industry_effects.refresh(state, ctx)
        self.assertEqual(line.recipe.inputs, {g: round(q * Q(7, 10)) for g, q in base.items()})
        plain = Recipe.from_catalog(self.catalog, 'P013').outputs
        self.assertEqual(line.recipe.outputs, dict(plain))              # no 6% defect loss
        calls = []
        production = NS(discard_and_switch=lambda l, r: calls.append((l.id, r.id)))
        self.assertFalse(industry_effects.scrap_molecular_on_outage(state, production, line))  # nothing in progress
        line.credit_nmU['iron_ore' if 'iron_ore' in line.recipe.inputs else next(iter(line.recipe.inputs))] = 500
        self.assertTrue(industry_effects.scrap_molecular_on_outage(state, production, line))
        self.assertEqual(calls, [(line.id, 'P013')])
        state.initial.facilities[fid] = replace(facility, profile='S4')
        self.assertFalse(industry_effects.scrap_molecular_on_outage(state, production, line))  # not S5
        line.credit_nmU.clear()
        state.initial.facilities[fid] = facility


if __name__ == '__main__':
    unittest.main()

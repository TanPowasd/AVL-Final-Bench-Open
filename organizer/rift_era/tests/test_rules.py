import itertools
import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.diplomacy import Diplomacy, AcceptedPeace, Relation
from organizer.rift_era.engine.combat import Unit, TargetContact, resolve, apply_result, blockade_fraction
from organizer.rift_era.engine.market import Order, clear_auction, finite_pool_fills, debt_limit
from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.ai import DataBook, DataCenter, AcceleratorCohort, train_model, validate_budget, unlock_milestones
from organizer.rift_era.engine.environment import RiverNode, rivers, finite_extraction
from organizer.rift_era.engine.population import Population, Passenger, Demography, grow, housing_factor, support_delta, update_support
from organizer.rift_era.engine.provenance import Leg, Lot, Use, ReceiptBook, terminal_receipt, erase_loops
from organizer.rift_era.engine.scoring import (action_loss, eta_sink, industrial_pillar, population_pillar,
    adjusted_end, relative_ratio, score_interval, Trajectory)
from organizer.rift_era.engine.numeric import units
from organizer.rift_era.engine.errors import RuleViolation, InvariantViolation
from organizer.rift_era.engine.research import LabOutput, combine_labs, Project


class RulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()

    def test_simultaneous_declarations_and_truce_do_not_add_allies(self):
        diplomacy = Diplomacy(frozenset({'A', 'B', 'C'}))
        diplomacy.phase1(0, declarations=(('A', 'C'), ('C', 'A')))
        self.assertEqual(diplomacy.pairs[('A', 'C')].aggressors, frozenset({'A', 'C'}))
        with self.assertRaisesRegex(RuleViolation, 'ERR_NOT_AT_WAR'):
            diplomacy.validate_kinetic('C', 'B')
        diplomacy.phase1(1, treaties=(AcceptedPeace('A', 'C', 'armistice', 10),))
        self.assertEqual(diplomacy.relation('A', 'C'), Relation.TRUCE)
        diplomacy.phase1(100)
        self.assertEqual(diplomacy.relation('A', 'C'), Relation.TRUCE)
        diplomacy.phase1(101)
        self.assertEqual(diplomacy.relation('A', 'C'), Relation.PEACE)

    def test_damage_caps_casualties_and_is_simultaneous(self):
        d = Diplomacy(frozenset({'A', 'B'}))
        d.phase1(0, declarations=(('A', 'B'),))
        a = Unit('a', 'A', 'infantry', 'r', 100000, 1000)
        b = Unit('b', 'B', 'infantry', 'r', 100000, 1000)
        contacts = {'a': (TargetContact('b', Q(1), 0),), 'b': (TargetContact('a', Q(1), 0),)}
        supply = {'a': {'ammunition': 500}, 'b': {'ammunition': 500}}
        result = resolve(self.catalog, d, {'a': a, 'b': b}, contacts, supply, {'r': 'plain'})
        reverse = resolve(self.catalog, d, {'b': b, 'a': a}, contacts, supply, {'r': 'plain'})
        self.assertEqual(result, reverse)
        self.assertEqual(result.damage_mU['a'], result.damage_mU['b'])
        self.assertGreater(result.action_losses['A'], 0)
        self.assertGreater(result.action_losses['B'], 0)
        original = a.hp_mU
        apply_result({'a': a, 'b': b}, result)
        self.assertEqual(a.crew_mU, 1000 - 1000 * result.damage_mU['a'] // original)

    def test_range_visibility_and_peace_prevent_fire(self):
        d = Diplomacy(frozenset({'A', 'B'}))
        a = Unit('a', 'A', 'infantry', 'r', 100000, 1000)
        b = Unit('b', 'B', 'infantry', 's', 100000, 1000)
        for contact in (TargetContact('b', Q(1), 0), TargetContact('b', Q(1, 10), 0), TargetContact('b', Q(1), 2)):
            result = resolve(self.catalog, d, {'a': a, 'b': b}, {'a': (contact,)}, {'a': {'ammunition': 500}}, {'r': 'plain', 's': 'plain'})
            self.assertEqual(result.shots, ())
        d.phase1(0, declarations=(('A', 'B'),))
        for contact in (TargetContact('b', Q(1, 10), 0), TargetContact('b', Q(1), 2)):
            result = resolve(self.catalog, d, {'a': a, 'b': b}, {'a': (contact,)}, {'a': {'ammunition': 500}}, {'r': 'plain', 's': 'plain'})
            self.assertEqual(result.shots, ())

    def test_no_ammunition_does_not_create_remote_or_aa_damage(self):
        d = Diplomacy(frozenset({'A', 'B'}))
        d.phase1(0, declarations=(('A', 'B'),))
        a = Unit('a', 'A', 'infantry', 'r', 100000, 1000)
        b = Unit('b', 'B', 'aircraft', 'r', 110000, 1000)
        result = resolve(self.catalog, d, {'a': a, 'b': b}, {'a': (TargetContact('b', Q(1), 0),)}, {}, {'r': 'plain'})
        self.assertEqual(result.damage_mU['b'], 0)
        self.assertEqual(result.combat_regions, frozenset())

    def test_land_artillery_can_target_surface_domain_on_ocean(self):
        d = Diplomacy(frozenset({'A', 'B'}))
        d.phase1(0, declarations=(('A', 'B'),))
        a = Unit('a', 'A', 'artillery', 'coast', 90000, 2000)
        b = Unit('b', 'B', 'escort', 'sea', 240000, 3000)
        result = resolve(self.catalog, d, {'a': a, 'b': b}, {'a': (TargetContact('b', Q(1), 1),)},
                         {'a': {'ammunition': 2000, 'fuel': 600}}, {'coast': 'port', 'sea': 'ocean'})
        self.assertGreater(result.damage_mU['b'], 0)

    def test_blockade_does_not_sum_fractions(self):
        self.assertEqual(blockade_fraction(Q(0), Q(0)), 0)
        self.assertEqual(blockade_fraction(Q(50), Q(0)), 1)
        self.assertEqual(blockade_fraction(Q(25), Q(25)), Q(1, 4))

    def test_partial_repair_capitalizes_once(self):
        material = {g: units(q) * 1000 for g, q in self.catalog.profiles['S1']['build'].items()}
        asset = Asset('s1', 'A', 'h', material)
        removed = asset.damage(500)
        value = sum(q / 1000000 * self.catalog.weights[g] for g, q in removed.items())
        self.assertEqual(value, Q('54.2836'))
        repair = asset.plan_repair(1000, frozenset())
        asset.repair = repair
        self.assertEqual(repair.duration_ticks, 100)
        for _ in range(100):
            repair.advance_paid(asset)
        self.assertEqual(asset.health_mU, 1000)
        self.assertEqual(asset.book_nmU, material)
        self.assertIsNone(asset.repair)

    def test_indivisible_repair_rejects_overcapitalization(self):
        asset = Asset('x', 'A', 'h', {'vehicle': 1000000})
        asset.damage(500)
        with self.assertRaisesRegex(RuleViolation, 'ERR_REPAIR_QUANTUM'):
            asset.plan_repair(1000, frozenset({'vehicle'}))

    def test_auction_price_crossing_and_order_invariance(self):
        orders = (Order('a', 'A', 'buy', 9, 20), Order('b', 'B', 'buy', 9, 20), Order('c', 'C', 'sell', 5, 10))
        expected = clear_auction(1, 4, 'food', orders, 15)
        self.assertEqual(expected.quantity, 5)
        self.assertEqual(expected.price, 15)
        for perm in itertools.permutations(orders):
            self.assertEqual(clear_auction(1, 4, 'food', perm, 15), expected)
        no_cross = clear_auction(1, 4, 'food', (Order('a', 'A', 'buy', 9, 5), Order('b', 'B', 'sell', 9, 10)), 8)
        self.assertEqual((no_cross.price, no_cross.quantity), (8, 0))

    def test_world_pool_is_single_finite_budget(self):
        requests = {f'n{i}/g{j}': 10000 for i in range(8) for j in range(4)}
        fill = finite_pool_fills(1, 4, 1000, requests)
        self.assertEqual(sum(fill.values()), 1000)
        self.assertLessEqual(max(fill.values()) - min(fill.values()), 1)
        self.assertEqual(debt_limit((1,) * 20), 1000000)
        self.assertEqual(debt_limit((100000,) * 20), 4000000)

    def test_transit_formula_and_fixed_credit(self):
        lot = Lot('x', 'A', 'h0', 'food', 10000, (Leg('h0', 'h1', 'A', 6), Leg('h1', 'h2', 'B', 4)))
        book = ReceiptBook()
        book.add_lot(lot)
        receipt = book.consume(1, 'x', 10000, event_id='use', cycle=0, consumer='C', final_hub='h2', use=Use.CIVILIAN,
                              weight=self.catalog.weights['food'], origin_credit=Q(1), initial_shortest_length=8)
        self.assertEqual(receipt.transit, {'A': Q('183.12'), 'B': Q('122.08')})
        self.assertEqual(book.mean20(0, 'A', 'transit'), Q('9.156'))
        self.assertEqual(book.mean20(20, 'A', 'transit'), 0)
        self.assertEqual(receipt.credit_weighted_export, Q('38.15'))
        with self.assertRaises(InvariantViolation):
            book.consume(1, 'x', 10000, event_id='use', cycle=0, consumer='C', final_hub='h2', use=Use.CIVILIAN,
                         weight=self.catalog.weights['food'], origin_credit=Q(1), initial_shortest_length=8)

    def test_cross_carrier_loops_erase_globally(self):
        path = (Leg('a', 'b', 'A', 5), Leg('b', 'c', 'B', 5), Leg('c', 'a', 'C', 5))
        self.assertEqual(erase_loops(path), ())
        lot = Lot('x', 'A', 'a', 'food', 1000, path)
        receipt = terminal_receipt('x', 0, lot, 1000, consumer='A', final_hub='a', use=Use.CIVILIAN,
                                   weight=Q(1), origin_credit=Q(1), initial_shortest_length=0)
        self.assertEqual(receipt.transit, {})
        self.assertEqual(receipt.credit_weighted_export, 0)

    def test_action_repair_and_build_have_no_terminal_receipt(self):
        lot = Lot('x', 'A', 'a', 'food', 1000)
        for use in (Use.ACTION_LOSS, Use.REPAIR, Use.CONSTRUCTION, Use.DESTRUCTION):
            self.assertIsNone(terminal_receipt('x', 0, lot, 1000, consumer='A', final_hub='a', use=use,
                                               weight=Q(1), origin_credit=Q(1), initial_shortest_length=0))

    def test_action_loss_and_sink_debt_prevent_free_consumption(self):
        self.assertEqual(action_loss({}, self.catalog.weights), Q(1, 1000))
        self.assertEqual(action_loss({'fuel': 144000}, self.catalog.weights), 630)
        self.assertEqual(eta_sink(Q(10000), Q(0), Q(20000)), eta_sink(Q(9000), Q(1000), Q(20000)))
        self.assertEqual(industrial_pillar(Q(264), Q(0), Q(630), Q(0), Q(0)) / 4, Q('-32.025'))

    def test_score_vectors_and_very_small_penalty_remains_strict(self):
        for vector in self.catalog.tables['scoring']['relative_scoring']['test_vectors']:
            ratio = relative_ratio(Q(vector['V_p']), Q(vector['V_s']))
            interval = score_interval(ratio)
            expected = float(vector['s_k'])
            self.assertLess(abs(float(interval.lower) - expected), 1e-9)
            self.assertLessEqual(interval.lower, interval.upper)
        baseline = Q(10**80)
        with_loss = baseline - Q(1, 1000)
        self.assertLess(relative_ratio(with_loss, baseline), relative_ratio(baseline, baseline))
        self.assertLess(score_interval(relative_ratio(with_loss, baseline), 110).upper, 50)

    def test_sustainability_monotone_even_for_negative_score(self):
        for value in (Q(-100), Q(0), Q(100)):
            self.assertLess(adjusted_end(value, Q(35, 100)), adjusted_end(value, Q(1)))

    def test_trajectory_is_readonly_and_no_duplicate_cycle(self):
        trajectory = Trajectory()
        for cycle in range(2880):
            trajectory.commit(cycle, Q(20))
        self.assertEqual(trajectory.value(Q(1)), 20)
        before = (tuple(trajectory.era_sums), tuple(trajectory.last40))
        for _ in range(100):
            trajectory.value(Q(1))
        self.assertEqual(before, (tuple(trajectory.era_sums), tuple(trajectory.last40)))
        with self.assertRaises(InvariantViolation):
            trajectory.commit(2879, Q(20))

    def test_synthetic_food_welfare_cap_and_zero_population(self):
        args = dict(support_mU=720, homeland=True, hold_cycles=0, food=Q(1), synthetic_share=Q(0),
                    consumer_goods=Q(1), medicine=Q(1), advanced_medicine=Q(0), water=Q(1), electricity=Q(1),
                    combat=Q(0), blockade=Q(0), pollution=Q(0), remediation=Q(0))
        ordinary = population_pillar(194000, **args)
        self.assertEqual(ordinary, Q('108.446'))
        self.assertLess(population_pillar(194000, **dict(args, synthetic_share=Q(1))), ordinary)
        self.assertEqual(population_pillar(0, **args), 0)

    def test_split_labs_global_count_and_no_project_overflow(self):
        distinct = tuple(LabOutput(str(i), str(i), 1000000, connected=True) for i in range(8))
        same = tuple(LabOutput(str(i), 'one', 1000000, connected=True) for i in range(8))
        self.assertEqual(combine_labs(distinct, organized=True), 4756828)
        self.assertEqual(combine_labs(same, organized=True), 6773962)
        p = Project('I01', 80000)
        p.advance_theory(1000000)
        self.assertEqual(p.theory_mU, 80000)
        self.assertTrue(p.theory_complete)

    def test_ai_split_training_fixed_data_and_no_duplicate_license(self):
        data = Q(100)
        once = train_model('once', 'A', 10, Q(1000), data)
        first = train_model('first', 'A', 5, Q(500), data)
        twice = train_model('twice', 'A', 10, Q(500), data, first)
        self.assertLess(abs(once.effective_compute - twice.effective_compute), Q(1, 10**35))
        book = DataBook()
        book.license('A', Q(1000))
        book.license('A', Q(1000))
        self.assertEqual(book.licensed_from['A'], 500)
        book.license('A', Q(2000))
        self.assertEqual(book.licensed_from['A'], 1000)

    def test_accelerator_refill_does_not_rejuvenate_old_cohort(self):
        center = DataCenter('d', 'h', 1, [AcceleratorCohort(0, 50)])
        self.assertEqual(center.capacity_mU(200), 25000)
        center.refill(200, 25)
        self.assertEqual(center.capacity_mU(400), 12500)
        self.assertEqual(center.capacity_mU(600), 0)

    def test_ai_budget_cannot_borrow_another_hubs_compute(self):
        centers = (DataCenter('a', 'a', 1, [AcceleratorCohort(0, 50)]), DataCenter('b', 'b', 3, [AcceleratorCohort(0, 50)]))
        with self.assertRaisesRegex(RuleViolation, 'ERR_CAPACITY_EXCEEDED'):
            validate_budget('a', {'train': 100000}, centers, 0, frozenset({'AI02'}))
        known, gain = unlock_milestones(self.catalog, frozenset({'AI02'}), Q(100))
        self.assertIn('AI07', known)
        self.assertNotIn('AI08', known)
        self.assertEqual(gain, Q(15, 100))

    def test_river_branching_and_dry_deposits_cannot_create_resources(self):
        nodes = (RiverNode('a', 1000, {'b': Q(1, 2), 'c': Q(1, 2)}, {'ma': 1000}),
                 RiverNode('b', 0, {}, {'mb': 1000}), RiverNode('c', 0, {}, {'mc': 1000}))
        result = rivers(1, 0, nodes)
        self.assertEqual(sum(result.withdrawn.values()) + result.ocean_mU, 1000)
        self.assertEqual(result.withdrawn['ma'], 800)
        self.assertEqual(finite_extraction(0, 1000, 1000, Q(1)), 0)
        self.assertLessEqual(finite_extraction(1, 1000, 1000, Q(1)), 1)
        self.assertEqual(rivers(1, 0, tuple(reversed(nodes))), result)

    def test_migration_no_teleport_and_no_housing_births(self):
        source = Population('a', dict(zip(('general_worker', 'skilled_worker', 'engineer'), (144000, 40000, 10000))))
        dest = Population('b', {})
        source.reserve_people({'general_worker': 5000})
        passenger = Passenger('p', 'a', 'b', {'general_worker': 5000}, 0, 2)
        self.assertFalse(passenger.arrive(1, dest))
        self.assertEqual(dest.total_mU, 0)
        self.assertTrue(passenger.arrive(2, dest))
        self.assertFalse(passenger.arrive(2, dest))
        self.assertEqual(source.skills_mU['general_worker'], 139000)
        self.assertEqual(housing_factor(dest.total_mU, 0), 0)
        births, deaths = grow(1, 0, dest, Demography(Q(1), 0, Q(0), Q(0), Q(0), Q(0), Q(0)))
        self.assertEqual(births, 0)

    def test_support_recovery_and_orange_before_six_red_cycles(self):
        delta = support_delta(food=Q(1), consumer_goods=Q(1), medicine=Q(1), employment=Q(1),
            tax=Q(5, 100), fatigue=Q(0), pollution=Q(0), autonomy=Q(1, 2), social_security=None, occupied=False)
        self.assertEqual(delta, 24)
        p = Population('h', {'general_worker': 1000}, support_mU=100)
        for _ in range(5):
            update_support(p, 0)
            self.assertEqual(p.warning, 'GOV_ALERT_STRIKE')
        update_support(p, 0)
        self.assertEqual(p.warning, 'GOV_CRISIS_PARALYSIS')
        update_support(p, 50)
        self.assertEqual(p.warning, 'GOV_ALERT_STRIKE')


if __name__ == '__main__':
    unittest.main()

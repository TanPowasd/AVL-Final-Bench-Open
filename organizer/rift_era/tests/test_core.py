import itertools
import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.determinism import digest, uniform
from organizer.rift_era.engine.errors import InvariantViolation, RuleViolation
from organizer.rift_era.engine.ledger import Account, Inventory, Transfer, CashAccount, CashLedger, CashTransfer
from organizer.rift_era.engine.numeric import proportional, split_payment
from organizer.rift_era.engine.production import Line, Recipe, Production, commit_buffers
from organizer.rift_era.engine.power import PLANTS, Plant, PlantMode, Supply, Load, PowerEdge, dispatch, storage_auto


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()

    def test_catalog_cross_tables_and_immutability(self):
        self.assertEqual(len(self.catalog.technologies), 123)
        self.assertEqual(len(self.catalog.recipes), 119)
        with self.assertRaises(TypeError):
            self.catalog.recipes['P001']['power'] = 0

    def test_clock_separates_decisions_from_cycles(self):
        clock = ClockConfig(decision_ticks=1)
        self.assertFalse(clock.official)
        self.assertEqual(sum(clock.is_decision(t) for t in range(10)), 10)
        self.assertEqual(sum(clock.is_cycle_end(t) for t in range(10)), 1)

    def test_hash_keys_have_types_and_no_concatenation_collision(self):
        self.assertNotEqual(digest('a:b', 'c'), digest('a', 'b:c'))
        self.assertNotEqual(digest(1), digest('1'))
        first = uniform(7, 'weather', 10, 'h')
        uniform(7, 'combat', 10, 'h')
        self.assertEqual(first, uniform(7, 'weather', 10, 'h'))

    def test_pool_conservation_and_order_invariance(self):
        for capacity in range(15):
            expected = proportional(capacity, {'a': 7, 'b': 3, 'c': 2}, key=(1, 'pool'))
            for order in itertools.permutations(('a', 'b', 'c')):
                actual = proportional(capacity, {k: {'a': 7, 'b': 3, 'c': 2}[k] for k in order}, key=(1, 'pool'))
                self.assertEqual(actual, expected)
            self.assertEqual(sum(expected.values()), min(capacity, 12))

    def test_money_uses_largest_remainder(self):
        self.assertEqual(split_payment(7, {'a': Q(1), 'b': Q(2)}, key=(2,)), {'a': 2, 'b': 5})

    def test_inventory_batch_rejection_is_atomic(self):
        inv = Inventory()
        a, b, c = (Account('A', x, 'available', 'coal') for x in ('a', 'b', 'c'))
        inv.initialize(a, 10)
        inv.seal()
        before = inv.snapshot()
        with self.assertRaises(RuleViolation):
            inv.transfer_many([Transfer(a, b, 7), Transfer(a, c, 7)])
        self.assertEqual(inv.snapshot(), before)
        inv.transfer(a, b, 7)
        inv.audit()
        with self.assertRaises(InvariantViolation):
            inv.initialize(a, 1)

    def test_locked_inventory_not_available_to_line(self):
        inv = Inventory()
        a = Account('A', 'h', 'available', 'coal')
        inv.initialize(a, 100)
        inv.transfer(a, Account('A', 'h', 'locked_order', 'coal'), 100)
        line = Line('l', 'A', 'h', Recipe('r', {'coal': 1000}, {'steel': 1000}, frozenset(), frozenset()))
        result = Production(1, inv).advance(0, [line], {'l': 100}, free_capacity_mU={'h': 10000})
        self.assertEqual(result[0].batch_mU, 0)
        inv.audit()

    def test_nanounit_prepaid_credit_does_not_create_material(self):
        inv = Inventory()
        stock = Account('A', 'h', 'available', 'precision_pack')
        inv.initialize(stock, 2)
        recipe = Recipe('P076_test', {'precision_pack': 100}, {'research_points': 1000}, frozenset(), frozenset({'research_points'}))
        line = Line('l', 'A', 'h', recipe)
        production = Production(1, inv)
        for tick in range(10):
            production.advance(tick, [line], {'l': 1}, free_capacity_mU={'h': 0})
        self.assertEqual(inv.balance(stock), 1)
        self.assertEqual(line.credit_nmU['precision_pack'], 0)
        self.assertEqual(line.used_nmU['precision_pack'], 1000)
        production.advance(10, [line], {'l': 1}, free_capacity_mU={'h': 0})
        self.assertEqual(inv.balance(stock), 0)
        self.assertEqual(line.credit_nmU['precision_pack'], 900)

    def test_whole_machine_waits_and_current_tick_cannot_cascade(self):
        inv = Inventory()
        producer = Line('a', 'A', 'h', Recipe('r', {}, {'vehicle': 1000}, frozenset({'vehicle'}), frozenset()))
        consumer = Line('b', 'A', 'h', Recipe('s', {'vehicle': 1000}, {'steel': 1000}, frozenset(), frozenset()))
        production = Production(1, inv)
        result = production.advance(0, [producer, consumer], {'a': 1250, 'b': 1000}, free_capacity_mU={'h': 10000})
        self.assertEqual(result[0].released_mU['vehicle'], 1000)
        self.assertEqual(producer.output_nmU['vehicle'], 250000)
        self.assertEqual(result[1].batch_mU, 0)
        commit_buffers(inv)
        result = production.advance(1, [consumer], {'b': 1000}, free_capacity_mU={'h': 10000})
        self.assertEqual(result[0].batch_mU, 1000)

    def test_many_input_constraints_return_unused_quota(self):
        def run(reverse):
            inv = Inventory()
            inv.initialize(Account('A', 'h', 'available', 'coal'), 100)
            inv.initialize(Account('A', 'h', 'available', 'water'), 10)
            a = Line('a', 'A', 'h', Recipe('r', {'coal': 1000, 'water': 1000}, {'steel': 1000}, frozenset(), frozenset()))
            b = Line('b', 'A', 'h', Recipe('r', {'coal': 1000}, {'steel': 1000}, frozenset(), frozenset()))
            results = Production(1, inv).advance(0, [b, a] if reverse else [a, b], {'a': 100, 'b': 100}, free_capacity_mU={'h': 10000})
            self.assertEqual({r.line_id: r.batch_mU for r in results}, {'a': 10, 'b': 50})
            self.assertEqual(inv.balance(Account('A', 'h', 'available', 'coal')), 40)
            inv.audit()
            return inv.snapshot()
        self.assertEqual(run(False), run(True))

    def test_cash_issuance_transfers_and_fx_basis(self):
        cash = CashLedger()
        a, ar = CashAccount('A', 'treasury', 'A'), CashAccount('A', 'reserves', 'RU')
        p, pr = CashAccount('world', 'pool', 'A'), CashAccount('world', 'pool', 'RU')
        cash.initialize(a, 150000000)
        cash.initialize(p, 500000000)
        cash.initialize(pr, 1000000000)
        cash.seal()
        cash.exchange(a, p, pr, ar, 99800000, 99800000, executed_ru_mU=Q(99800000))
        cash.transfer(a, p, 200000)
        for live_rate in (Q(1), Q(1, 2), Q(1, 20)):
            # A screen FX quote is deliberately NOT an argument to owner_book().
            self.assertEqual(cash.owner_book('A') - cash.initial_owner_book['A'], -200000)
        before = cash.owner_book('A')
        cash.issue(a, 1000, rate=Q(1, 20))
        self.assertEqual(cash.owner_book('A') - cash.issue_book['A'], before)
        cash.audit()

    def test_tiny_fees_accumulate_and_failed_transfer_rolls_back(self):
        cash = CashLedger()
        a, b = CashAccount('A', 'a', 'A'), CashAccount('B', 'b', 'A')
        cash.initialize(a, 5)
        for _ in range(10):
            cash.fractional_fee(a, b, Q(1, 10), kind='fee')
        self.assertEqual(cash.balance(b).native_mU, 1)
        before = cash.snapshot()
        with self.assertRaises(RuleViolation):
            cash.transfer_many([CashTransfer(a, b, 3), CashTransfer(a, b, 3)])
        self.assertEqual(cash.snapshot(), before)
        cash.audit()

    def test_nuclear_deadlock_and_previous_pump(self):
        plant = Plant('n', PLANTS['P073'], PlantMode.ON, True, Q(7, 10), pump_previous=Q(1))
        self.assertEqual(plant.prepare(Q(2, 5), Q(1), Q(1)), (Q(2, 5), Q(2, 5)))
        plant.pump_previous = Q(4, 5)
        self.assertEqual(plant.prepare(Q(1), Q(1), Q(1)), (0, 0))
        self.assertEqual(plant.mode, PlantMode.SCRAM)

    def test_storage_direction_ignores_current_price(self):
        self.assertEqual(storage_auto((Q(10),) * 10, Q(1), Q(85, 100)), ('idle', Q(0)))

    def test_grid_islands_and_no_phantom_supply(self):
        supplies = (Supply('s', 'a', 'A', 10, Q(1)),)
        loads = (Load('a', 'a', 'A', 5, 1, Q(50), Q(100)), Load('b', 'b', 'B', 5, 1, Q(50), Q(100)))
        edges = (PowerEdge('e', 'a', 'b', 1, Q(1), 'A'),)
        result = dispatch(1, 0, supplies, loads, edges)
        self.assertEqual(result.received, {'a': 5, 'b': 1})
        self.assertEqual(result.source_remaining, {'s': 4})
        other = dispatch(1, 0, tuple(reversed(supplies)), tuple(reversed(loads)), edges)
        self.assertEqual(result, other)

    def test_grid_receipt_cash_and_energy_balance(self):
        result = dispatch(1, 0, (Supply('s', 'a', 'A', 100, Q(1)),),
                          (Load('b', 'b', 'B', 100, 1, Q(50), Q(100)),),
                          (PowerEdge('e', 'a', 'b', 100, Q(9, 10), 'A', Q(0)),))
        for receipt in result.receipts:
            self.assertEqual(receipt.cost_ru_mU, receipt.source_payment_ru_mU)
            self.assertGreaterEqual(receipt.sent_mU, receipt.received_mU)
        self.assertLessEqual(result.spent['b'], 100)
        self.assertEqual(sum(r.sent_mU for r in result.receipts) + result.source_remaining['s'], 100)
        self.assertEqual(result.received['b'], 90)


if __name__ == '__main__':
    unittest.main()

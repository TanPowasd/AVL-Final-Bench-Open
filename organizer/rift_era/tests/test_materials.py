import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.consumption import Consumption, InitialPaths
from organizer.rift_era.engine.determinism import canonical, fingerprint
from organizer.rift_era.engine.errors import InvariantViolation, RuleViolation
from organizer.rift_era.engine.ledger import Account, CashAccount, CashLedger, CashTransfer, DeliveryPayment, Inventory, Transfer
from organizer.rift_era.engine.logistics import Corridor, Transport, TransitMode, Vehicle
from organizer.rift_era.engine.materials import Origin, split_origins
from organizer.rift_era.engine.production import Line, Recipe, Production, commit_buffers
from organizer.rift_era.engine.provenance import Leg, Use


class MaterialFixture:
    def inventory(self, balances):
        inventory = Inventory()
        for account, quantity in balances.items():
            inventory.initialize(account, quantity)
        inventory.seal()
        inventory.enable_provenance(seed=11, credits={'A': Q(1), 'B': Q(1), 'O': Q(1)}).begin_tick(0)
        return inventory

    def settle(self, inventory, accounting, *, cycle=0, paths=()):
        accounting.advance(inventory.materials.tick, cycle, tuple(inventory.materials.events),
            weights={e.good: Q(2) for e in inventory.materials.events},
            shortest=InitialPaths(paths).distance)


class MaterialTests(MaterialFixture, unittest.TestCase):

    def test_local_sales_and_custody_preserve_origin_without_recognition(self):
        source = Account('A', 'h', 'available', 'food')
        target = Account('B', 'h', 'next_available', 'food')
        inventory = self.inventory({source: 1000})
        inventory.transfer(source, target, 1000)
        commit_buffers(inventory)
        inventory.transfer(Account('B', 'h', 'available', 'food'), Account('C', 'h', 'available', 'food'), 1000)
        self.assertEqual(inventory.materials.composition(Account('C', 'h', 'available', 'food')), {Origin('A', 'h'): 1000})
        self.assertEqual(inventory.materials.events, [])
        consumption = Consumption()
        self.settle(inventory, consumption)
        self.assertEqual(consumption.receipt_mean20('A', 'export'), 0)
        inventory.materials.begin_tick(1)
        inventory.materials.credits['A'] = Q(3, 5)
        inventory.transform({Account('C', 'h', 'available', 'food'): 1000}, {}, reason='actual_civilian_use', use=Use.CIVILIAN)
        inventory.materials.credits['A'] = Q(1, 10)
        self.settle(inventory, consumption)
        self.assertEqual(consumption.receipt_mean20('A', 'export'), Q(3, 50))
        self.assertEqual(consumption.consumption20('C', 'food'), 1000)
        self.assertFalse(inventory.materials.composition(Account('C', 'h', 'available', 'food')))
        before = fingerprint(consumption.snapshot())
        with self.assertRaises(InvariantViolation):
            self.settle(inventory, consumption)
        self.assertEqual(before, fingerprint(consumption.snapshot()))
        inventory.audit()

    def test_dvp_failure_cannot_move_goods_or_metadata_or_cash(self):
        source = Account('A', 'h', 'available', 'steel')
        target = Account('B', 'h', 'next_available', 'steel')
        inventory = self.inventory({source: 1000})
        cash = CashLedger()
        payer, payee = CashAccount('B', 'treasury', 'B'), CashAccount('A', 'treasury', 'B')
        cash.initialize(payer, 1)
        cash.seal()
        before = fingerprint((inventory.snapshot(), cash.snapshot()))
        with self.assertRaises(RuleViolation):
            DeliveryPayment.prepare(inventory, cash, [Transfer(source, target, 100)], [CashTransfer(payer, payee, 2)])
        self.assertEqual(before, fingerprint((inventory.snapshot(), cash.snapshot())))
        plan = DeliveryPayment.prepare(inventory, cash, [Transfer(source, target, 100)], [CashTransfer(payer, payee, 1)])
        inventory.materials.begin_tick(1)
        before = fingerprint((inventory.snapshot(), cash.snapshot()))
        with self.assertRaises(InvariantViolation):
            plan.commit()
        self.assertEqual(before, fingerprint((inventory.snapshot(), cash.snapshot())))

    def test_cross_hub_transfer_requires_an_actual_continuous_path(self):
        source = Account('A', 'a', 'available', 'steel')
        target = Account('B', 'b', 'available', 'steel')
        inventory = self.inventory({source: 1000})
        before = fingerprint(inventory.snapshot())
        for transfer in (Transfer(source, target, 100),
                         Transfer(source, target, 100, legs=(Leg('x', 'b', 'A', 4),))):
            with self.assertRaises(InvariantViolation):
                inventory.transfer_many([transfer])
            self.assertEqual(before, fingerprint(inventory.snapshot()))
        inventory.transfer_many([Transfer(source, target, 100, legs=(Leg('a', 'b', 'A', 4),))])
        self.assertEqual(next(iter(inventory.materials.composition(target))).location, 'b')
        inventory.audit()

    def test_transfer_order_does_not_change_origin_allocation(self):
        states = []
        for reverse in (False, True):
            a, b = Account('A', 'h', 'available', 'food'), Account('B', 'h', 'available', 'food')
            shared = Account('C', 'h', 'available', 'food')
            inventory = self.inventory({a: 877, b: 123})
            inventory.transfer_many([Transfer(a, shared, 877), Transfer(b, shared, 123)])
            transfers = [Transfer(shared, Account(p, 'h', 'available', 'food'), q)
                         for p, q in (('D', 337), ('E', 221), ('F', 101))]
            inventory.transfer_many(reversed(transfers) if reverse else transfers)
            inventory.audit()
            totals = {}
            for account in inventory.balances():
                for origin, quantity in inventory.materials.composition(account).items():
                    totals[origin.nation] = totals.get(origin.nation, 0) + quantity
            self.assertEqual(totals, {'A': 877, 'B': 123})
            states.append(fingerprint(inventory.snapshot()))
        self.assertEqual(*states)

    def test_circular_mixing_does_not_grow_unbounded_ratio_denominators(self):
        a, b = Account('A', 'h', 'available', 'food'), Account('B', 'h', 'available', 'food')
        inventory = self.inventory({a: 1000, b: 1000})
        for _ in range(2000):
            inventory.transfer(a, b, 1)
            inventory.transfer(b, a, 1)
        self.assertLessEqual(max(q.denominator for x in (a, b) for q in inventory.materials.composition(x).values()), 1000)
        self.assertLess(len(canonical(inventory.snapshot())), 2000)
        for origin in (Origin('A', 'h'), Origin('B', 'h')):
            self.assertEqual(sum(inventory.materials.composition(x).get(origin, 0) for x in (a, b)), 1000)
        inventory.audit()

    def test_sub_nanounit_fuel_dust_is_never_overdrawn_or_erased(self):
        origins = [Origin(p, 'h') for p in ('A', 'B', 'C')]
        weights = dict(zip(origins, (Q(1, 7000), Q(3, 11000), Q(1))))
        amount = Q(1, 6000)
        draw = split_origins(amount, weights, capped=True, key=(1, 'dust'))
        self.assertEqual(sum(draw.values()), amount)
        self.assertTrue(all(q <= weights[o] for o, q in draw.items()))
        self.assertTrue(all(q > 0 for q in draw.values()))

    def test_provenance_cannot_be_started_after_untracked_trading(self):
        inventory = Inventory()
        a, b = Account('A', 'h', 'available', 'food'), Account('B', 'h', 'available', 'food')
        inventory.initialize(a, 1000)
        inventory.seal()
        inventory.transfer(a, b, 1000)
        with self.assertRaises(InvariantViolation):
            inventory.enable_provenance()

    def test_action_consumption_cannot_raise_own_good_inventory_eta(self):
        account = Account('A', 'h', 'available', 'fuel')
        inventory = self.inventory({account: 10000})
        consumption = Consumption()
        before = consumption.eta(inventory.materials, 'A', 'fuel')
        inventory.transform({account: 3000}, {}, reason='actual_military_movement', use=Use.ACTION_LOSS)
        self.settle(inventory, consumption)
        self.assertEqual(consumption.eta(inventory.materials, 'A', 'fuel'), before)
        self.assertEqual(consumption.sink_debt[('A', 'fuel')], 3000)
        inventory.transfer(account, Account('B', 'h', 'available', 'fuel'), 7000)
        self.assertEqual(consumption.sink_debt[('A', 'fuel')], 3000)
        consumption.audit()

    def test_input_credit_and_wip_are_physical_but_only_actual_use_is_recognized(self):
        source = Account('A', 'h', 'available', 'ore')
        buyer = Account('B', 'h', 'available', 'ore')
        inventory = self.inventory({source: 1})
        inventory.transfer(source, buyer, 1)
        line = Line('line', 'B', 'h', Recipe('tiny', {'ore': 1}, {'steel': 1}, frozenset(), frozenset()))
        production = Production(11, inventory)
        consumption = Consumption()
        production.advance(0, [line], {'line': 1}, free_capacity_mU={'h': 100})
        self.assertEqual(inventory.balance(buyer), 0)
        self.assertEqual(inventory.materials.quantity(production.credit_account(line, 'ore')), Q(999, 1000))
        self.assertEqual(inventory.materials.quantity(production.wip_account(line, 'steel')), Q(1, 1000))
        self.settle(inventory, consumption)
        self.assertEqual(consumption.consumption20('B', 'ore'), Q(1, 1000))
        self.assertEqual(consumption.receipt_mean20('A', 'export'), Q(1, 10000000))
        inventory.materials.begin_tick(1)
        production.advance(1, [line], {'line': 999}, free_capacity_mU={'h': 100})
        self.settle(inventory, consumption)
        self.assertEqual(consumption.consumption20('B', 'ore'), 1)
        self.assertEqual(inventory.materials.quantity(production.credit_account(line, 'ore')), 0)
        self.assertEqual(inventory.materials.composition(Account('B', 'h', 'next_available', 'steel')), {Origin('B', 'h'): 1})
        inventory.audit()

    def test_source_override_cannot_feed_a_factory_from_a_remote_warehouse(self):
        source = Account('A', 'remote', 'available', 'ore')
        inventory = self.inventory({source: 1000})
        line = Line('line', 'A', 'local', Recipe('steel', {'ore': 1000}, {'steel': 1000}, frozenset(), frozenset()))
        before = fingerprint((inventory.snapshot(), line))
        with self.assertRaises(InvariantViolation):
            Production(11, inventory).advance(0, [line], {'line': 1000}, free_capacity_mU={'local': 1000},
                input_sources={'line': ('A', 'remote', 'available')})
        self.assertEqual(before, fingerprint((inventory.snapshot(), line)))

    def test_whole_machine_wip_switch_is_destroyed_once(self):
        source = Account('A', 'h', 'available', 'steel')
        inventory = self.inventory({source: 1000})
        recipe = Recipe('machine', {'steel': 1000}, {'machine': 1000}, frozenset({'machine'}), frozenset())
        line = Line('line', 'A', 'h', recipe)
        production = Production(11, inventory)
        production.advance(0, [line], {'line': 100}, free_capacity_mU={'h': 0})
        self.assertEqual(inventory.balance(Account('A', 'h', 'next_available', 'machine')), 0)
        self.assertEqual(inventory.materials.physical_by_owner('A')['machine'], 100)
        self.assertEqual(production.discard_and_switch(line, recipe), {'machine': 100000, 'steel': 0})
        consumption = Consumption()
        self.settle(inventory, consumption)
        self.assertEqual(consumption.sink_debt[('A', 'machine')], 100)
        self.assertEqual(production.discard_and_switch(line, recipe), {})
        inventory.audit()

    def test_recycling_keeps_origin_and_never_restarts_trade_reward(self):
        scrap, coal = Account('A', 'h', 'available', 'scrap_steel'), Account('A', 'h', 'available', 'coal')
        inventory = self.inventory({scrap: 4000, coal: 1000})
        for account in (scrap, coal):
            inventory.transfer(account, Account('B', 'h', 'available', account.asset), inventory.balance(account))
        recipe = Recipe.from_catalog(Catalog(), 'P066')
        line = Line('recycle', 'B', 'h', recipe)
        Production(11, inventory).advance(0, [line], {'recycle': 1000}, free_capacity_mU={'h': 10000})
        destination = Account('B', 'h', 'next_available', 'steel')
        self.assertEqual(inventory.materials.composition(destination), {Origin('A', 'h', recognized=True): 2000})
        consumption = Consumption()
        self.settle(inventory, consumption)
        self.assertEqual(consumption.receipt_mean20('A', 'export'), 0)
        self.assertEqual(consumption.consumption20('B', 'coal'), 1000)
        inventory.materials.begin_tick(1)
        final = Account('C', 'h', 'available', 'steel')
        inventory.transfer(destination, final, 2000)
        inventory.transform({final: 2000}, {}, reason='industrial_use_of_recycled_steel', use=Use.INDUSTRY)
        self.settle(inventory, consumption)
        self.assertEqual(consumption.receipt_mean20('A', 'export'), 0)
        inventory.audit()

    def test_synthetic_food_mixes_through_manufacture_trade_and_consumption(self):
        accounts = {Account('A', 'h', 'available', good): amount for good, amount in
                    (('chemicals', 1000), ('water', 2000), ('biomass', 1000), ('food', 5000))}
        inventory = self.inventory(accounts)
        line = Line('synthetic', 'A', 'h', Recipe.from_catalog(Catalog(), 'P090'))
        Production(11, inventory).advance(0, [line], {'synthetic': 1000}, free_capacity_mU={'h': 10000})
        commit_buffers(inventory)
        food = Account('B', 'h', 'available', 'food')
        inventory.transfer(Account('A', 'h', 'available', 'food'), food, 2000)
        self.assertEqual(sum(q for o, q in inventory.materials.composition(food).items() if o.synthetic_food), 1000)
        inventory.transform({food: 2000}, {}, reason='household', use=Use.CIVILIAN)
        consumption = Consumption()
        self.settle(inventory, consumption)
        self.assertEqual(consumption.synthetic_share('h', 0), Q(1, 2))
        inventory.audit()

    def test_military_destruction_repair_are_not_positive_consumption_and_debt_never_decays(self):
        account = Account('A', 'h', 'available', 'fuel')
        inventory = self.inventory({account: 1000})
        for purpose in (Use.ACTION_LOSS, Use.MILITARY_SUPPORT, Use.DESTRUCTION, Use.REPAIR):
            inventory.transform({account: 100}, {}, reason=purpose.value, use=purpose)
        inventory.transform({account: 100}, {}, reason='build', use=Use.CONSTRUCTION)
        inventory.transform({account: 100}, {}, reason='normal_maintenance', use=Use.MAINTENANCE)
        consumption = Consumption()
        self.settle(inventory, consumption)
        self.assertEqual(consumption.sink_debt[('A', 'fuel')], 400)
        self.assertEqual(consumption.consumption20('A', 'fuel'), 100)
        for cycle in range(1, 31):
            inventory.materials.begin_tick(cycle * 10)
            self.settle(inventory, consumption, cycle=cycle)
        self.assertEqual(consumption.sink_debt[('A', 'fuel')], 400)
        self.assertEqual(consumption.consumption20('A', 'fuel'), 0)
        self.assertLessEqual(len(consumption.snapshot()['eligible']), 20)
        before = fingerprint((inventory.snapshot(), consumption.snapshot()))
        for _ in range(100):
            consumption.consumption20('A', 'fuel')
            consumption.receipt_mean20('A', 'export')
            inventory.audit()
        self.assertEqual(before, fingerprint((inventory.snapshot(), consumption.snapshot())))


class MaterialTransportTests(MaterialFixture, unittest.TestCase):

    def drive(self, inventory, transport, shipment_id, start, *, max_ticks=30):
        for tick in range(start, start + max_ticks):
            inventory.materials.begin_tick(tick)
            transport.advance(tick, shipment_id, entry_granted=True, free_capacity_mU=10000)
            inventory.audit()
            transport.audit()
            if transport.shipments[shipment_id].mode == TransitMode.COMPLETED:
                return tick
        self.fail('vehicle did not complete its physical round trip')

    def test_actual_multi_carrier_route_erases_cross_carrier_loops(self):
        origin = Account('O', 'a', 'available', 'food')
        inventory = self.inventory({origin: 1000, Account('A', 'a', 'available', 'fuel'): 1000,
                                     Account('B', 'b', 'available', 'fuel'): 1000})
        inventory.transfer(origin, Account('A', 'a', 'available', 'food'), 1000)
        edges = {'ab': Corridor('ab', 'a', 'b', 2, 100000), 'bc': Corridor('bc', 'b', 'c', 3, 100000),
                 'bd': Corridor('bd', 'b', 'd', 4, 100000)}
        vehicles = {'a': Vehicle('a', 'A', 'a', 'truck', 10000, Q(10), Q(1)),
                    'b': Vehicle('b', 'B', 'b', 'truck', 10000, Q(10), Q(1))}
        transport = Transport(inventory, edges, vehicles)
        transport.launch('first', 'A', 'a', 'food', 1000, 'a', 'b', ('ab',), 0)
        last = self.drive(inventory, transport, 'first', 1)
        inventory.transfer(Account('A', 'b', 'available', 'food'), Account('B', 'b', 'available', 'food'), 1000)
        transport.launch('second', 'B', 'b', 'food', 1000, 'b', 'd', ('bc', 'bc', 'bd'), last)
        last = self.drive(inventory, transport, 'second', last + 1)
        final = Account('B', 'd', 'available', 'food')
        attributes, = inventory.materials.composition(final)
        self.assertEqual(attributes.path, (Leg('a', 'b', 'A', 2), Leg('b', 'd', 'B', 4)))
        inventory.materials.begin_tick(last + 1)
        inventory.transform({final: 1000}, {}, reason='household', use=Use.CIVILIAN)
        consumption = Consumption()
        self.settle(inventory, consumption, paths=(('a', 'b', 2), ('b', 'c', 3), ('b', 'd', 4)))
        self.assertEqual(consumption.receipt_mean20('O', 'export'), Q(1, 10))
        self.assertEqual(consumption.receipt_mean20('A', 'transit'), Q(1, 5))
        self.assertEqual(consumption.receipt_mean20('B', 'transit'), Q(2, 5))

    def test_fractional_vehicle_fuel_survives_empty_return_and_next_trip(self):
        food, fuel = Account('A', 'a', 'available', 'food'), Account('A', 'a', 'available', 'fuel')
        inventory = self.inventory({food: 2000, fuel: 1})
        vehicle = Vehicle('v', 'A', 'a', 'truck', 10000, Q(1), Q(1, 3))
        transport = Transport(inventory, {'ab': Corridor('ab', 'a', 'b', 1, 100000)}, {'v': vehicle})
        transport.launch('first', 'A', 'v', 'food', 1000, 'a', 'b', ('ab',), 0)
        last = self.drive(inventory, transport, 'first', 1)
        self.assertEqual(vehicle.fuel_credit_mU, Q(1, 2))
        self.assertEqual(inventory.materials.physical_by_owner('A')['fuel'], Q(1, 2))
        self.assertEqual(inventory.balance(fuel), 0)
        transport.launch('second', 'A', 'v', 'food', 1000, 'a', 'b', ('ab',), last)
        self.assertEqual(transport.shipments['second'].fuel_reserved_mU, 0)
        self.drive(inventory, transport, 'second', last + 1)
        self.assertEqual(vehicle.fuel_credit_mU, 0)
        self.assertEqual(inventory.materials.physical_by_owner('A').get('fuel', 0), 0)


if __name__ == '__main__':
    unittest.main()


class OriginSelectedTransferTests(MaterialFixture, unittest.TestCase):
    def fixture(self):
        a=Account('A','h','available','advanced_chip')
        b=Account('B','h','available','advanced_chip')
        mixed=Account('O','h','available','advanced_chip')
        stock=self.inventory({a:1000,b:1000})
        stock.transfer_many((Transfer(a,mixed,1000),Transfer(b,mixed,1000)))
        return stock,mixed

    def test_selected_and_unrestricted_draws_are_order_independent(self):
        outcomes=[]
        for reverse in (False,True):
            stock,mixed=self.fixture()
            a=Account('C','h','available','advanced_chip')
            b=Account('D','h','available','advanced_chip')
            transfers=[Transfer(mixed,a,1000,origin_nation='A'),Transfer(mixed,b,1000)]
            stock.transfer_many(tuple(reversed(transfers)) if reverse else tuple(transfers))
            self.assertEqual(stock.materials.composition(a),{Origin('A','h'):1000})
            self.assertEqual(stock.materials.composition(b),{Origin('B','h'):1000})
            outcomes.append(fingerprint(stock.materials.snapshot()))
            stock.audit()
        self.assertEqual(*outcomes)

    def test_wrong_origin_rejects_atomically_despite_sufficient_total_stock(self):
        stock,mixed=self.fixture()
        before=fingerprint(stock.snapshot())
        target=Account('C','h','available','advanced_chip')
        with self.assertRaises(InvariantViolation):
            stock.transfer_many((Transfer(mixed,target,1001,origin_nation='A'),))
        self.assertEqual(before,fingerprint(stock.snapshot()))
        with self.assertRaises(InvariantViolation):
            stock.transfer_many((Transfer(mixed,target,600,origin_nation='A'),
                                 Transfer(mixed,target,600,origin_nation='A')))
        self.assertEqual(before,fingerprint(stock.snapshot()))
        stock.audit()

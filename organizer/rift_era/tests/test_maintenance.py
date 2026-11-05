import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.ledger import Inventory, Account, CashLedger, CashAccount
from organizer.rift_era.engine.maintenance import Maintenance
from organizer.rift_era.engine.payments import Payments


class MaintenanceTests(unittest.TestCase):
    def fixture(self, quantity=100, money=1000):
        inventory, cash = Inventory(), CashLedger()
        stock = Account('A', 'h', 'available', 'parts')
        payer, seller = CashAccount('A', 'treasury', 'A'), CashAccount('A', 'supplier', 'A')
        inventory.initialize(stock, quantity)
        inventory.seal()
        inventory.enable_provenance()
        cash.initialize(payer, money)
        cash.seal()
        asset = Asset('f', 'A', 'h', {'steel': 1000000})
        maintenance = Maintenance(inventory, cash, Payments(cash), {'f': asset}, seed=1)
        maintenance.register('f', {'parts': 100}, payer, {'parts': seller})
        return inventory, cash, asset, maintenance, stock, payer, seller

    def tick(self, inventory, maintenance, tick):
        inventory.materials.begin_tick(tick)
        maintenance.begin_tick(tick)
        maintenance.reserve(maintenance.requests(lambda g, h: Q(2)))
        maintenance.audit()
        result = maintenance.settle()
        inventory.audit()
        maintenance.cash.audit()
        maintenance.audit()
        return result

    def test_due_period_consumes_real_parts_and_pays_supplier(self):
        inventory, cash, asset, maintenance, stock, payer, seller = self.fixture()
        self.assertEqual(self.tick(inventory, maintenance, 198), ())
        receipt, = self.tick(inventory, maintenance, 199)
        self.assertEqual(receipt.consumed_mU, {'parts': 100})
        self.assertEqual(cash.balance(payer).native_mU, 800)
        self.assertEqual(cash.balance(seller).native_mU, 200)
        self.assertEqual(inventory.balance(stock), 0)
        self.assertEqual(asset.maintenance_debt, 0)
        self.assertEqual(maintenance.status['f'].next_due_tick, 399)

    def test_partial_payment_debt_requires_material_and_two_full_cycles_to_clear(self):
        inventory, cash, asset, maintenance, stock, payer, seller = self.fixture(quantity=50)
        self.tick(inventory, maintenance, 199)
        self.assertEqual(asset.maintenance_debt, Q(1, 2))
        self.assertEqual(asset.availability, Q(85, 100))
        # Explicit test delivery, not an automatic shortage top-up in the engine.
        inventory.transform({}, {stock: 60}, reason='fixture_delivery')
        receipt, = self.tick(inventory, maintenance, 200)
        self.assertTrue(receipt.recovery)
        self.assertEqual(inventory.balance(stock), 0)
        self.assertEqual(asset.availability, 0)
        self.assertEqual(asset.maintenance_debt, Q(1, 2))
        for tick in range(201, 221):
            self.tick(inventory, maintenance, tick)
            self.assertEqual(asset.availability, 0)
        self.tick(inventory, maintenance, 221)
        self.assertEqual(asset.maintenance_debt, 0)
        self.assertEqual(asset.availability, 1)
        self.assertEqual(cash.balance(seller).native_mU, 220)

    def test_no_cash_cannot_buy_maintenance_even_with_parts(self):
        inventory, cash, asset, maintenance, stock, payer, seller = self.fixture(money=0)
        self.tick(inventory, maintenance, 199)
        self.assertEqual(inventory.balance(stock), 100)
        self.assertEqual(asset.maintenance_debt, 1)
        self.assertEqual(cash.balance(seller).native_mU, 0)

    def test_fault_probability_is_clamped_and_outage_lasts_four_cycles(self):
        inventory, cash, asset, maintenance, stock, payer, seller = self.fixture(quantity=0)
        asset.maintenance_debt = Q(20)
        self.tick(inventory, maintenance, 0)
        self.assertEqual(maintenance.status['f'].fault_until_tick, 40)
        for tick in range(1, 40):
            self.tick(inventory, maintenance, tick)
            self.assertEqual(asset.availability, 0)


if __name__ == '__main__':
    unittest.main()

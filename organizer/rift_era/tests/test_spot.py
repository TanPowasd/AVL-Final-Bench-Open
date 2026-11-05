import random
import unittest
from dataclasses import replace
from fractions import Fraction as Q

from organizer.rift_era.engine.errors import InvariantViolation, RuleViolation
from organizer.rift_era.engine.ledger import (
    Account, CashAccount, CashLedger, CashTransfer, DeliveryPayment, Inventory, Transfer,
)
from organizer.rift_era.engine.spot import RestingOrder, SpotExchange, Venue


class AtomicSettlementTests(unittest.TestCase):
    def setUp(self):
        self.goods, self.cash = Inventory(), CashLedger()
        self.seller = Account('seller', 'h', 'available', 'steel')
        self.buyer = Account('buyer', 'h', 'next_available', 'steel')
        self.payer = CashAccount('buyer', 'reserve', 'RU')
        self.payee = CashAccount('seller', 'reserve', 'RU')
        self.goods.initialize(self.seller, 1000)
        self.cash.initialize(self.payer, 1000, rate=Q(3, 2))
        self.goods.seal()
        self.cash.seal()

    def snapshot(self):
        return self.goods.snapshot(), self.cash.snapshot()

    def test_either_insufficient_leg_leaves_both_books_unchanged(self):
        before = self.snapshot()
        for quantity, cost in ((1001, 1), (1, 1001)):
            with self.assertRaises(RuleViolation):
                DeliveryPayment.prepare(self.goods, self.cash, [Transfer(self.seller, self.buyer, quantity)],
                                        [CashTransfer(self.payer, self.payee, cost)])
            self.assertEqual(before, self.snapshot())

    def test_preflight_is_pure_stale_commit_rejects_before_any_write(self):
        before = self.snapshot()
        plan = DeliveryPayment.prepare(self.goods, self.cash, [Transfer(self.seller, self.buyer, 100)],
                                       [CashTransfer(self.payer, self.payee, 50)])
        self.assertEqual(before, self.snapshot())
        self.cash.transfer(self.payer, self.payee, 1)
        before = self.snapshot()
        with self.assertRaises(InvariantViolation):
            plan.commit()
        self.assertEqual(before, self.snapshot())

    def test_exact_basis_and_no_reexecution(self):
        plan = DeliveryPayment.prepare(self.goods, self.cash, [Transfer(self.seller, self.buyer, 100)],
                                       [CashTransfer(self.payer, self.payee, 51)])
        plan.commit()
        self.assertEqual(self.cash.balance(self.payee).book_ru_mU, Q(153, 2))
        self.assertEqual(self.goods.balance(self.buyer), 100)
        before = self.snapshot()
        with self.assertRaises(InvariantViolation):
            plan.commit()
        self.assertEqual(before, self.snapshot())
        self.cash.audit()
        self.goods.audit()

    def test_incoming_cash_or_goods_cannot_back_other_debits(self):
        before = self.snapshot()
        with self.assertRaises(RuleViolation):
            DeliveryPayment.prepare(self.goods, self.cash, [Transfer(self.seller, self.buyer, 100)],
                [CashTransfer(self.payer, self.payee, 50), CashTransfer(self.payee, self.payer, 20)])
        self.assertEqual(before, self.snapshot())
        with self.assertRaises(RuleViolation):
            self.goods.prepare_transfers([Transfer(self.seller, self.buyer, 100), Transfer(self.buyer, self.seller, 1)])
        self.assertEqual(before, self.snapshot())


class SpotTests(unittest.TestCase):
    def fixture(self, *, quantum=1, tariff=Q(0)):
        goods, cash = Inventory(), CashLedger()
        for owner in ('b1', 'b2', 's1', 's2'):
            goods.initialize(Account(owner, 'h', 'available', 'steel'), 20000)
            cash.initialize(CashAccount(owner, 'treasury', 'C'), 100000, rate=Q(3, 2))
        goods.seal()
        cash.seal()
        exchange = SpotExchange(goods, cash)
        exchange.add_venue(Venue('v', 'h', 'steel', 'C', 1000, quantum, tariff, CashAccount('host', 'tax', 'C')))
        return exchange

    def order(self, owner, sequence, side, quantity, price=1000, **kwargs):
        return RestingOrder(f'{owner}/{sequence}', str(sequence), owner, 'v', side, quantity, price, 100,
            CashAccount(owner, 'treasury', 'C'), Account(owner, 'h', 'available' if side == 'sell' else 'next_available', 'steel'),
            **kwargs)

    def post(self, exchange, *orders):
        for order in orders:
            before = self.snapshot(exchange)
            prepared = exchange.prepare_order(order, tick=0)
            self.assertEqual(before, self.snapshot(exchange))
            prepared.commit()

    def snapshot(self, e):
        # snapshot() is a read view; primitive freezes mutable mappings for tests.
        from organizer.rift_era.engine.determinism import primitive
        return primitive((e.snapshot(), e.inventory.snapshot(), e.cash.snapshot()))

    def audit(self, e):
        before = self.snapshot(e)
        e.inventory.audit()
        e.cash.audit()
        e.audit()
        self.assertEqual(before, self.snapshot(e))

    def test_delivery_is_local_buffered_and_cash_is_real(self):
        e = self.fixture(tariff=Q(1, 10))
        self.post(e, self.order('b1', 1, 'buy', 1500, 1200), self.order('s1', 1, 'sell', 1000, 900))
        self.assertEqual(e.inventory.balance(Account('s1', 'h', 'available', 'steel')), 19000)
        result = e.clear(12, 4, 'v')
        self.assertEqual((result.quantity_mU, result.price_mU, result.paid_mU, result.tariff_mU), (1000, 1000, 1000, 100))
        self.assertEqual(e.inventory.balance(Account('b1', 'h', 'next_available', 'steel')), 1000)
        self.assertEqual(e.inventory.balance(Account('b1', 'h', 'available', 'steel')), 20000)
        self.assertEqual(e.cash.balance(CashAccount('s1', 'treasury', 'C')).native_mU, 101000)
        self.assertEqual(e.cash.balance(CashAccount('host', 'tax', 'C')).native_mU, 100)
        self.assertEqual(e.orders['b1/1'].remaining_mU, 500)
        e.prepare_cancel('b1', 'b1/1').commit()
        self.assertEqual(e.cash.balance(CashAccount('b1', 'treasury', 'C')).native_mU, 98900)
        self.audit(e)

    def test_rejected_order_does_not_register_venue_or_lock_any_assets(self):
        e = self.fixture()
        before = self.snapshot(e)
        order = replace(self.order('b1', 1, 'buy', 999999999), venue='new')
        with self.assertRaises(RuleViolation):
            e.prepare_order(order, tick=0, new_venue=Venue('new', 'h', 'steel', 'C', 1000))
        self.assertEqual(before, self.snapshot(e))

    def test_cancel_and_expiry_refund_full_original_basis(self):
        e = self.fixture()
        buy, sell = self.order('b1', 1, 'buy', 1000), self.order('s1', 1, 'sell', 1000)
        self.post(e, buy, sell)
        self.assertEqual(e.expire(99), ())
        e.prepare_cancel('s1', sell.id).commit()
        self.assertEqual(e.inventory.balance(sell.stock_account), 20000)
        self.assertEqual(e.expire(100), (buy.id,))
        self.assertEqual(e.cash.balance(buy.cash_account).native_mU, 100000)
        self.assertEqual(e.cash.balance(buy.cash_account).book_ru_mU, 150000)
        self.audit(e)

    def test_cannot_sell_custody_twice_or_cancel_another_owners_order(self):
        e = self.fixture()
        self.post(e, self.order('s1', 1, 'sell', 20000))
        before = self.snapshot(e)
        with self.assertRaises(RuleViolation):
            e.prepare_order(self.order('s1', 2, 'sell', 1), tick=0)
        with self.assertRaisesRegex(RuleViolation, 'ERR_UNKNOWN_TARGET_ID'):
            e.prepare_cancel('b1', 's1/1')
        self.assertEqual(before, self.snapshot(e))

    def test_uniform_auction_permutation_and_whole_machine_quantum(self):
        orders = [self.order('b1', 1, 'buy', 5000, 1100), self.order('b2', 1, 'buy', 5000, 1100),
                  self.order('s1', 1, 'sell', 7000, 900)]
        snapshots = []
        for sequence in (orders, list(reversed(orders))):
            e = self.fixture(quantum=1000)
            self.post(e, *sequence)
            result = e.clear(3, 4, 'v')
            self.assertEqual(result.quantity_mU, 7000)
            self.assertEqual(sorted(result.buy_fills.values()), [3000, 4000])
            self.audit(e)
            snapshots.append(self.snapshot(e))
        self.assertEqual(*snapshots)

    def test_many_partial_sub_mu_orders_cannot_erase_payment_and_fee_carries(self):
        e = self.fixture(tariff=Q(1, 5))
        # 100 x 0.1 mU principal = 10 mU; 100 x 0.02 mU tax = 2 mU.
        for sequence in range(1, 101):
            self.post(e, self.order('b1', sequence, 'buy', 1, 100), self.order('s1', sequence, 'sell', 1, 100))
            e.clear(8, sequence - 1, 'v')
        self.assertEqual(e.cash.balance(CashAccount('b1', 'treasury', 'C')).native_mU, 99988)
        self.assertEqual(e.cash.balance(CashAccount('s1', 'treasury', 'C')).native_mU, 100010)
        self.assertEqual(e.cash.balance(CashAccount('host', 'tax', 'C')).native_mU, 2)
        self.assertFalse(e.orders)
        self.assertFalse(e.residuals)
        self.audit(e)

    def test_many_tiny_simultaneous_orders_preserve_each_lock_budget(self):
        e = self.fixture(tariff=Q(3, 10))
        orders = [self.order('b1', seq, 'buy', 1, 501) for seq in range(1, 101)]
        self.post(e, *orders, self.order('s1', 1, 'sell', 100, 501))
        result = e.clear(4, 4, 'v')
        self.assertEqual((result.quantity_mU, result.paid_mU, result.tariff_mU), (100, 50, 15))
        self.audit(e)

    def test_random_partial_fills_cancel_and_repost_preserve_global_books(self):
        randomizer = random.Random(193)
        e = self.fixture(tariff=Q(1777, 10000))
        for tick in range(80):
            for owner, side in (('b1', 'buy'), ('b2', 'buy'), ('s1', 'sell'), ('s2', 'sell')):
                order = self.order(owner, tick + 1, side, randomizer.randrange(1, 100), randomizer.randrange(900, 1100))
                e.prepare_order(order, tick=tick).commit()
            e.clear(3, tick, 'v')
            for order in tuple(e.orders.values()):
                if randomizer.randrange(4) == 0:
                    e.prepare_cancel(order.owner, order.id).commit()
            self.audit(e)
        e.expire(100)
        self.assertFalse(e.orders)
        self.audit(e)

    def test_bilateral_dvp_rejects_without_cash_and_keeps_goods_at_seller_hub(self):
        e = self.fixture()
        offer = self.order('s1', 1, 'sell', 1000, bilateral=True)
        self.post(e, offer)
        buyer = self.order('b1', 1, 'buy', 1000)
        e.cash.transfer(buyer.cash_account, offer.cash_account, 100000)
        before = self.snapshot(e)
        with self.assertRaises(RuleViolation):
            e.prepare_accept(0, 1, buyer, offer.id)
        self.assertEqual(before, self.snapshot(e))
        e.cash.transfer(offer.cash_account, buyer.cash_account, 1000)
        prepared, result = e.prepare_accept(0, 1, buyer, offer.id)
        prepared.commit()
        self.assertEqual(result.quantity_mU, 1000)
        self.assertEqual(e.inventory.balance(buyer.stock_account), 1000)
        self.assertEqual(e.cash.balance(buyer.cash_account).native_mU, 0)
        self.audit(e)

    def test_competing_acceptances_split_offer_and_refund_excess(self):
        snapshots = []
        for reverse in (False, True):
            e = self.fixture()
            offer = self.order('s1', 1, 'sell', 1001, bilateral=True)
            self.post(e, offer)
            bids = [self.order(b, 1, 'buy', 1000, 2000, bilateral=True, offer_id=offer.id) for b in ('b1', 'b2')]
            self.post(e, *(reversed(bids) if reverse else bids))
            result, = e.clear_offers(2, 1)
            self.assertEqual(sorted(result.buy_fills.values()), [500, 501])
            self.assertEqual(sum(e.cash.balance(b.cash_account).native_mU for b in bids), 198999)
            self.assertFalse(e.orders)
            self.audit(e)
            snapshots.append(self.snapshot(e))
        self.assertEqual(*snapshots)

    def test_cancelled_offer_refunds_all_pending_buyers(self):
        e = self.fixture()
        offer = self.order('s1', 1, 'sell', 1000, bilateral=True)
        buyer = self.order('b1', 1, 'buy', 1000, bilateral=True, offer_id=offer.id)
        self.post(e, offer, buyer)
        e.prepare_cancel('s1', offer.id).commit()
        self.assertEqual(e.clear_offers(0, 1), ())
        self.assertEqual(e.cash.balance(buyer.cash_account).native_mU, 100000)
        self.assertEqual(e.inventory.balance(offer.stock_account), 20000)
        self.audit(e)

    def test_public_offer_aliases_hide_private_sequence_and_queries_are_read_only(self):
        views = []
        for private_sequence in (1, 987654321):
            e = self.fixture()
            offer = self.order('s1', private_sequence, 'sell', 1000, bilateral=True)
            self.post(e, offer)
            e.publish_offers(('b1',))
            before = self.snapshot(e)
            views.append(e.visible('b1'))
            for _ in range(20):
                self.assertEqual(e.visible('b1'), views[-1])
            self.assertEqual(before, self.snapshot(e))
            self.assertEqual(e.resolve_offer('b1', 'offer-1'), offer.id)
            self.assertIsNone(e.resolve_offer('b2', 'offer-1'))
            e.prepare_cancel('s1', offer.id).commit()
            e.publish_offers(('b1',))
            self.assertIsNone(e.resolve_offer('b1', 'offer-1'))
            self.post(e, self.order('s1', private_sequence + 1, 'sell', 1000, bilateral=True))
            e.publish_offers(('b1', 'b2'))
            self.assertEqual(e.visible('b1')['export_offers'][0]['id'], 'offer-2')
            self.assertEqual(e.visible('b2')['export_offers'][0]['id'], 'offer-1')
        self.assertEqual(*views)


if __name__ == '__main__':
    unittest.main()

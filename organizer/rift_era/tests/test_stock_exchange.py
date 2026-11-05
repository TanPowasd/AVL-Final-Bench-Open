import unittest

from organizer.rift_era.engine.errors import InvariantViolation,RuleViolation
from organizer.rift_era.engine.ledger import Account,CashAccount,CashLedger,Inventory
from organizer.rift_era.engine.market import Order,clear_auction
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.spot import RestingOrder,Venue
from organizer.rift_era.engine.stock_exchange import StockExchange


class StockExchangeTests(unittest.TestCase):
    def fixture(self,shares=20,discounts=None,unissued=False):
        shares_ledger=Inventory();cash=CashLedger()
        shares_ledger.initialize(Account('seller','securities','unissued' if unissued else 'available','company'),shares*1000)
        shares_ledger.seal();cash.initialize(CashAccount('buyer','treasury','C'),100000,rate=Q(3,2));cash.seal()
        market=StockExchange(shares_ledger,cash,discounts=lambda p:(discounts or {}).get(p,Q(0)))
        market.add_venue(Venue('company','securities','company','C',100,1000,Q(1,500),CashAccount('world','pool','C')))
        return market

    def order(self,owner,seq,side,shares,source='available'):
        return RestingOrder(f'{owner}/{seq}',str(seq),owner,'company',side,shares*1000,100,100,
            CashAccount(owner,'treasury','C'),Account(owner,'securities',source if side=='sell' else 'next_available','company'),cash_namespace='stock_cash:')

    def post(self,market,*orders):
        for order in orders:market.prepare_order(order,tick=0).commit()

    def test_whole_share_dvp_two_real_fees_and_exact_principal_basis(self):
        market=self.fixture()
        self.post(market,self.order('buyer',1,'buy',10),self.order('seller',1,'sell',10))
        clearing=market.clear(7,1,'company',allowed=lambda a,b:a!=b)
        self.assertEqual((clearing.quantity_mU,clearing.paid_mU),(10000,1000))
        self.assertEqual(clearing.buyer_tariffs,{'buyer/1':2})
        self.assertEqual(clearing.seller_fees,{'seller/1':2})
        self.assertEqual(market.cash.balance(CashAccount('buyer','treasury','C')).native_mU,98998)
        self.assertEqual(market.cash.balance(CashAccount('seller','treasury','C')).native_mU,998)
        self.assertEqual(market.cash.balance(CashAccount('world','pool','C')).native_mU,4)
        self.assertEqual(clearing.buyer_cost_book_ru_mU,{'buyer/1':1500})
        self.assertEqual(clearing.seller_capital_book_ru_mU,{'seller/1':1500})
        self.assertEqual(market.inventory.balance(Account('buyer','securities','next_available','company')),10000)
        market.audit();market.cash.audit();market.inventory.audit()

    def test_partial_fills_keep_collateral_and_carry_without_new_funding(self):
        market=self.fixture()
        self.post(market,self.order('buyer',1,'buy',10),self.order('seller',1,'sell',4))
        first=market.clear(7,1,'company')
        self.assertEqual(first.buyer_tariffs['buyer/1'],0)
        self.assertEqual(market.cash.balance(market.orders['buyer/1'].cash_lock).native_mU,602)
        self.post(market,self.order('seller',2,'sell',6))
        second=market.clear(7,2,'company')
        self.assertEqual(second.buyer_tariffs['buyer/1'],2)
        self.assertEqual(second.seller_fees['seller/2'],2)
        self.assertNotIn('buyer/1',market.orders)
        self.assertEqual(first.buyer_cost_book_ru_mU['buyer/1']+second.buyer_cost_book_ru_mU['buyer/1'],1500)
        market.audit();market.cash.audit();market.inventory.audit()

    def test_split_orders_do_not_reset_fees_or_increase_account_proceeds(self):
        values=[]
        for parts in ((10,),(5,5),(1,)*10):
            market=self.fixture()
            for seq,shares in enumerate(parts,1):
                self.post(market,self.order('buyer',seq,'buy',shares),self.order('seller',seq,'sell',shares))
            clearing=market.clear(7,1,'company')
            values.append((market.cash.balance(CashAccount('buyer','treasury','C')),
                           market.cash.balance(CashAccount('seller','treasury','C')),
                           sum(clearing.buyer_cost_book_ru_mU.values(),Q(0))))
            market.audit();market.cash.audit();market.inventory.audit()
        self.assertTrue(all(v==values[0] for v in values))

    def test_actual_ai_discount_changes_fee_and_refunds_unused_budget(self):
        market=self.fixture(discounts={'buyer':Q(1,2),'seller':Q(1,2)})
        self.post(market,self.order('buyer',1,'buy',10),self.order('seller',1,'sell',10))
        clearing=market.clear(7,1,'company')
        self.assertEqual(clearing.buyer_tariffs['buyer/1'],1)
        self.assertEqual(clearing.seller_fees['seller/1'],1)
        self.assertEqual(market.cash.balance(CashAccount('buyer','treasury','C')).native_mU,98999)
        self.assertEqual(market.cash.balance(CashAccount('seller','treasury','C')).native_mU,999)
        market.cash.audit();market.audit()

    def test_cancelled_issue_returns_restricted_stock_not_free_inventory(self):
        market=self.fixture(unissued=True)
        order=self.order('seller',1,'sell',10,source='unissued')
        market.prepare_issue(order,tick=0).commit()
        self.assertEqual(market.inventory.balance(order.stock_lock),10000)
        market.prepare_cancel('seller',order.id).commit()
        self.assertEqual(market.inventory.balance(Account('seller','securities','unissued','company')),20000)
        self.assertEqual(market.inventory.balance(Account('seller','securities','available','company')),0)
        with self.assertRaises(RuleViolation):market.prepare_order(self.order('seller',2,'sell',1),tick=0)
        market.audit();market.inventory.audit()

    def test_stale_cash_fails_before_stock_fees_or_order_mutation(self):
        market=self.fixture();self.post(market,self.order('buyer',1,'buy',10),self.order('seller',1,'sell',10))
        auction=clear_auction(7,1,'company',tuple(Order(o.id,o.owner,o.side,o.remaining_mU,o.limit_price_mU)
            for o in market.orders.values()),100,quantum=1000)
        _,commit=market._prepare_settlement(7,1,market.venues['company'],auction)
        market.cash.transfer(CashAccount('buyer','treasury','C'),CashAccount('other','treasury','C'),1)
        before=(market.snapshot(),market.inventory.snapshot(),market.cash.snapshot())
        with self.assertRaises(InvariantViolation):commit()
        self.assertEqual(before,(market.snapshot(),market.inventory.snapshot(),market.cash.snapshot()))

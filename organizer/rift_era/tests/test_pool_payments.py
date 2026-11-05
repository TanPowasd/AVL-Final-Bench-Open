import unittest

from organizer.rift_era.engine.ledger import CashAccount,CashLedger
from organizer.rift_era.engine.payments import ForeignExchange,Payments,FXRequest,PoolPayment
from organizer.rift_era.engine.numeric import Q


class CombinedPoolTests(unittest.TestCase):
    def fixture(self,native=10,ru=10):
        cash=CashLedger()
        cash.initialize(CashAccount('world','pool','C'),native,rate=Q(2))
        cash.initialize(CashAccount('world','pool','RU'),ru)
        cash.initialize(CashAccount('buyer','reserves','RU'),100)
        cash.initialize(CashAccount('seller','treasury','C'),100)
        cash.seal()
        return cash,ForeignExchange(cash,Payments(cash))

    def test_fx_and_direct_payouts_share_one_frozen_currency_pool(self):
        cash,fx=self.fixture()
        native=CashAccount('buyer','treasury','C');ru=CashAccount('buyer','reserves','RU')
        sale=CashAccount('seller','treasury','C');sale_ru=CashAccount('seller','reserves','RU')
        loan=CashAccount('borrower','margin_funding','C')
        fx.execute(7,0,(FXRequest('buy_native',ru,native,20,Q(1)),FXRequest('sell_native',sale,sale_ru,20,Q(1))),
            pool_payments=(PoolPayment('loan',loan,20),))
        self.assertEqual(fx.last_fills['buy_native'].received_mU,5)
        self.assertEqual(fx.last_pool_fills['loan'].paid_mU,5)
        self.assertEqual(fx.last_pool_fills['loan'].book_ru_mU,10)
        self.assertEqual(cash.balance(loan).book_ru_mU,10)
        self.assertEqual(fx.last_fills['sell_native'].received_mU,10)
        # The additional 10 C received in this same batch could not finance
        # either request above. It remains in the pool for a later stage.
        self.assertEqual(cash.balance(CashAccount('world','pool','C')).native_mU,10)
        cash.audit()

    def test_empty_pool_neither_funds_credit_nor_burns_offered_reserves(self):
        cash,fx=self.fixture(native=0)
        source=CashAccount('buyer','reserves','RU');target=CashAccount('buyer','treasury','C')
        loan=CashAccount('borrower','margin_funding','C')
        fx.execute(7,0,(FXRequest('buyer',source,target,20,Q(1)),),pool_payments=(PoolPayment('loan',loan,20),))
        self.assertEqual(fx.last_fills['buyer'].received_mU,0)
        self.assertEqual(fx.last_pool_fills['loan'].paid_mU,0)
        self.assertEqual(cash.balance(source).native_mU,100)
        self.assertEqual(cash.balance(loan).native_mU,0)
        cash.audit()

    def test_splitting_same_recipient_cannot_win_an_extra_pool_rounding_share(self):
        for seed in range(30):
            outcomes=[]
            for split in (False,True):
                cash,fx=self.fixture(native=1)
                a=CashAccount('a','portfolio','C');b=CashAccount('b','portfolio','C')
                claims=(PoolPayment('a1',a,1),PoolPayment('a2',a,1)) if split else (PoolPayment('a',a,2),)
                fx.execute(seed,0,(),pool_payments=claims+(PoolPayment('b',b,1),))
                outcomes.append((cash.balance(a),cash.balance(b)))
                self.assertEqual(sum(f.paid_mU for f in fx.last_pool_fills.values()),1)
                self.assertEqual(sum((f.book_ru_mU for f in fx.last_pool_fills.values()),Q(0)),2)
            self.assertEqual(*outcomes)

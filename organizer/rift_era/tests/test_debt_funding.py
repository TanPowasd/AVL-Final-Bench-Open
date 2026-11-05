import unittest
from types import SimpleNamespace as NS

from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.debt_funding import DebtFunding, FundingClaim
from organizer.rift_era.engine.ledger import CashAccount, CashLedger
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.payments import ForeignExchange, Payments


class DebtFundingTests(unittest.TestCase):
    def fixture(self, native=100, ru=0, counterparty=1000):
        cash=CashLedger()
        for account,amount in ((CashAccount('n0','treasury','n0'),native),
                (CashAccount('n0','reserves','RU'),ru),
                (CashAccount('world','pool','n0'),counterparty),
                (CashAccount('world','pool','RU'),1000)):
            cash.initialize(account,amount)
        cash.seal()
        state=NS(initial=NS(cash=cash),rates={'n0':Q(1)},fx=ForeignExchange(cash,Payments(cash)))
        service=DebtFunding(state)
        ctx=NS(seed=17,tick=9,cycle=0,clock=ClockConfig())
        return state,service,ctx

    def test_interest_across_providers_precedes_all_principal(self):
        state,service,ctx=self.fixture()
        a=CashAccount('n0','bond_reserved','n0');b=CashAccount('n0','margin_reserved','n0')
        service.add_provider(lambda ctx:(FundingClaim('a_i','n0','n0',0,20,a),FundingClaim('a_p','n0','n0',1,100,a)))
        service.add_provider(lambda ctx:(FundingClaim('b_i','n0','n0',0,80,b),))
        service.fund(ctx)
        self.assertEqual(state.initial.cash.balance(a).native_mU,20)
        self.assertEqual(state.initial.cash.balance(b).native_mU,80)
        state.initial.cash.audit()

    def test_p7_recalls_old_principal_reservation_for_new_interest(self):
        state,service,ctx=self.fixture()
        a=CashAccount('n0','a','n0');b=CashAccount('n0','b','n0')
        claims=[FundingClaim('a','n0','n0',1,100,a)]
        service.add_provider(lambda ctx:tuple(claims));service.fund(ctx)
        claims.append(FundingClaim('b','n0','n0',0,50,b));service.fund(ctx)
        self.assertEqual(state.initial.cash.balance(a).native_mU,50)
        self.assertEqual(state.initial.cash.balance(b).native_mU,50)
        claims.clear();service.fund(ctx)
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','treasury','n0')).native_mU,100)
        state.initial.cash.audit()

    def test_failed_fx_retains_reserves_and_same_priority_is_proportional(self):
        state,service,ctx=self.fixture(native=30,ru=100,counterparty=0)
        accounts=[CashAccount('n0',name,'n0') for name in ('a','b')]
        service.add_provider(lambda ctx:tuple(FundingClaim(name,'n0','n0',0,q,a)
            for name,q,a in zip(('a','b'),(100,200),accounts)))
        service.fund(ctx)
        self.assertEqual([state.initial.cash.balance(a).native_mU for a in accounts],[10,20])
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','reserves','RU')).native_mU,100)
        state.initial.cash.audit()

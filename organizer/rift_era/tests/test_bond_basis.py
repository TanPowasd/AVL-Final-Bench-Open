"""Historical bond basis follows real cash, not face or a screen FX quote."""
import unittest
from types import SimpleNamespace as NS

from organizer.rift_era.tests import test_sovereign_finance as fixtures
from organizer.rift_era.engine.sovereign_finance import BondCommands
from organizer.rift_era.engine.bond_market import BondOrderCommands,FinancialExchange
from organizer.rift_era.engine.ledger import CashAccount,CashLedger,Inventory,Account,CashTransfer
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.spot import Venue,RestingOrder
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.errors import InvariantViolation


class BondBasisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SovereignFinanceTests.setUpClass();cls.helper=fixtures.SovereignFinanceTests()

    def issued(self,face=100000):
        state=self.helper.state();book=state.sovereign_finance
        BondCommands(state).prepare('n0',{'id':'1','kind':'finance','op':'bond_issue','term':20,
            'face_mU':face,'min_price_mU':1000},self.helper.ctx(0)).commit()
        book.begin_tick(self.helper.ctx(0,False));book.clear_primary(self.helper.ctx(0))
        return state,next(iter(book.bonds.values()))

    def buy(self,state,bond,owner='n1',seq='2'):
        state.initial.cash.transfer(CashAccount('world','pool','n0'),CashAccount(owner,'treasury','n0'),bond.face)
        alias=state.sovereign_finance.aliases[owner][bond.id]
        BondOrderCommands(state).prepare(owner,{'id':seq,'kind':'finance','op':'bond_order','bond_id':alias,
            'side':'buy','qty_mU':bond.face,'limit_price_mU':1000},self.helper.ctx(1)).commit()
        state.bond_market.clear(self.helper.ctx(1))

    def test_actual_primary_cash_creates_equal_debt_without_raising_borrower_nfa(self):
        state=self.helper.state();before=state.initial.cash.owner_book('n0')
        book=state.sovereign_finance
        BondCommands(state).prepare('n0',{'id':'1','kind':'finance','op':'bond_issue','term':20,'face_mU':100000,'min_price_mU':1000},self.helper.ctx(0)).commit()
        book.begin_tick(self.helper.ctx(0,False));book.clear_primary(self.helper.ctx(0))
        self.assertEqual(state.initial.cash.owner_book('n0')-book.debt_book('n0'),before)
        self.assertEqual(state.bond_market.asset_book('world'),book.debt_book('n0'))
        old=book.debt_book('n0');state.rates['n0']=Q(7,3)
        self.assertEqual(book.debt_book('n0'),old)
        state.audit()

    def test_secondary_price_cost_is_separate_from_issuer_debt_and_screen_fx(self):
        state,bond=self.issued();debt=state.sovereign_finance.debt_book('n0')
        self.buy(state,bond)
        asset=state.bond_market.asset_book('n1')
        self.assertGreater(asset,0)
        self.assertEqual(state.bond_market.asset_book('world'),0)
        self.assertEqual(state.sovereign_finance.debt_book('n0'),debt)
        state.rates['n0']=Q(9,5)
        self.assertEqual(state.bond_market.asset_book('n1'),asset)
        state.audit()

    def test_coupon_then_maturity_disposes_all_principal_basis(self):
        state,bond=self.issued();self.buy(state,bond)
        state.sovereign_finance.settle(self.helper.ctx(20))
        self.assertEqual(state.sovereign_finance.debt_book('n0'),0)
        self.assertEqual(state.bond_market.asset_book('n1'),0)
        self.assertFalse(state.sovereign_finance.bonds)
        state.audit()

    def test_partial_default_retains_real_remaining_debt_and_capitalized_interest(self):
        state,bond=self.issued();self.buy(state,bond)
        self.helper.cash(state,native=0,ru=0,pool_native=0)
        before=state.sovereign_finance.debt_book('n0');asset=state.bond_market.asset_book('n1')
        state.rates['n0']=Q(2)
        state.sovereign_finance.settle(self.helper.ctx(20))
        unpaid=state.sovereign_finance.reports['n0']['due_mU']-100000
        self.assertTrue(state.sovereign_finance.reports['n0']['defaulted'])
        self.assertEqual(state.sovereign_finance.debt_book('n0'),before+2*unpaid)
        self.assertEqual(state.bond_market.asset_book('n1'),asset+2*unpaid)
        state.audit()

    def test_actual_cash_plan_multi_seller_rounding_is_exact_and_prepare_is_pure(self):
        cash=CashLedger();payer=CashAccount('B','treasury','C')
        cash.initialize(payer,7,rate=Q(1,3));cash.seal()
        faces=Inventory();faces.initialize(Account('S1','h','available','bond'),1);faces.initialize(Account('S2','h','available','bond'),1);faces.seal()
        ex=FinancialExchange(faces,cash,cash_namespace='bond_cash:')
        ex.add_venue(Venue('v','h','bond','C',1000))
        for seller in ('S1','S2'):
            ex.prepare_order(RestingOrder(seller,seller,seller,'v','sell',1,1000,10,CashAccount(seller,'treasury','C'),Account(seller,'h','available','bond'),cash_namespace='bond_cash:'),tick=0).commit()
        bid=RestingOrder('bid','bid','B','v','buy',2,1000,10,payer,Account('B','h','next_available','bond'),cash_namespace='bond_cash:')
        ex.prepare_order(bid,tick=0).commit()
        # Settle a 2/7 historical-cost custody into two real sellers. The sum
        # must equal their actual moved basis, including CashLedger tails.
        seller_before=sum(cash.owner_book(s) for s in ('S1','S2'))
        ex.clear(1,1,'v')
        received=sum(cash.owner_book(s) for s in ('S1','S2'))-seller_before
        self.assertEqual(ex.buyer_books['bid'],received)
        self.assertEqual(sum(cash.balance(a).book_ru_mU for a in cash.balances()),Q(round(Q(7,3)*10**12),10**12))
        cash.audit();faces.audit()


if __name__=='__main__':unittest.main()

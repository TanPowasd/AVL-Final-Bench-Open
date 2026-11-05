import unittest
from types import SimpleNamespace as NS

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import CashAccount
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.sovereign_finance import BondCommands, SovereignBond
from organizer.rift_era.engine.world import generate_candidate


class SovereignFinanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def state(self):
        state=opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=20,commerce=True,debug=False).state
        state.research['n0'].known.add('F02')
        return state

    @staticmethod
    def ctx(cycle,end=True):
        return NS(seed=1000,tick=cycle*10+(9 if end else 0),cycle=cycle,clock=ClockConfig())

    def cash(self,state,native=0,ru=0,pool_native=None):
        ledger=state.initial.cash
        for currency,name,amount in (('n0','treasury',native),('RU','reserves',ru)):
            account=CashAccount('n0',name,currency)
            ledger.transfer(account,CashAccount('test','escrow',currency),ledger.balance(account).native_mU)
            ledger.transfer(CashAccount('world','pool',currency),account,amount)
        if pool_native is not None:
            account=CashAccount('world','pool','n0')
            current=ledger.balance(account).native_mU
            ledger.transfer(account,CashAccount('test','pool_escrow','n0'),current-pool_native)

    def bond(self,state,face=1000000,rate=0,due=20,id='b'):
        bond=SovereignBond(id,'n0',0,due,rate,{'world':face},Q(face))
        state.sovereign_finance.bonds[id]=bond
        state.bond_market.register(bond)
        return bond

    def test_issue_requires_real_world_cash_and_rejection_does_not_mutate(self):
        state=self.state();book=state.sovereign_finance;handler=BondCommands(state)
        order={'id':'1','kind':'finance','op':'bond_issue','term':20,'face_mU':1000000,'min_price_mU':1000}
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):handler.prepare('n0',order|{'face_mU':1000001},self.ctx(0))
        self.assertEqual(before,fingerprint(state.snapshot()))
        self.cash(state,pool_native=500000)
        handler.prepare('n0',order,self.ctx(0)).commit()
        book.begin_tick(self.ctx(0,False));book.clear_primary(self.ctx(0))
        self.assertEqual(book.outstanding('n0'),50000)
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','treasury','n0')).native_mU,50000)
        self.assertEqual(book.offers['bond_issue:n0:1'].remaining_mU,950000)
        book.clear_primary(self.ctx(0))
        self.assertEqual(book.outstanding('n0'),50000) # one shared cycle allowance
        state.audit()

    def test_foreign_reserves_pay_actual_maturity_before_default(self):
        state=self.state();book=state.sovereign_finance
        self.bond(state);self.cash(state,ru=2000000)
        world_before=state.initial.cash.balance(CashAccount('world','pool','n0')).native_mU
        book.reserve(self.ctx(20));book.settle(self.ctx(20))
        self.assertEqual(book.reports['n0'],{'due_mU':1000000,'paid_mU':1000000,'defaulted':False})
        self.assertEqual(book.default_count['n0'],0)
        self.assertFalse(book.bonds)
        self.assertEqual(state.initial.cash.balance(CashAccount('world','pool','n0')).native_mU,world_before)
        self.assertLess(state.initial.cash.balance(CashAccount('n0','reserves','RU')).native_mU,1000000) # actual FX fee
        state.audit()

    def test_empty_native_counterparty_does_not_burn_reserves_and_defaults_once(self):
        state=self.state();book=state.sovereign_finance
        bond=self.bond(state,rate=1000);self.cash(state,ru=2000000,pool_native=0)
        credit=state.credit['n0'];support=state.initial.populations['n0-h0'].support_mU
        book.reserve(self.ctx(20));book.settle(self.ctx(20))
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','reserves','RU')).native_mU,2000000)
        self.assertEqual(bond.face,1001000)
        self.assertEqual(bond.due_cycle,70)
        self.assertEqual(bond.annual_rate_bp,1500)
        self.assertEqual(book.default_count['n0'],1)
        self.assertEqual(state.credit['n0'],max(Q(1,10),credit-Q(3,10)))
        self.assertEqual(state.initial.populations['n0-h0'].support_mU,max(0,support-100))
        self.assertTrue(state.macro.cycles['n0'].defaulted)
        state.audit()

    def test_interest_priority_and_proportional_principal_for_two_bonds(self):
        state=self.state();book=state.sovereign_finance
        a=self.bond(state,face=100000,rate=1000,id='a')
        b=self.bond(state,face=300000,rate=1000,id='b')
        self.cash(state,native=100400)
        book.reserve(self.ctx(20));book.settle(self.ctx(20))
        self.assertEqual(a.face,75000)
        self.assertEqual(b.face,225000)
        self.assertEqual(book.reports['n0']['paid_mU'],100400)
        self.assertEqual(book.default_count['n0'],1)
        state.audit()

    def test_micro_bond_split_does_not_round_away_coupon(self):
        state=self.state();book=state.sovereign_finance
        for n in range(20):self.bond(state,face=100,rate=500,due=100,id=str(n))
        self.cash(state,native=1000)
        before=state.initial.cash.balance(CashAccount('world','pool','n0')).native_mU
        book.reserve(self.ctx(1));book.settle(self.ctx(1))
        self.assertEqual(book.reports['n0']['due_mU'],1)
        self.assertEqual(state.initial.cash.balance(CashAccount('world','pool','n0')).native_mU,before+1)
        state.audit()

    def test_actual_command_assembly_pays_first_coupon_in_following_cycle(self):
        game=opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=20,commerce=True,debug=False)
        state=game.state;state.research['n0'].known.add('F02')
        command={'id':'1','kind':'finance','op':'bond_issue','term':20,'face_mU':1000000,'min_price_mU':1000}
        game.step({'n0':[command]})
        self.assertEqual(state.command_streams['n0'].recent[-1].status,'ACCEPTED')
        self.assertFalse(state.sovereign_finance.bonds)
        self.assertIn('1',state.command_streams['n0'].plans)
        for _ in range(9):game.step()
        bond,=state.sovereign_finance.bonds.values()
        self.assertNotIn('1',state.command_streams['n0'].plans)
        self.assertEqual(state.sovereign_finance.reports['n0']['due_mU'],0)
        for _ in range(10):game.step()
        expected=1000000*bond.annual_rate_bp//1000000
        self.assertEqual(state.sovereign_finance.reports['n0'],{'due_mU':expected,'paid_mU':expected,'defaulted':False})
        self.assertEqual(bond.face,1000000)
        public=state.sovereign_finance.visible('n1')['bonds']
        self.assertEqual(public[0]['id'],'bond-1')
        self.assertNotIn(bond.id,str(public))
        frozen=fingerprint(state.snapshot())
        for _ in range(10):state.sovereign_finance.visible('n1')
        self.assertEqual(frozen,fingerprint(state.snapshot()))
        state.audit()

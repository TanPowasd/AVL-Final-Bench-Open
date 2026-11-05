import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.corporate_finance import DividendCommands
from organizer.rift_era.engine.economic_ownership import record_income
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.economy_state import OperatingWorld,CycleEconomy
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import CashAccount
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.tests import test_compute_operations as compute_fixture


class CorporateFinanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def fixture(self):
        state=OperatingWorld(self.catalog,initialize(self.catalog,self.world))
        return state,state.initial.companies['n0-tech']

    def income(self,state,company,amount,currency=None):
        currency=currency or company.issuer
        target=state.corporate_finance.cash(company,currency)
        state.initial.cash.transfer(CashAccount('world','pool',currency),target,amount)
        record_income(state,(target,),(amount,))

    def ctx(self,cycle=0):return compute_fixture.ComputeOperationsTests.ctx(cycle,9)

    def test_positive_cash_profit_tax_and_minorities_retained_cash_are_conserved(self):
        state,company=self.fixture()
        from organizer.rift_era.engine.stock_market import EquityIssue,LongLot
        market=state.stock_market
        market.shares.transfer(market.account('n0',company.id,'unissued'),market.account('n1',company.id),490000000)
        market.external_issues[company.id].append(EquityIssue(490000,Q(0),Q(1)))
        market.long_lots['n1',company.id,Q(1)]=LongLot(490000,Q(0))
        market._sync()
        self.income(state,company,2000)
        cash=state.corporate_finance.cash(company)
        state.initial.cash.transfer(cash,state.native('n0','household:n0-h0'),800)
        state.cycles['n0'].costs['corp:tech']=Q(800)
        state.corporate_finance.payout_mU[company.id]=500
        before=state.initial.cash.balance(state.native('n0','treasury')).native_mU
        state.corporate_finance.taxes(self.ctx());state.corporate_finance.dividends(self.ctx())
        row=state.corporate_finance.reports[company.id]
        self.assertEqual((row.profit_native_mU,row.tax_paid_native_mU,row.dividend_paid_native_mU),(1200,240,480))
        self.assertEqual(state.initial.cash.balance(cash).native_mU,480)
        self.assertEqual(state.initial.cash.balance(state.native('n0','treasury')).native_mU,before+485)
        self.assertEqual(state.initial.cash.balance(CashAccount('n1','treasury','n0')).native_mU,235)
        self.assertEqual(company.shares,{'n0':510000,'n1':490000})
        state.audit()

    def test_negative_cash_profit_does_not_mint_valuation_income_or_pay_dividend(self):
        state,company=self.fixture();cash=state.corporate_finance.cash(company)
        state.initial.cash.transfer(state.native('n0','treasury'),cash,500) # declared old working capital, not income
        self.income(state,company,1000)
        state.initial.cash.transfer(cash,state.native('n0','household:n0-h0'),1200)
        state.cycles['n0'].costs['corp:tech']=Q(1200)
        state.corporate_finance.payout_mU[company.id]=1000
        before=state.initial.cash.snapshot()
        state.corporate_finance.taxes(self.ctx());state.corporate_finance.dividends(self.ctx())
        row=state.corporate_finance.reports[company.id]
        self.assertEqual((row.profit_native_mU,row.tax_paid_native_mU,row.dividend_paid_native_mU),(-200,0,0))
        self.assertEqual(state.initial.cash.snapshot(),before)
        self.assertEqual(state.initial.cash.balance(cash).native_mU,300)

    def test_ru_profit_uses_actual_finite_fx_and_includes_real_fee(self):
        state,company=self.fixture();state.rates['n0']=Q(2)
        self.income(state,company,100000,'RU')
        state.corporate_finance.taxes(self.ctx());state.corporate_finance.dividends(self.ctx())
        row=state.corporate_finance.reports[company.id]
        self.assertEqual(row.profit_native_mU,49980)
        self.assertEqual(row.tax_paid_native_mU,9996)
        self.assertEqual(state.initial.cash.balance(state.corporate_finance.cash(company,'RU')).native_mU,79960)
        self.assertEqual(state.initial.cash.balance(state.corporate_finance.cash(company)).native_mU,4)
        self.assertEqual(state.cycles['n0'].cash_income_by_currency['RU']['corp:tech'],100000)
        state.initial.cash.audit()

    def test_illiquid_native_pool_keeps_real_tax_and_dividend_claims(self):
        state,company=self.fixture();state.rates['n0']=Q(2);cash=state.initial.cash
        self.income(state,company,100000,'RU');state.corporate_finance.payout_mU[company.id]=1000
        pool=CashAccount('world','pool','n0')
        cash.transfer(pool,CashAccount('world','sequestered_test','n0'),cash.balance(pool).native_mU)
        state.corporate_finance.taxes(self.ctx());state.corporate_finance.dividends(self.ctx())
        row=state.corporate_finance.reports[company.id]
        self.assertEqual((row.tax_paid_native_mU,row.tax_arrears_native_mU,row.dividend_paid_native_mU),(0,10000,0))
        self.assertEqual(state.corporate_finance.dividend_claims[company.id],{'n0':40000})
        self.assertEqual(cash.balance(state.corporate_finance.cash(company,'RU')).native_mU,100000)
        state.cycles={p:CycleEconomy() for p in state.initial.known}
        cash.transfer(CashAccount('world','sequestered_test','n0'),pool,60000)
        state.corporate_finance.taxes(self.ctx(1));state.corporate_finance.dividends(self.ctx(1))
        row=state.corporate_finance.reports[company.id]
        self.assertEqual(row.tax_arrears_native_mU,0)
        self.assertEqual(row.dividend_paid_native_mU,39900)
        self.assertEqual(state.corporate_finance.dividend_claims[company.id],{'n0':100})
        self.assertEqual(cash.balance(state.corporate_finance.cash(company)).native_mU,0)
        state.audit()

    def test_dividend_policy_checks_control_and_schema_without_mutating(self):
        state,company=self.fixture();handler=DividendCommands(state)
        order={'id':'1','kind':'finance','op':'dividend','company':company.id,'payout_mU':500}
        for owner,change in (('n1',{}),('n0',{'payout_mU':1001}),('n0',{'payout_mU':True})):
            with self.assertRaises(RuleViolation):handler.prepare(owner,order|change,self.ctx())
            self.assertEqual(state.corporate_finance.payout_mU[company.id],0)
        handler.prepare('n0',order,self.ctx()).commit()
        self.assertEqual(state.corporate_finance.payout_mU[company.id],500)

    def test_real_economy_corporate_settlement_occurs_once_after_payroll(self):
        game=opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=20,commerce=True,debug=False)
        state=game.state
        for tick in range(20):
            game.step({'n0':[{'id':'1','kind':'finance','op':'dividend','company':'n0-energy','payout_mU':500}]} if tick==0 else {})
            if tick%10==9:
                self.assertEqual(state.corporate_finance.last_tax_cycle,tick//10)
                self.assertEqual(state.corporate_finance.last_dividend_cycle,tick//10)
        self.assertEqual(state.command_streams['n0'].recent[-1].status,'ACCEPTED')
        state.audit()

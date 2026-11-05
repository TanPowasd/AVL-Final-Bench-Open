import unittest
from organizer.rift_era.tests import test_sovereign_finance as fixtures
from organizer.rift_era.engine.bond_market import BondOrderCommands
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.ledger import CashAccount


class BondMarketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SovereignFinanceTests.setUpClass()
        cls.helper=fixtures.SovereignFinanceTests()

    def fixture(self):
        state=self.helper.state()
        bond=self.helper.bond(state,face=100000,rate=600)
        state.sovereign_finance.publish()
        return state,bond

    def order(self,state,owner,side='buy',qty=100000,seq='1',price=1000,bid='b'):
        alias=state.sovereign_finance.aliases[owner][bid]
        return BondOrderCommands(state).prepare(owner,{'id':seq,'kind':'finance','op':'bond_order',
            'bond_id':alias,'side':side,'qty_mU':qty,'limit_price_mU':price},self.helper.ctx(1))

    @staticmethod
    def fund(state,owner,amount):
        state.initial.cash.transfer(CashAccount('world','pool','n0'),CashAccount(owner,'treasury','n0'),amount)

    def test_dvp_changes_financial_face_without_creating_physical_inventory(self):
        state,bond=self.fixture();self.fund(state,'n1',100000)
        before=fingerprint(state.initial.inventory.snapshot())
        self.order(state,'n1').commit()
        self.assertEqual(state.initial.cash.balance(CashAccount('n1','treasury','n0')).native_mU,0)
        state.bond_market.clear(self.helper.ctx(1))
        self.assertEqual(bond.holders_face_mU,{'n1':100000})
        self.assertEqual(bond.face,100000)
        self.assertEqual(before,fingerprint(state.initial.inventory.snapshot()))
        self.assertFalse(state.bond_market.exchange.orders)
        state.audit()

    def test_sell_custody_rejection_is_pure_and_cancel_restores_cash(self):
        state,bond=self.fixture();self.fund(state,'n1',100000)
        self.order(state,'n1').commit()
        state.bond_market.clear(self.helper.ctx(1))
        self.order(state,'n1',side='sell',seq='2').commit()
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):self.order(state,'n1',side='sell',seq='3')
        self.assertEqual(before,fingerprint(state.snapshot()))
        state.bond_market.cancel('n1','2').commit()
        self.assertEqual(state.bond_market.faces.balance(state.bond_market.account('n1',bond.id)),100000)
        self.fund(state,'n2',100000)
        self.order(state,'n2').commit()
        state.bond_market.cancel('n2','1').commit()
        self.assertEqual(state.initial.cash.balance(CashAccount('n2','treasury','n0')).native_mU,100000)
        state.audit()

    def test_world_pool_cannot_spend_other_venue_same_batch_proceeds(self):
        state,a=self.fixture()
        self.fund(state,'n1',100000);self.order(state,'n1').commit()
        state.bond_market.clear(self.helper.ctx(1))
        b=self.helper.bond(state,face=100000,rate=600,id='second')
        state.sovereign_finance.publish()
        self.fund(state,'n2',100000)
        cash=state.initial.cash;pool=CashAccount('world','pool','n0')
        cash.transfer(pool,CashAccount('test','pool_escrow','n0'),cash.balance(pool).native_mU)
        self.order(state,'n1',side='sell',seq='2',price=900).commit()
        self.order(state,'n2',bid=b.id).commit()
        state.bond_market.clear(self.helper.ctx(2))
        self.assertEqual(a.holders_face_mU,{'n1':100000})
        self.assertEqual(b.holders_face_mU,{'n2':100000})
        self.assertGreater(cash.balance(pool).native_mU,0)
        state.bond_market.clear(self.helper.ctx(3))
        self.assertEqual(a.holders_face_mU,{'world':100000})
        state.audit()

    def test_issuer_buyback_extinguishes_debt_instead_of_defaulting_to_itself(self):
        state,bond=self.fixture()
        self.order(state,'n0').commit()
        state.bond_market.clear(self.helper.ctx(1))
        self.assertFalse(state.sovereign_finance.bonds)
        self.assertFalse(state.bond_market.exchange.venues)
        self.assertFalse(state.bond_market.faces.balances())
        state.audit()

    def test_price_band_prevents_outside_limit_cross(self):
        state,bond=self.fixture();self.fund(state,'n1',100000)
        self.order(state,'n1').commit();state.bond_market.clear(self.helper.ctx(1))
        self.order(state,'n1',side='sell',seq='2',price=2000).commit()
        self.fund(state,'n2',300000);self.order(state,'n2',price=3000).commit()
        state.bond_market.clear(self.helper.ctx(2))
        self.assertEqual(bond.holders_face_mU,{'n1':100000})
        self.assertEqual(state.bond_market.exchange.prices[bond.id],1000)
        state.audit()

    def test_retired_series_does_not_erase_fractional_principal_obligation(self):
        state=self.helper.state()
        first=self.helper.bond(state,face=1,rate=0,id='tiny')
        state.sovereign_finance.publish();self.fund(state,'n1',2)
        self.order(state,'n1',bid=first.id,qty=1,price=999).commit()
        state.bond_market.clear(self.helper.ctx(1))
        state.sovereign_finance.settle(self.helper.ctx(20))
        second=self.helper.bond(state,face=1,rate=0,due=50,id='next_tiny')
        state.sovereign_finance.publish()
        cash=CashAccount('n1','treasury','n0');before=state.initial.cash.balance(cash).native_mU
        self.order(state,'n1',bid=second.id,qty=1,price=999,seq='2').commit()
        state.bond_market.clear(self.helper.ctx(21))
        self.assertEqual(state.initial.cash.balance(cash).native_mU,before-1)
        self.assertEqual(second.holders_face_mU,{'n1':1})
        state.audit()

    def test_active_order_cap_is_shared_with_commodity_market(self):
        from organizer.rift_era.engine.commands import EngineCommands
        state,_=self.fixture();self.fund(state,'n1',256)
        for seq in range(1,257):self.order(state,'n1',qty=1,seq=str(seq)).commit()
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation) as raised:
            EngineCommands(state).prepare('n1',{'id':'300','kind':'logistics','op':'market_ask',
                'hub_id':'n1-h0','good':'food','qty_mU':1,'limit_price_mU':1000,'expire_cycles':1},self.helper.ctx(1))
        self.assertEqual(raised.exception.code,'ERR_ENTITY_CAP_EXCEEDED')
        self.assertEqual(before,fingerprint(state.snapshot()))
        state.bond_market.cancel('n1','1').commit()
        self.assertEqual(state.active_order_count('n1'),255)
        state.audit()
